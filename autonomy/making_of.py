#!/usr/bin/env python3
"""Making-of generator — turn a run's event log into the "what happened" doc.

The brief was explicit: alongside the delivered software and its docs,
produce a narrative of *how it was built* — the tasks, who did what, the tools
the agents created for themselves (skills, hooks, MCP servers), the blockers and
decisions, the discussion. Because the board is an append-only event log, this
writes almost by itself: we fold the same ``board.jsonl`` into prose. No second
source to keep in sync.

``render(root)`` returns the markdown; the CLI writes it to a file::

    python3 -m autonomy.making_of --root <project> -o MAKING_OF.md
"""
from __future__ import annotations

from datetime import datetime, timezone

from .board import Board
from .run import Run


def _instant(ts):
    """Parse an ISO-8601 timestamp into a comparable UTC ``datetime``.

    The board stamps its run window in UTC (``…+00:00``) while a mesh bus may
    stamp messages in local time with an offset (``…+02:00``). Comparing those
    as raw strings is wrong — ``"11:00:04+02:00"`` sorts *after*
    ``"09:00:43+00:00"`` even though they are the same instant. We normalise to
    UTC so the window filter compares real instants. A naive timestamp (no
    offset) is assumed UTC. Unparseable / empty → ``None`` (caller keeps the
    message rather than dropping it on a parse error)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Free event kinds that document tooling the agents built for themselves.
_TOOLING = {
    "skill_created": "Skill",
    "hook_created": "Hook",
    "mcp_created": "MCP server",
    "command_created": "Command",
}


def _fmt_ts(ts: str) -> str:
    return (ts or "").replace("T", " ")[:19]


def load_jsonl_messages(paths) -> list[dict]:
    """Load mesh-style messages from one or more JSONL files. Each line is a
    message object; we only rely on the generic ``from``/``to``/``ts``/``body``
    fields, so any bus that writes those works. Missing files / torn lines are
    skipped. This keeps the repo generic — the live system passes its own inbox
    paths; nothing here hardcodes a mesh location."""
    import json
    from pathlib import Path
    out: list[dict] = []
    if isinstance(paths, (str, Path)):
        paths = [paths]
    for p in paths:
        fp = Path(p)
        if not fp.is_file():
            continue
        try:
            with fp.open(encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        continue
        except Exception:
            continue
    return out


def filter_discussion(messages, participants, since=None, until=None) -> list[dict]:
    """Keep messages exchanged between run participants within [since, until].

    A message is in scope if either its sender or recipient is a participant and
    its timestamp falls in the window (open-ended if a bound is None). Returned
    sorted by timestamp, de-duplicated by (ts, from, to, body) so the same
    message appearing in several inboxes is shown once."""
    who = set(participants or [])
    lo, hi = _instant(since), _instant(until)
    seen = set()
    kept = []
    for m in messages:
        frm, to = m.get("from"), m.get("to")
        if who and frm not in who and to not in who:
            continue
        ts = m.get("ts", "")
        mt = _instant(ts)
        # Compare as real instants (UTC-normalised). An unparseable timestamp
        # (mt is None) is kept rather than silently dropped.
        if mt is not None:
            if lo is not None and mt < lo:
                continue
            if hi is not None and mt > hi:
                continue
        key = (ts, frm, to, m.get("body", ""))
        if key in seen:
            continue
        seen.add(key)
        kept.append(m)
    kept.sort(key=lambda m: (_instant(m.get("ts", "")) or datetime.min.replace(tzinfo=timezone.utc)))
    return kept


def _task_title(events, tid) -> str:
    for e in events:
        if e.get("type") == "task_created" and e.get("task") == tid:
            return e.get("title", tid)
    return tid


def render(root, discussion=None) -> str:
    """Render the making-of. ``discussion`` is an optional list of mesh messages
    (``{from, to, ts, body}``); when given, the real inter-agent conversation
    during the run is filtered to the run window/participants and woven in. Pass
    it via :func:`load_jsonl_messages` + :func:`filter_discussion`, or let the CLI
    ``--mesh`` option do it."""
    board = Board(root)
    run = Run(root)
    events = board.events()
    tasks = board.tasks()
    state = run.status()

    out: list[str] = []
    goal = state.get("goal") or "(goal not recorded)"
    out.append(f"# Making-of — {goal}\n")

    # --- run summary --------------------------------------------------------
    parts = ", ".join(state.get("participants", [])) or "—"
    out.append("## Run\n")
    out.append(f"- **Goal**: {goal}")
    out.append(f"- **Participants**: {parts}")
    if state.get("started"):
        out.append(f"- **Started**: {_fmt_ts(state['started'])}")
    if state.get("ended"):
        out.append(f"- **Ended**: {_fmt_ts(state['ended'])} ({state.get('end_reason', 'n/a')})")
    done = sum(1 for t in tasks.values() if t["status"] == "done")
    out.append(f"- **Tasks**: {done}/{len(tasks)} done")
    commits = [e for e in events if e.get("type") == "commit"]
    if commits:
        out.append(f"- **Commits**: {len(commits)}")
    out.append("")

    # --- what was built -----------------------------------------------------
    out.append("## What was built\n")
    if not tasks:
        out.append("_No tasks were recorded._\n")
    for t in tasks.values():
        mark = {"done": "✅", "in_review": "🔎", "blocked": "⛔",
                "in_progress": "⏳", "assigned": "📌", "todo": "•"}.get(t["status"], "•")
        line = f"- {mark} **{t['id']}** — {t['title']}"
        if t["owner"]:
            line += f" _(by {t['owner']})_"
        if t["branch"]:
            line += f" — branch `{t['branch']}`"
        out.append(line)
        if t["artifacts"]:
            out.append(f"    - artifacts: {', '.join(f'`{a}`' for a in t['artifacts'])}")
        if t["status"] == "blocked" and t["block_reason"]:
            out.append(f"    - blocked: {t['block_reason']}")
    out.append("")

    # --- tooling the agents created ----------------------------------------
    tooling = [e for e in events if e.get("type") in _TOOLING]
    if tooling:
        out.append("## Tooling the agents created for themselves\n")
        for e in tooling:
            label = _TOOLING[e["type"]]
            name = e.get("name", "(unnamed)")
            by = e.get("by", "?")
            why = e.get("why") or e.get("purpose") or ""
            line = f"- **{label}**: `{name}` — by {by}"
            if why:
                line += f" — {why}"
            out.append(line)
        out.append("")

    # --- blockers & decisions ----------------------------------------------
    decisions = [e for e in events
                 if e.get("type") in ("task_blocked", "task_unblocked", "usage_kill",
                                       "review_failed", "decision", "escalation")]
    if decisions:
        out.append("## Blockers & decisions\n")
        for e in decisions:
            ts = _fmt_ts(e.get("ts", ""))
            tid = e.get("task")
            who = e.get("by", "?")
            etype = e["type"]
            if etype == "task_blocked":
                out.append(f"- `{ts}` ⛔ **{tid}** blocked by {who}: {e.get('reason', '')}")
            elif etype == "task_unblocked":
                out.append(f"- `{ts}` ▶ **{tid}** unblocked by {who}")
            elif etype == "usage_kill":
                out.append(f"- `{ts}` 🛑 usage kill-switch tripped: {e.get('reason', '')}")
            elif etype == "review_failed":
                out.append(f"- `{ts}` 🔁 **{tid}** review failed: {e.get('notes', '')}")
            else:
                out.append(f"- `{ts}` 🗳 {etype} ({who}): {e.get('note') or e.get('reason', '')}")
        out.append("")

    # --- discussion logged on the board ------------------------------------
    logged = [e for e in events if e.get("type") in ("discussion", "note")]
    if logged:
        out.append("## Discussion (logged)\n")
        for e in logged:
            ts = _fmt_ts(e.get("ts", ""))
            who = e.get("by", "?")
            msg = e.get("message") or e.get("note") or ""
            out.append(f"- `{ts}` **{who}**: {msg}")
        out.append("")

    # --- real inter-agent conversation pulled from the mesh ----------------
    if discussion:
        convo = filter_discussion(
            discussion, state.get("participants", []),
            since=state.get("started"), until=state.get("ended"),
        )
        if convo:
            out.append("## Conversation (mesh)\n")
            out.append("_The agents' own exchanges during the run:_\n")
            for m in convo:
                ts = _fmt_ts(m.get("ts", ""))
                frm = m.get("from", "?")
                to = m.get("to", "?")
                body = (m.get("body", "") or "").strip().replace("\n", " ")
                if len(body) > 280:
                    body = body[:277] + "…"
                out.append(f"- `{ts}` **{frm} → {to}**: {body}")
            out.append("")

    # --- full timeline ------------------------------------------------------
    out.append("## Timeline\n")
    for e in events:
        ts = _fmt_ts(e.get("ts", ""))
        tid = e.get("task")
        who = e.get("by", "")
        suffix = f" — {_task_title(events, tid)}" if tid else ""
        who_s = f" ({who})" if who else ""
        out.append(f"- `{ts}` {e.get('type')}{who_s}" + (f" · {tid}{suffix}" if tid else ""))
    out.append("")

    return "\n".join(out)


def _main(argv=None) -> int:
    import argparse
    import os
    p = argparse.ArgumentParser(description="Generate a run's making-of from its event log")
    p.add_argument("--root", default=os.environ.get("LOOM_ROOT", ""), help="project root")
    p.add_argument("-o", "--output", help="write to this file instead of stdout")
    p.add_argument("--mesh", action="append", metavar="JSONL",
                   help="mesh inbox JSONL file(s) to weave the real conversation in (repeatable / globs)")
    args = p.parse_args(argv)
    if not args.root:
        raise SystemExit("no project root: pass --root or set LOOM_ROOT")
    discussion = None
    if args.mesh:
        import glob
        paths = [p for pat in args.mesh for p in glob.glob(pat)] or args.mesh
        discussion = load_jsonl_messages(paths)
    md = render(args.root, discussion=discussion)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(md, encoding="utf-8")
        print(f"wrote {args.output} ({len(md)} bytes)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
