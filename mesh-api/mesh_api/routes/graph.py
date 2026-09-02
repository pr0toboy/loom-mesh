"""GET /graph — mesh activity graph over a time window."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from ..auth import require_auth
from ..models import GraphResponse, GraphNode, GraphEdge
from ..peers import AGENTS, ALL_PEERS

router = APIRouter()

MESH_DIR = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
#: The graph always shows the human, even before they have sent anything —
#: a mesh with no operator node reads as a system with no one driving it.
#: Named for the role, not for a person: this ships to other people's meshes.
OPERATOR_NODE = os.environ.get("MESH_OPERATOR_NODE", "operator")
TICKETS_DIR = Path(os.environ.get("MESH_TICKETS_DIR", str(MESH_DIR / "tickets")))

# Node colours, supplied by the deployment: this module knows no agent of its
# own. An optional JSON file {"<agent>": "#RRGGBB"}, pointed at by
# MESH_NODE_COLORS (default: $MESH_HOME/node-colors.json). Missing file means an
# empty palette, and the front end falls back to its own default colour.
def _load_node_colors() -> dict:
    path = Path(os.environ.get("MESH_NODE_COLORS", str(MESH_DIR / "node-colors.json")))
    try:
        data = json.loads(path.read_text())
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


NODE_COLORS = _load_node_colors()

WINDOW_MAP = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}

# Canonicalise OLD ids onto current names. A deployment that has renamed its
# agents still holds HISTORICAL messages carrying the old id: without this
# fallback the graph would draw two nodes for one agent. Format of
# MESH_AGENT_ALIASES: "old:new,old2:new2". Empty by default.
ALIASES = {
    k.strip(): v.strip()
    for k, _, v in (pair.partition(":") for pair in os.environ.get("MESH_AGENT_ALIASES", "").split(","))
    if k.strip() and v.strip()
}


def _canon(name: str) -> str:
    return ALIASES.get(name, name)


def _parse_window(window: str) -> timedelta:
    hours = WINDOW_MAP.get(window)
    if hours is None:
        raise ValueError(f"Invalid window: {window}")
    return timedelta(hours=hours)


def _build_graph(since: datetime) -> tuple[list[GraphNode], list[GraphEdge]]:
    edge_counts: dict[tuple[str, str, str], list[str]] = {}  # (from, to, kind) → [ts]

    # Count messages from inbox JSONL files
    for agent in AGENTS:
        inbox = MESH_DIR / f"inbox-{agent}.jsonl"
        if not inbox.exists():
            continue
        with inbox.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    ts_str = msg.get("ts", "")
                    if not ts_str:
                        continue
                    ts = datetime.fromisoformat(ts_str)
                    if ts < since:
                        continue
                    from_ = _canon(msg.get("from", ""))
                    to = _canon(msg.get("to", ""))
                    if from_ and to:
                        key = (from_, to, "message")
                        edge_counts.setdefault(key, []).append(ts_str)
                except Exception:
                    continue

    # Count tickets from done dirs
    for agent in AGENTS:
        done_dir = TICKETS_DIR / agent / "done"
        if not done_dir.exists():
            continue
        for f in done_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                ts_str = data.get("completed_at") or data.get("queued_at") or ""
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str)
                if ts < since:
                    continue
                from_ = _canon(data.get("from", OPERATOR_NODE))
                to = _canon(data.get("to", agent))
                key = (from_, to, "ticket")
                edge_counts.setdefault(key, []).append(ts_str)
            except Exception:
                continue

    # Build edge objects
    edges: list[GraphEdge] = []
    for (from_, to, kind), timestamps in edge_counts.items():
        last_ts = max(timestamps) if timestamps else None
        edges.append(GraphEdge(
            **{"from": from_},
            to=to,
            weight=len(timestamps),
            kind=kind,
            last_ts=last_ts,
        ))

    # Build node set from edges + all known peers
    node_ids: set[str] = set(ALL_PEERS) | {OPERATOR_NODE}
    for (from_, to, _) in edge_counts:
        node_ids.add(from_)
        node_ids.add(to)

    nodes: list[GraphNode] = []
    for node_id in sorted(node_ids):
        ntype = "operator" if node_id == OPERATOR_NODE else "agent"
        label = node_id.replace("-", " ").title()
        color = NODE_COLORS.get(node_id, "#607D8B")
        nodes.append(GraphNode(id=node_id, type=ntype, label=label, color=color))

    return nodes, edges


@router.get("/graph", response_model=GraphResponse)
async def get_graph(
    window: str = Query("1h", pattern=r"^(1h|6h|24h|7d)$"),
    _: str = Depends(require_auth),
):
    try:
        delta = _parse_window(window)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    since = datetime.now(timezone.utc) - delta
    nodes, edges = _build_graph(since)
    ts = datetime.now(timezone.utc).isoformat()

    return GraphResponse(window=window, generated_at=ts, nodes=nodes, edges=edges)
