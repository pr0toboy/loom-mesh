"""GET /contexts — context window usage for all mesh agents."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from ..auth import require_auth
from ..models import ContextEntry, ContextsResponse
from ..peers import AGENTS as _AGENTS_SET

log = logging.getLogger(__name__)
router = APIRouter()

_AGENT_BASE = os.environ.get("MESH_AGENT_BASE", os.path.expanduser("~"))
_VAULT = os.environ.get("MESH_VAULT", "")
_MESH_HOME = os.environ.get("MESH_HOME", os.path.expanduser("~/mesh"))

CLAUDE_PROJECTS = Path(f"{_AGENT_BASE}/.claude/projects")
SESSIONS_DIR = Path(f"{_AGENT_BASE}/.claude/sessions")
# Which tmux server to LOOK AT. Reading the wrong one is not dangerous, it is
# worse than it sounds: every agent then reports as down and the dashboard shows
# a dead mesh. The writers (bus watcher, ticket dispatcher, night reconcile) take
# the server from MESH_TMUX; the readers have to look at the same one, or the
# status of a deployment with its own socket is simply wrong.
_TMUX = shlex.split(os.environ.get("MESH_TMUX", "") or "tmux")

# Contexts reported by a remote host (the collector is optional).
REMOTE_CONTEXTS_PATH = Path(_MESH_HOME) / os.environ.get("MESH_REMOTE_CONTEXTS", "contexts-remote.json")
REMOTE_STALE_AFTER = timedelta(minutes=30)
_CONTEXTS_CACHE_TTL = 10  # seconds

# A cwd -> agent fallback, supplied by the deployment: this module knows no agent
# of its own. Format of MESH_CWD_MAP: "path:agent,path2:agent2". Empty by default.
#
# It is only a FALLBACK: the authority is the tmux session name, which is the
# agent id. It is needed because several agents can share one working directory —
# the cwd alone then confuses them, and only the session name tells them apart.
CWD_TO_AGENT = {
    k.strip(): v.strip()
    for k, _, v in (pair.rpartition(":") for pair in os.environ.get("MESH_CWD_MAP", "").split(","))
    if k.strip() and v.strip()
}

ALL_AGENTS = sorted(_AGENTS_SET)

_ctx_cache: Optional[list[dict]] = None
_ctx_cache_at: float = 0.0


def _max_tokens(model: str, observed: int) -> int:
    # Opus 4.x has a 1M context window regardless of observed tokens.
    # Detecting by model string avoids the false "10%" display when an Opus
    # session is at 100k tokens but hasn't crossed the 200k heuristic threshold.
    m = model.lower()
    if "opus-4" in m or ("opus" in m and observed > 200_000):
        return 1_000_000
    return 200_000


def _read_session_context(session_id: str) -> dict | None:
    jsonl_path = None
    for project_dir in CLAUDE_PROJECTS.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            jsonl_path = candidate
            break

    if not jsonl_path:
        return None

    try:
        size = jsonl_path.stat().st_size
        with jsonl_path.open("rb") as f:
            f.seek(max(0, size - 262144))
            tail = f.read().decode("utf-8", errors="ignore").splitlines()
    except Exception as e:
        log.warning("failed to read session jsonl %s: %s", jsonl_path, e)
        return None

    for line in reversed(tail):
        try:
            obj = json.loads(line)
            msg = obj.get("message", {})
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not usage or usage.get("cache_read_input_tokens") is None:
                continue
            tokens = usage.get("cache_read_input_tokens", 0) + usage.get("input_tokens", 0)
            model = msg.get("model", "")
            max_tok = _max_tokens(model, tokens)
            return {
                "tokens": tokens,
                "model": model,
                "max_tokens": max_tok,
                "pct": round((tokens / max_tok) * 100, 1) if max_tok else 0,
            }
        except Exception:
            continue

    return None


def _tmux_pane_pids() -> dict[int, str]:
    """Map each tmux pane's shell pid -> its session name.

    On the Pi the tmux session name is the agent id, so this is the ground
    truth that separates agents sharing a cwd. Returns {} if tmux is unavailable — callers fall back to cwd.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    try:
        out = subprocess.run(
            [*_TMUX, "list-panes", "-a", "-F", "#{pane_pid} #{session_name}"],
            capture_output=True, text=True, timeout=5, env=env,
        )
    except Exception as e:
        log.warning("tmux list-panes failed: %s", e)
        return {}
    mapping: dict[int, str] = {}
    for line in out.stdout.splitlines():
        pid_str, _, name = line.partition(" ")
        try:
            mapping[int(pid_str)] = name.strip()
        except ValueError:
            continue
    return mapping


