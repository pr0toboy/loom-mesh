"""GET /projtime — development time accumulated per project, from the projtime ledger."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends
from ..auth import require_auth

router = APIRouter()
LEDGER = os.environ.get("PROJTIME_LEDGER", str(Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh"))) / "projtime-ledger.json"))


@router.get("/projtime")
async def projtime(_: str = Depends(require_auth)):
    try:
        with open(LEDGER, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {"open": {}, "totals": {}, "intervals": []}
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    projects = []
    for proj, t in (d.get("totals") or {}).items():
        projects.append({
            "project": proj,
            "total_seconds": int(t.get("seconds", 0)),
            "today_seconds": int((t.get("by_day") or {}).get(today, 0)),
            "by_agent": {a: int(s) for a, s in (t.get("by_agent") or {}).items()},
        })
    projects.sort(key=lambda x: -x["total_seconds"])
    open_now = []
    now = datetime.now(timezone.utc)
    for agent, op in (d.get("open") or {}).items():
        try:
            elapsed = int((now - datetime.fromisoformat(op["start"])).total_seconds())
        except Exception:
            elapsed = 0
        open_now.append({"agent": agent, "project": op.get("project"),
                         "elapsed_seconds": elapsed})
    open_now.sort(key=lambda x: -x["elapsed_seconds"])
    return {"projects": projects, "open": open_now, "today": today}
