"""CRUD operations on the filesystem ticket store under $MESH_HOME/tickets/."""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..peers import AGENTS, DEFAULT_SENDER

TICKETS_DIR = Path(os.environ.get("MESH_TICKETS_DIR",
    str(Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh"))) / "tickets")))
_TICKET_ID_RE = re.compile(r"^tk-[0-9a-f]{6}$")
STATES = ("draft", "armed", "blocked", "queued", "running", "done", "failed", "cancelled")


def ensure_dirs() -> None:
    for agent in AGENTS:
        for state in STATES:
            (TICKETS_DIR / agent / state).mkdir(parents=True, exist_ok=True)


def scaffold_working_dir(ticket_id: str, agent: str) -> None:
    """Create working/<tk-id>/ with 4 template files. Idempotent — skips existing files."""
    working_dir = TICKETS_DIR / agent / "working" / ticket_id
    working_dir.mkdir(parents=True, exist_ok=True)
    working_dir.chmod(0o755)
    todo = (
        f"# Todo — {ticket_id}\n\n"
        "## To do\n- [ ] ...\n\n"
        "## In progress\n- [ ] ...\n\n"
        "## Done\n- [x] ...\n"
    )
    for fname, content in [("brief.md", ""), ("todo.md", todo), ("notes.md", ""), ("output.md", "")]:
        p = working_dir / fname
        if not p.exists():
            p.write_text(content)
            p.chmod(0o644)


def archive_working_dir(ticket_id: str, agent: str, final_status: str, tldr: str | None = None) -> None:
    """Write tldr to output.md and move working/<tk-id>/ to <final_status>/<tk-id>/."""
    working_dir = TICKETS_DIR / agent / "working" / ticket_id
    if not working_dir.exists():
        return
    if tldr:
        (working_dir / "output.md").write_text(tldr)
    dest = TICKETS_DIR / agent / final_status / ticket_id
    if dest.exists():
        shutil.rmtree(dest)
    working_dir.rename(dest)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_ticket_id(ticket_id: str) -> None:
    if not _TICKET_ID_RE.match(ticket_id):
        raise ValueError(f"Invalid ticket_id: {ticket_id!r}")


def _ticket_path(agent: str, status: str, ticket_id: str) -> Path | None:
    _validate_ticket_id(ticket_id)
    d = TICKETS_DIR / agent / status
    for f in d.glob(f"*{ticket_id}*"):
        return f
    return None


def find_ticket(ticket_id: str, agent: Optional[str] = None) -> tuple[dict, Path] | None:
    _validate_ticket_id(ticket_id)
    agents = [agent] if agent else list(AGENTS)
    for ag in agents:
        for state in STATES:
            d = TICKETS_DIR / ag / state
            for f in d.glob(f"*{ticket_id}*.json"):
                try:
                    data = json.loads(f.read_text())
                    return data, f
                except Exception:
                    continue
    return None


def list_tickets(agent: str, status_filter: str = "all", limit: int = 100) -> list[dict]:
    states = [status_filter] if status_filter != "all" else list(STATES)
    results = []
    for state in states:
        d = TICKETS_DIR / agent / state
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                results.append(data)
            except Exception:
                continue
    results.sort(key=lambda t: t.get("queued_at") or "", reverse=True)
    return results[:limit]


def _next_queue_prefix(agent: str) -> str:
    d = TICKETS_DIR / agent / "queued"
    existing = sorted(d.glob("*.json"))
    if not existing:
        return "001"
    last = existing[-1].name
    m = re.match(r"^(\d+)-", last)
    n = int(m.group(1)) + 1 if m else len(existing) + 1
    return f"{n:03d}"


def create_ticket(
    to: str,
    prompt: str,
    priority: str = "normal",
    dispatch_mode: str = "draft",
    depends_on: list[str] | None = None,
    parent_ticket_id: str | None = None,
    from_: str = DEFAULT_SENDER,
) -> dict:
    ensure_dirs()
    ticket_id = f"tk-{uuid.uuid4().hex[:6]}"
    ts = _now_iso()
    ticket: dict = {
        "id": ticket_id,
        "to": to,
        "from": from_,
        "queued_at": ts,
        "started_at": None,
        "completed_at": None,
        "status": dispatch_mode,  # initially same as dispatch_mode
        "dispatch_mode": dispatch_mode,
        "priority": priority,
        "prompt": prompt,
        "tldr": None,
        "depends_on": depends_on or [],
        "parent_ticket_id": parent_ticket_id,
    }

    initial_state = dispatch_mode  # "draft" or "armed"
    # If armed but has unresolved deps, place directly in blocked
    if dispatch_mode == "armed" and depends_on:
        if not _all_deps_done(depends_on):
            initial_state = "blocked"
            ticket["status"] = "blocked"

    dest = TICKETS_DIR / to / initial_state
    if initial_state == "queued":
        prefix = _next_queue_prefix(to)
        dest_file = dest / f"{prefix}-{ticket_id}.json"
    else:
        dest_file = dest / f"{ticket_id}.json"

    dest_file.write_text(json.dumps(ticket, indent=2, ensure_ascii=False))
    scaffold_working_dir(ticket_id, to)
    return ticket


def _all_deps_done(depends_on: list[str]) -> bool:
    for dep_id in depends_on:
        found = False
        for agent in AGENTS:
            d = TICKETS_DIR / agent / "done"
            if any(d.glob(f"*{dep_id}*")):
                found = True
                break
        if not found:
            return False
    return True


def patch_ticket(agent: str, ticket_id: str, patch: dict) -> dict:
    result = find_ticket(ticket_id, agent)
    if result is None:
        raise KeyError(ticket_id)
    data, current_path = result
    current_status = data.get("status", "")

    if current_status in ("running", "done", "failed", "cancelled"):
        raise PermissionError(f"Cannot patch ticket in status={current_status}")

    if "prompt" in patch:
        data["prompt"] = patch["prompt"]

    if "dispatch_mode" in patch:
        new_mode = patch["dispatch_mode"]
        data["dispatch_mode"] = new_mode
        if new_mode == "draft":
            new_status = "draft"
        elif new_mode == "armed":
            if data.get("depends_on") and not _all_deps_done(data["depends_on"]):
                new_status = "blocked"
            else:
                new_status = "armed"
        else:
            new_status = current_status
        data["status"] = new_status
        new_path = TICKETS_DIR / agent / new_status / current_path.name
        new_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.rename(new_path)
        new_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return data

    if "position" in patch and current_status == "queued":
        pos = int(patch["position"])
        queued_dir = TICKETS_DIR / agent / "queued"
        files = sorted(queued_dir.glob("*.json"))
        pure_name = re.sub(r"^\d+-", "", current_path.name)
        if pos == 0:
            prefix = "000"
        else:
            prefix = f"{pos:03d}"
        new_path = queued_dir / f"{prefix}-{pure_name}"
        current_path.rename(new_path)
        new_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return data

    current_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


def delete_ticket(agent: str, ticket_id: str) -> None:
    result = find_ticket(ticket_id, agent)
    if result is None:
        raise KeyError(ticket_id)
    data, path = result
    status = data.get("status", "")
    if status == "running":
        data["status"] = "cancelled"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        cancelled_path = TICKETS_DIR / agent / "cancelled" / path.name
        cancelled_path.parent.mkdir(parents=True, exist_ok=True)
        path.rename(cancelled_path)
        archive_working_dir(ticket_id, agent, "cancelled")
    elif status in ("done", "failed", "cancelled"):
        raise PermissionError(f"Cannot delete ticket with status={status}")
    else:
        path.unlink(missing_ok=True)
        working_dir = TICKETS_DIR / agent / "working" / ticket_id
        if working_dir.exists():
            shutil.rmtree(working_dir)


def get_response_path(agent: str, ticket_id: str) -> Path | None:
    # complete_ticket()/archive_working_dir() write output.md inside the DIRECTORY
    # done/<ticket_id>/, not a file named done/*<id>*.out.md — so the glob this
    # replaces matched nothing, ever, and every response came back empty.
    candidate = TICKETS_DIR / agent / "done" / ticket_id / "output.md"
    if candidate.exists():
        return candidate
    return None
