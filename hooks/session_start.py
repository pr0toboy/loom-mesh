#!/usr/bin/env python3
"""SessionStart hook: hand an agent its unread messages as it opens.

Without this, a message that arrived while an agent was down waits until someone
thinks to read the inbox. With it, every session — cold boot, resume, or a
context reset — starts by knowing what it owes.

The three judgement calls in here are the interesting part.

**Who am I?** Getting this wrong corrupts another agent's read cursor, so it is
not taken on trust. When the process really is inside a tmux pane (``$TMUX`` is
set), the pane's session name wins over ``MESH_AGENT``: a shared tmux server
started by one agent's service exports its own name into every pane it later
creates, so the variable can name a different agent entirely. When there is no
pane — the hook invoked over SSH from another machine — tmux would answer with
whatever session lives *there*, so the environment is trusted instead.

**What counts as unread?** Cursor position alone is not enough. A message pushed
live mid-session and acknowledged then is still *newer* than the cursor, which
only moves here; re-injecting it makes the agent do the work twice. So an
acknowledged message is never unread, whatever the cursor says.

**Say something after a reset.** After a compact or a clear the harness hands
the agent a fresh turn with no instruction. Staying silent there — the natural
behaviour when the inbox is empty — makes an agent acknowledge and stop, mid
task, looking like it decided to quit. On those two sources the hook always
emits a resume directive.

Environment:
    MESH_HOME    bus directory (default ~/mesh)
    MESH_AGENT   this agent's name (authoritative only outside a tmux pane)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MESH_DIR = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))

MAX_INJECTED = 10


def _known_agents() -> frozenset[str]:
    """Roster from the deployment; empty when the mesh is not deployed yet.

    Loaded by path rather than by putting a directory on ``sys.path``. The mesh
    home was prepended to it, which means a file called ``json.py`` dropped in
    there would shadow the standard library for this hook — and the mesh home is
    writable by every agent. Same trust domain, but a hook that runs before every
    session should not widen it for a convenience.
    """
    for candidate in (MESH_DIR / "roster.py",
                      Path(__file__).resolve().parents[1] / "bus" / "roster.py"):
        if not candidate.is_file():
            continue
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location("_loom_roster_hook", candidate)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            peers = frozenset(getattr(mod, "INBOX_PEERS", ()) or ())
            if peers:
                return peers
        except Exception:
            continue
    return frozenset()


AGENTS = _known_agents()


def detect_agent() -> str | None:
    env_agent = os.environ.get("MESH_AGENT", "").strip().lower()
    tmux_agent = None
    if os.environ.get("TMUX"):
        try:
            out = subprocess.run(["tmux", "display-message", "-p", "#S"],
                                 capture_output=True, text=True, timeout=2)
            name = out.stdout.strip()
            if name in AGENTS:
                tmux_agent = name
        except Exception:
            pass

    if tmux_agent:
        if env_agent and env_agent != tmux_agent:
            # stderr, never stdout: stdout is injected into the agent's context.
            print(f"[session-start] identity conflict: MESH_AGENT={env_agent!r} but "
                  f"tmux session={tmux_agent!r} → using {tmux_agent!r} (the pane wins)",
                  file=sys.stderr)
        return tmux_agent
    return env_agent if env_agent in AGENTS else None


def _is_after_cursor(msg_ts: str, msg_id: str | None, cursor: object) -> bool:
    if not isinstance(cursor, dict):
        return True
    base_ts = cursor.get("ts", "")
    if str(msg_ts) > str(base_ts):
        return True
    if str(base_ts) > str(msg_ts):
        return False
    seen = cursor.get("ids_at_ts")
    if not isinstance(seen, list):
        base_id = cursor.get("id")
        seen = [base_id] if base_id is not None else []
    return msg_id not in seen


def load_inbox(agent: str) -> list[dict]:
    path = MESH_DIR / f"inbox-{agent}.jsonl"
    if not path.exists():
        return []
    msgs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue          # one bad line must not hide the inbox
        if isinstance(rec, dict):
            msgs.append(rec)
    return msgs


def load_state(agent: str) -> dict:
    path = MESH_DIR / f"state-{agent}.json"
    if not path.exists():
        return {"last_read": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"last_read": {}}


def save_state(agent: str, state: dict) -> None:
    try:
        (MESH_DIR / f"state-{agent}.json").write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


def advance_cursor(state: dict, msgs: list[dict]) -> dict:
    """Move each sender's cursor to their newest injected message.

    The ids seen at that exact timestamp are kept: two messages sent in the same
    second are otherwise indistinguishable from one another, and the sibling
    that was not injected would be skipped forever.
    """
    last_read = state.setdefault("last_read", {})
    for m in msgs:
        sender, ts, mid = m.get("from"), m.get("ts", ""), m.get("id")
        if not sender:
            continue
        cur = last_read.get(sender)
        if isinstance(cur, dict) and cur.get("ts") == ts:
            ids = cur.get("ids_at_ts") or []
            if mid not in ids:
                ids.append(mid)
            cur["ids_at_ts"] = ids
        elif not isinstance(cur, dict) or str(ts) > str(cur.get("ts", "")):
            last_read[sender] = {"ts": ts, "id": mid, "ids_at_ts": [mid]}
    return state


def read_source() -> str:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return "startup"
    return (data or {}).get("source", "startup")


def digest(agent: str, unread: list[dict], resume: bool) -> str:
    lines: list[str] = []
    if resume:
        lines += [
            "Your context was just reset — this is not a signal to stop.",
            "Pick the thread back up: deal with anything below, then continue the "
            "task that was in progress. Only conclude if there is genuinely "
            "nothing left.",
            "",
        ]
    if unread:
        lines.append(f"## {len(unread)} unread message(s) in your inbox\n")
        for m in unread[:MAX_INJECTED]:
            body = (m.get("body") or "").strip()
            lines.append(f"**[{m.get('ts')}] {m.get('from')} → you** "
                         f"(id={m.get('id')}, {m.get('priority')})")
            lines.append(body)
            lines.append("")
        if len(unread) > MAX_INJECTED:
            lines.append(f"_({len(unread) - MAX_INJECTED} more — "
                         f"`python3 {MESH_DIR}/read.py {agent} --unacked`)_")
        lines.append(f"Acknowledge what you handle: "
                     f"`python3 {MESH_DIR}/read.py {agent} --ack <id>`")
    return "\n".join(lines).strip()


def main() -> None:
    source = read_source()
    agent = detect_agent()
    if agent is None:
        return          # not a mesh agent: emit nothing at all

    resume = source in {"compact", "clear"}
    msgs = load_inbox(agent)
    state = load_state(agent)
    last_read = state.get("last_read", {})

    unread = [
        m for m in msgs
        if not m.get("acked")
        and _is_after_cursor(m.get("ts", ""), m.get("id"), last_read.get(m.get("from")))
    ]

    if not unread and not resume:
        return

    text = digest(agent, unread, resume)
    if not text:
        return

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": text,
    }}))

    if unread:
        save_state(agent, advance_cursor(state, unread))


if __name__ == "__main__":
    main()
