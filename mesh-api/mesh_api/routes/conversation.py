"""GET /conversation/{agent} — unified message + ticket timeline for an agent."""
from fastapi import APIRouter, Depends, HTTPException, Query
from ..auth import require_auth
from ..lib import conversation_io
from ..peers import ALL_PEERS

router = APIRouter()


@router.get("/conversation/{agent}")
async def get_conversation(
    agent: str,
    limit: int = Query(100, ge=1, le=500),
    before: str | None = Query(None),
    _: str = Depends(require_auth),
):
    if agent not in ALL_PEERS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    return conversation_io.get_conversation(agent, limit=limit, before=before)
