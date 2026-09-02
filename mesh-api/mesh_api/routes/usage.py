"""GET /usage — Max plan usage feed.

The feed is produced by an external collector and written to
``$MESH_HOME/usage/max-usage.json``; this route serves it read-only
behind the same bearer auth as the rest of the API (so the dashboard fetches it
with its token rather than the file being exposed as a static asset)."""
import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_auth

router = APIRouter()

USAGE_FILE = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh"))) / "usage" / "max-usage.json"
HISTORY_FILE = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh"))) / "usage" / "max-usage-history.jsonl"


@router.get("/usage")
async def usage(_: str = Depends(require_auth)):
    if not USAGE_FILE.exists():
        raise HTTPException(status_code=404, detail="usage feed not available yet")
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=502, detail="usage feed unreadable")


@router.get("/usage/history")
async def usage_history(limit: int = 180, _: str = Depends(require_auth)):
    """Time series for the dashboard sparkline: the last `limit` points
    ({t, five_hour, seven_day, sonnet, opus}) from the history JSONL."""
    if not HISTORY_FILE.exists():
        return {"points": []}
    limit = max(1, min(limit, 1000))
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise HTTPException(status_code=502, detail="usage history unreadable")
    points = []
    for ln in lines[-limit:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            points.append(json.loads(ln))
        except json.JSONDecodeError:
            continue  # skip malformed lines, don't break the series
    return {"points": points}
