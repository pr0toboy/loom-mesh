import re
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from ..auth import require_auth, require_write_auth
from ..models import (
    TicketCreateRequest, BulkTicketCreateRequest,
    TicketPatchRequest, TicketCreateResponse, BulkCreateResponse,
    TicketsListResponse, TicketOut,
)
from ..lib import tickets_io
from ..peers import AGENTS, DEFAULT_SENDER

router = APIRouter()

_PLACEHOLDER_RE = re.compile(r"^_step(\d+)$")
_TICKET_ID_RE = re.compile(r"^tk-[0-9a-f]{6}$")


def _check_ticket_id(ticket_id: str) -> None:
    if not _TICKET_ID_RE.match(ticket_id):
        raise HTTPException(status_code=422, detail=f"Invalid ticket_id format: {ticket_id!r}")


def _ticket_to_out(t: dict, agent: str) -> TicketOut:
    response_url = None
    if t.get("status") == "done":
        response_url = f"/tickets/{agent}/{t['id']}/response"
    preview = (t.get("prompt") or "")[:80]
    return TicketOut(
        id=t["id"],
        **{"from": t.get("from", DEFAULT_SENDER)},
        to=t.get("to", agent),
        status=t.get("status", ""),
        dispatch_mode=t.get("dispatch_mode", "draft"),
        priority=t.get("priority", "normal"),
        prompt=t.get("prompt", ""),
        prompt_preview=preview,
        depends_on=t.get("depends_on", []),
        parent_ticket_id=t.get("parent_ticket_id"),
        queued_at=t.get("queued_at"),
        started_at=t.get("started_at"),
        completed_at=t.get("completed_at"),
        tldr=t.get("tldr"),
        position=t.get("position"),
        full_response_url=response_url,
    )


@router.post("/tickets", response_model=TicketCreateResponse)
async def create_ticket(req: TicketCreateRequest, _: str = Depends(require_write_auth)):
    if req.to not in AGENTS:
        raise HTTPException(status_code=422, detail=f"Unknown target agent: {req.to}")
    ticket = tickets_io.create_ticket(
        to=req.to,
        prompt=req.prompt,
        priority=req.priority.value,
        dispatch_mode=req.dispatch_mode.value,
        depends_on=req.depends_on,
        parent_ticket_id=req.parent_ticket_id,
        from_=req.from_,
    )
    return TicketCreateResponse(
        id=ticket["id"],
        to=ticket["to"],
        status=ticket["status"],
        queued_at=ticket["queued_at"],
        position=ticket.get("position"),
    )


def _validate_placeholders(tickets: list) -> None:
    """Validate _stepN placeholder references before bulk creation."""
    for i, item in enumerate(tickets):
        for dep in item.depends_on:
            m = _PLACEHOLDER_RE.match(dep)
            if m:
                n = int(m.group(1))
                if n >= i:
                    raise HTTPException(
                        status_code=422,
                        detail=f"tickets[{i}].depends_on: placeholder '{dep}' references index {n} which is not before {i}",
                    )
            elif not re.match(r"^[0-9a-f]{8}$", dep):
                # not a valid placeholder and not a known 8-hex ticket ID format
                # only reject if it looks like a malformed placeholder attempt
                if dep.startswith("_step"):
                    raise HTTPException(
                        status_code=422,
                        detail=f"tickets[{i}].depends_on: malformed placeholder '{dep}' — expected _step<N> with integer N",
                    )


def _resolve_placeholders(depends_on: list[str], resolver: dict[str, str]) -> list[str]:
    resolved = []
    for dep in depends_on:
        m = _PLACEHOLDER_RE.match(dep)
        if m:
            resolved.append(resolver[dep])
        else:
            resolved.append(dep)
    return resolved


@router.post("/tickets/bulk", response_model=BulkCreateResponse)
async def create_bulk_tickets(req: BulkTicketCreateRequest, _: str = Depends(require_write_auth)):
    if req.to not in AGENTS:
        raise HTTPException(status_code=422, detail=f"Unknown target agent: {req.to}")
    _validate_placeholders(req.tickets)
    placeholder_resolver: dict[str, str] = {}
    ids = []
    for i, item in enumerate(req.tickets):
        resolved_deps = _resolve_placeholders(item.depends_on, placeholder_resolver)
        ticket = tickets_io.create_ticket(
            to=req.to,
            prompt=item.prompt,
            priority=req.priority.value,
            dispatch_mode=req.dispatch_mode.value,
            depends_on=resolved_deps,
            from_=req.from_,
        )
        placeholder_resolver[f"_step{i}"] = ticket["id"]
        ids.append(ticket["id"])
    return BulkCreateResponse(ids=ids, created_count=len(ids))


@router.get("/tickets/{agent}", response_model=TicketsListResponse)
async def list_tickets(
    agent: str,
    status: str = Query("all"),
    limit: int = Query(100, le=500),
    _: str = Depends(require_auth),
):
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    # `status` used to reach a path glob (TICKETS_DIR/agent/status) unvalidated:
    # path traversal, and a 500 for anything that did not resolve. Bounded to the
    # known enum instead.
    if status != "all" and status not in tickets_io.STATES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status!r}")
    tickets = tickets_io.list_tickets(agent, status_filter=status, limit=limit)
    return TicketsListResponse(
        agent=agent,
        tickets=[_ticket_to_out(t, agent) for t in tickets],
        total=len(tickets),
    )


@router.get("/tickets/{agent}/{ticket_id}", response_model=TicketOut)
async def get_ticket(agent: str, ticket_id: str, _: str = Depends(require_auth)):
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    _check_ticket_id(ticket_id)
    result = tickets_io.find_ticket(ticket_id, agent)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    data, _ = result
    return _ticket_to_out(data, agent)


@router.get("/tickets/{agent}/{ticket_id}/response", response_class=PlainTextResponse)
async def get_ticket_response(agent: str, ticket_id: str, _: str = Depends(require_auth)):
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    _check_ticket_id(ticket_id)
    path = tickets_io.get_response_path(agent, ticket_id)
    if path is None:
        raise HTTPException(status_code=404, detail="No response file for this ticket")
    return path.read_text()


@router.patch("/tickets/{agent}/{ticket_id}", response_model=TicketOut)
async def patch_ticket(
    agent: str, ticket_id: str, patch: TicketPatchRequest,
    _: str = Depends(require_write_auth),
):
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    _check_ticket_id(ticket_id)
    try:
        data = tickets_io.patch_ticket(agent, ticket_id, patch.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _ticket_to_out(data, agent)


@router.delete("/tickets/{agent}/{ticket_id}", status_code=204)
async def delete_ticket(agent: str, ticket_id: str, _: str = Depends(require_write_auth)):
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    _check_ticket_id(ticket_id)
    try:
        tickets_io.delete_ticket(agent, ticket_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
