"""Kanban routes — the task board, readable freely, writable only with a token.

Reading (`GET /kanban`) follows the same regime as the rest of the dashboard
(private network plus bypass). Writing goes through `require_write_auth`, which
ignores the bypass; auth.py explains why.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_auth, require_write_auth
from ..lib import kanban_io

router = APIRouter(prefix="/kanban", tags=["kanban"])


class CardCreate(BaseModel):
    column: str
    text: str = Field(min_length=1, max_length=500)
    tag: str | None = None


class CardPatch(BaseModel):
    column: str | None = None
    done: bool | None = None


def _guard(fn, *a, **kw):
    """A domain error (unknown column, card gone, store busy) answers 4xx, not 500.

    409 for "the board moved under you" is the one that earns its place: it tells
    the client to reload rather than retry the same write.
    """
    try:
        return fn(*a, **kw)
    except kanban_io.BoardError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("")
async def get_board(_: str = Depends(require_auth)):
    return kanban_io.read_board()


@router.post("/cards")
async def create_card(req: CardCreate, _: str = Depends(require_write_auth)):
    return _guard(kanban_io.add_card, req.column, req.text, req.tag)


@router.patch("/cards/{card_id}")
async def patch_card(card_id: str, req: CardPatch, _: str = Depends(require_write_auth)):
    if req.done is None and req.column is None:
        raise HTTPException(status_code=422, detail="nothing to change")
    board = None
    if req.done is not None:
        board = _guard(kanban_io.set_done, card_id, req.done)
    if req.column is not None:
        board = _guard(kanban_io.move_card, card_id, req.column)
    return board


@router.delete("/cards/{card_id}")
async def remove_card(card_id: str, _: str = Depends(require_write_auth)):
    return _guard(kanban_io.delete_card, card_id)
