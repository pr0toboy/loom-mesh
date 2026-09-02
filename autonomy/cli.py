#!/usr/bin/env python3
"""``loom`` CLI — how agents and the coordinator drive a run from the shell.

One entry point, three command groups::

    python3 -m autonomy.cli run   start|end|status ...
    python3 -m autonomy.cli task  new|assign|claim|start|progress|block|unblock|submit|review|done|list ...
    python3 -m autonomy.cli mine                       # tasks open for me right now
    python3 -m autonomy.cli log   <kind> --field k=v   # free making-of event

The project root is ``$LOOM_ROOT`` (or ``--root``). The acting agent is
``$MESH_AGENT`` (the pane's tmux session name wins inside a real tmux), so the
same wrappers the hook references (``loom-task progress ...``) Just Work when an
agent runs them. Output is plain text for humans and JSON with ``--json``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import worktree
from .board import Board
from .run import Run


def _root(args) -> str:
    root = (getattr(args, "root", None) or os.environ.get("LOOM_ROOT", "")).strip()
    if not root:
        sys.exit("no project root: pass --root or set LOOM_ROOT")
    return root


def _agent(args=None) -> str:
    """Resolve the acting agent's identity.

    Precedence: an explicit ``--as <name>`` flag wins always (this is how the
    coordinator stamps its identity when it drives the board *over SSH*, where
    there is no ``$TMUX`` and no ``MESH_AGENT`` — without it the events were
    recorded as ``unknown``). Otherwise the pane's tmux session name wins inside
    a real tmux, then ``$LOOM_AGENT``/``$MESH_AGENT`` from the environment, and
    finally ``unknown`` as a last resort."""
    explicit = (getattr(args, "as_agent", None) or "").strip().lower() if args is not None else ""
    if explicit:
        return explicit
    if os.environ.get("TMUX"):
        try:
            import subprocess
            # Deliberately plain `tmux`, not MESH_TMUX: guarded by $TMUX above,
            # this asks the server we are RUNNING INSIDE which session we are —
            # not which server the deployment drives. Pointing it elsewhere would
            # answer with someone else's session name.
            r = subprocess.run(["tmux", "display-message", "-p", "#S"],
                               capture_output=True, text=True, timeout=2)
            name = r.stdout.strip().lower()
            if name:
                return name
        except Exception:
            pass
    return (os.environ.get("LOOM_AGENT", "").strip().lower()
            or os.environ.get("MESH_AGENT", "").strip().lower()
            or "unknown")


def _csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _emit(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    elif isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False))


# --- run --------------------------------------------------------------------

def cmd_run(args):
    run = Run(_root(args))
    if args.run_cmd == "start":
        guardrails = {}
        if args.cap is not None:
            guardrails["window_token_cap"] = args.cap
        if args.max_iter is not None:
            if args.max_iter < 1:
                sys.exit("--max-iter must be >= 1 (0 would silently disable the cap, "
                         "a negative value would end the run on the first tick)")
            guardrails["max_iterations"] = args.max_iter
        if args.usage_sources:
            guardrails["usage_sources"] = _csv(args.usage_sources)
        state = run.start(args.id, _csv(args.participants), goal=args.goal or "",
                          guardrails=guardrails)
        _emit(state, args.json)
    elif args.run_cmd == "end":
        _emit(run.end(reason=args.reason or ""), args.json)
    elif args.run_cmd == "status":
        _emit(run.status(), args.json)
    elif args.run_cmd == "gc":
        _emit(worktree.branch_gc(_root(args), args.into), args.json)


# --- task -------------------------------------------------------------------

def cmd_task(args):
    board = Board(_root(args))
    me = _agent(args)
    c = args.task_cmd
    if c == "new":
        ev = board.create_task(args.id, args.title or args.id, description=args.description or "",
                               scope=args.scope, owner=args.owner, deps=_csv(args.deps),
                               acceptance=_csv(args.acceptance), branch=args.branch, by=me)
        _emit(ev, args.json)
    elif c == "assign":
        _emit(board.assign(args.id, args.owner, by=me), args.json)
    elif c == "claim":
        # Guarded claim: two agents racing to claim the same task must not both
        # win (the race F3 closes). None = the task was taken/moved on first.
        ev = board.claim_if_available(args.id, me)
        _emit(ev if ev is not None else {"claimed": None, "reason": "task already taken or not claimable"},
              args.json)
    elif c == "start":
        _emit(board.start(args.id, me), args.json)
    elif c == "progress":
        _emit(board.progress(args.id, me, note=args.message or ""), args.json)
    elif c == "block":
        _emit(board.block(args.id, me, reason=args.message or "blocked"), args.json)
    elif c == "unblock":
        _emit(board.unblock(args.id, by=me), args.json)
    elif c == "submit":
        _emit(board.submit(args.id, me, branch=args.branch, artifacts=_csv(args.artifacts),
                           note=args.message or ""), args.json)
    elif c == "review":
        _emit(board.review(args.id, me, passed=args.passed, notes=args.message or ""), args.json)
    elif c == "done":
        _emit(board.done(args.id, by=me), args.json)
    elif c == "reopen":
        ev = board.reopen(args.id, by=me, reason=args.message or "", to=args.to)
        _emit(ev if ev is not None else {"reopened": None, "reason": "task is not done"}, args.json)
    elif c == "abandon":
        ev = board.abandon(args.id, by=me, reason=args.message or "")
        _emit(ev if ev is not None else {"abandoned": None, "reason": "task is already terminal"}, args.json)
    elif c == "deps":
        _emit(board.set_deps(args.id, _csv(args.deps), by=me), args.json)
    elif c == "list":
        tasks = board.tasks()
        if args.json:
            _emit(tasks, True)
        else:
            for t in tasks.values():
                owner = t["owner"] or "-"
                print(f"{t['id']:<10} {t['status']:<12} {owner:<12} {t['title']}")
    elif c == "worktree":
        t = board.tasks().get(args.id)
        branch = (t.get("branch") if t else None) or worktree.default_branch(args.id)
        path = worktree.ensure(_root(args), args.id, branch=branch, base=args.base)
        # record the branch on the board if it wasn't set, so review/integrate find it
        if t and not t.get("branch"):
            board.log("worktree_created", by=me, task=args.id, branch=branch, path=str(path))
        _emit(str(path) if not args.json else {"task": args.id, "branch": branch, "path": str(path)},
              args.json)
    elif c == "worktree-rm":
        removed = worktree.remove(_root(args), args.id)
        _emit({"task": args.id, "removed": removed}, args.json)


def cmd_mine(args):
    board = Board(_root(args))
    me = _agent(args)
    opn = board.open_for(me)
    if args.json:
        _emit(opn, True)
    else:
        if not opn:
            print(f"(no open tasks for {me})")
        for t in opn:
            print(f"{t['id']:<10} {t['status']:<12} {t['title']}")


def cmd_log(args):
    board = Board(_root(args))
    fields = {}
    for kv in args.field or []:
        k, _, v = kv.partition("=")
        fields[k.strip()] = v.strip()
    _emit(board.log(args.kind, by=_agent(args), **fields), args.json)


# --- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loom", description="LoomMesh autonomy CLI")
    p.add_argument("--root", help="project root (default $LOOM_ROOT)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--as", dest="as_agent", metavar="AGENT",
                   help="act as this agent (overrides tmux/$LOOM_AGENT/$MESH_AGENT; "
                        "use it when driving the board over SSH so events aren't 'unknown')")
    sub = p.add_subparsers(dest="group", required=True)

    # run
    pr = sub.add_parser("run", help="run lifecycle")
    rs = pr.add_subparsers(dest="run_cmd", required=True)
    p_start = rs.add_parser("start")
    p_start.add_argument("id")
    p_start.add_argument("--participants", required=True, help="comma-separated agent names")
    p_start.add_argument("--goal", default="")
    p_start.add_argument("--cap", type=int, help="window token cap guardrail")
    p_start.add_argument("--max-iter", type=int, dest="max_iter")
    p_start.add_argument("--usage-sources", dest="usage_sources",
                         help="comma-separated expected usage host labels (enables "
                              "missing-host → unknown fail-closed detection)")
    p_end = rs.add_parser("end")
    p_end.add_argument("--reason", default="")
    rs.add_parser("status")
    p_gc = rs.add_parser("gc", help="remove run worktrees + delete merged loom/* branches")
    p_gc.add_argument("--into", default="main", help="integration branch to check merges against")
    pr.set_defaults(func=cmd_run)

    # task
    pt = sub.add_parser("task", help="task operations")
    ts = pt.add_subparsers(dest="task_cmd", required=True)
    p_new = ts.add_parser("new")
    p_new.add_argument("id")
    p_new.add_argument("--title")
    p_new.add_argument("--description")
    p_new.add_argument("--scope")
    p_new.add_argument("--owner")
    p_new.add_argument("--deps", help="comma-separated task ids")
    p_new.add_argument("--acceptance", help="comma-separated acceptance criteria")
    p_new.add_argument("--branch")
    for name in ("assign",):
        q = ts.add_parser(name)
        q.add_argument("id")
        q.add_argument("owner")
    for name in ("claim", "start", "unblock", "done"):
        ts.add_parser(name).add_argument("id")
    for name in ("progress", "block", "abandon"):
        q = ts.add_parser(name)
        q.add_argument("id")
        q.add_argument("-m", "--message", default="")
    p_reopen = ts.add_parser("reopen", help="reopen a done task (coordinator remediation)")
    p_reopen.add_argument("id")
    p_reopen.add_argument("-m", "--message", default="")
    p_reopen.add_argument("--to", choices=("in_review", "in_progress"), default="in_review")
    p_deps = ts.add_parser("deps", help="replace a task's dependency list")
    p_deps.add_argument("id")
    p_deps.add_argument("--deps", default="", help="comma-separated task ids (empty clears)")
    p_sub = ts.add_parser("submit")
    p_sub.add_argument("id")
    p_sub.add_argument("--branch")
    p_sub.add_argument("--artifacts", help="comma-separated paths")
    p_sub.add_argument("-m", "--message", default="")
    p_rev = ts.add_parser("review")
    p_rev.add_argument("id")
    g = p_rev.add_mutually_exclusive_group(required=True)
    g.add_argument("--pass", dest="passed", action="store_true")
    g.add_argument("--fail", dest="passed", action="store_false")
    p_rev.add_argument("-m", "--message", default="")
    ts.add_parser("list")
    p_wt = ts.add_parser("worktree", help="create/return this task's isolated git worktree")
    p_wt.add_argument("id")
    p_wt.add_argument("--base", default="HEAD", help="base ref to branch from (e.g. main)")
    ts.add_parser("worktree-rm").add_argument("id")
    pt.set_defaults(func=cmd_task)

    # mine
    sub.add_parser("mine", help="tasks open for me right now").set_defaults(func=cmd_mine)

    # log
    pl = sub.add_parser("log", help="record a free making-of event")
    pl.add_argument("kind", help="e.g. commit, skill_created, hook_created, mcp_created, discussion")
    pl.add_argument("--field", action="append", help="k=v (repeatable)")
    pl.set_defaults(func=cmd_log)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