def _agent_from_tmux(pid: int, pane_pids: dict[int, str]) -> str | None:
    """Walk up the process tree from pid until we hit a tmux pane shell,
    returning that pane's session name (== agent id)."""
    cur = pid
    for _ in range(6):
        name = pane_pids.get(cur)
        if name:
            return name
        try:
            with open(f"/proc/{cur}/status") as f:
                ppid = next(
                    (int(l.split()[1]) for l in f if l.startswith("PPid:")), None
                )
        except OSError:
            return None
        if not ppid or ppid <= 1:
            return None
        cur = ppid
    return None


def _local_contexts() -> dict[str, dict]:
    results: dict[str, dict] = {}
    if not SESSIONS_DIR.exists():
        return results

    pane_pids = _tmux_pane_pids()

    for session_file in SESSIONS_DIR.glob("*.json"):
        try:
            session = json.loads(session_file.read_text())
        except Exception:
            continue
        cwd = session.get("cwd")
        pid = session.get("pid")
        # tmux session name is authoritative; cwd map is the fallback.
        agent = None
        if isinstance(pid, int):
            resolved = _agent_from_tmux(pid, pane_pids)
            if resolved in _AGENTS_SET:
                agent = resolved
        if not agent:
            agent = CWD_TO_AGENT.get(cwd)
        if not agent:
            continue
        session_id = session.get("sessionId")
        if not session_id:
            continue
        ctx = _read_session_context(session_id)
        if not ctx:
            continue
        ctx["status"] = session.get("status", "?")
        results[agent] = ctx

    return results


def _build_contexts() -> list[dict]:
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()
    entries: list[dict] = []

    for agent, ctx in _local_contexts().items():
        entries.append({**ctx, "agent": agent, "source": "live", "updated_at": now_str})

    if REMOTE_CONTEXTS_PATH.exists():
        try:
            data = json.loads(REMOTE_CONTEXTS_PATH.read_text())
            updated_at_str = data.get("updated_at", "")
            remote_updated = datetime.fromisoformat(updated_at_str)
            stale = (now - remote_updated) > REMOTE_STALE_AFTER
            source = "stale" if stale else "cached"
            for agent, ctx in data.get("contexts", {}).items():
                entries.append({**ctx, "agent": agent, "source": source, "updated_at": updated_at_str})
        except Exception as e:
            log.warning("failed to read remote contexts: %s", e)

    found = {e["agent"] for e in entries}
    for agent in ALL_AGENTS:
        if agent not in found:
            entries.append({"agent": agent, "source": "unknown"})
    return entries


@router.get("/contexts", response_model=ContextsResponse)
async def get_contexts(_: str = Depends(require_auth)):
    global _ctx_cache, _ctx_cache_at
    now_mono = time.monotonic()
    if _ctx_cache is None or (now_mono - _ctx_cache_at) > _CONTEXTS_CACHE_TTL:
        # _build_contexts shells out to tmux (up to 5 s) and reads files, so it
        # runs off the event loop: inline, the whole API froze for the duration of
        # every rebuild.
        _ctx_cache = await asyncio.to_thread(_build_contexts)
        _ctx_cache_at = now_mono
    entries = _ctx_cache

    return ContextsResponse(contexts=[ContextEntry(**e) for e in entries])
