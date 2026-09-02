from fastapi import APIRouter, Depends, HTTPException, Query
from ..auth import require_auth
from ..models import InboxResponse, MessageOut, SendResponse, AckResponse, SendRequest, AckRequest
from ..lib import mesh_io
from ..peers import ALL_PEERS

router = APIRouter()


@router.get("/inbox/{agent}", response_model=InboxResponse)
async def get_inbox(
    agent: str,
    unread: bool = Query(True),
    limit: int = Query(50, le=200),
    _: str = Depends(require_auth),
):
    if agent not in ALL_PEERS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    messages = mesh_io.read_inbox(agent, unread_only=unread, limit=limit)
    out = []
    for m in messages:
        out.append(MessageOut(
            id=m.get("id", ""),
            **{"from": m.get("from", "")},
            to=m.get("to", ""),
            priority=m.get("priority", "normal"),
            body=m.get("body", ""),
            ts=m.get("ts", ""),
            reply_to=m.get("reply_to"),
        ))
    return InboxResponse(agent=agent, messages=out, total=len(out))
