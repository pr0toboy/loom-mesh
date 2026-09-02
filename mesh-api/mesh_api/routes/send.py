from fastapi import APIRouter, Depends, HTTPException
from ..auth import require_auth, require_write_auth
from ..models import SendRequest, SendResponse, AckRequest, AckResponse
from ..lib import mesh_io
from ..peers import AGENTS, ALL_PEERS

router = APIRouter()


@router.post("/send", response_model=SendResponse)
async def send_message(req: SendRequest, _: str = Depends(require_write_auth)):
    # Validate destination is a known peer (P1-4: 422 not 502)
    if req.to not in ALL_PEERS:
        raise HTTPException(status_code=422, detail=f"Unknown destination peer: {req.to!r}")
    try:
        result = await mesh_io.send_message_async(
            from_=req.from_,
            to=req.to,
            priority=req.priority.value,
            body=req.body,
            reply_to=req.reply_to,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return SendResponse(**result)


@router.post("/ack", response_model=AckResponse)
async def ack_message(req: AckRequest, _: str = Depends(require_write_auth)):
    if req.agent not in ALL_PEERS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {req.agent}")
    try:
        n = mesh_io.ack_message(req.agent, req.id)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return AckResponse(acked=bool(n), count=n)
