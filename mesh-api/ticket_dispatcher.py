#!/usr/bin/env python3
"""Ticket dispatcher daemon — watches armed/ dirs and dispatches tickets to agents.

Usage:
    python3 ticket_dispatcher.py [--config PATH]

Config: ~/mesh/ticket-dispatcher.conf (TOML)

Remote agents are detected automatically: an agent for which the config defines
``<agent>_ssh`` is treated as a remote SSH+WSL agent rather than a local tmux
session.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

_MESH_HOME = os.environ.get("MESH_HOME", os.path.expanduser("~/mesh"))
# Optional secondary host, for agents deployed on a second machine and reached
# over SSH. The defaults are empty on purpose: with no explicit configuration
# there is no remote host at all. A default inherited from the original install
# would have this dispatcher opening connections to a machine that does not
# belong to whoever runs it.
_REMOTE_USER = os.environ.get("MESH_REMOTE_USER", "")
_REMOTE_HOST = os.environ.get("MESH_REMOTE_HOST", "")
MESH_DIR = Path(_MESH_HOME)
TICKETS_DIR = Path(os.environ.get("MESH_TICKETS_DIR", str(MESH_DIR / "tickets")))
LOG_PATH = MESH_DIR / "logs" / "ticket-dispatcher.log"
DEFAULT_CONFIG_PATH = MESH_DIR / "ticket-dispatcher.conf"
ALL_STATES = ("draft", "armed", "blocked", "queued", "running", "done", "failed", "cancelled")

# ── Which tmux server, and may we type into it? ───────────────────────────────
#
# _push_local ends with `send-keys Enter`: it RUNS the ticket in the agent's
# pane. Two problems were hiding in a hardcoded "tmux":
#
#  * a test bench could not redirect it at all, short of shadowing `tmux` on
#    PATH — which is what reviewers ended up doing, so the isolation of every
#    bench depended on a trick rather than on this code;
#  * with the default server, the only thing separating a bench from a live
#    agent is that no session happens to be NAMED like one. Deployments of this
#    project name sessions after their agents, so there that separation does
#    not exist: a bench holding a real agent name in its roster would type a
#    command into that agent's pane and press Enter.
#
# So the server is configurable (MESH_TMUX, same variable the bus watcher
# uses), and a mesh home that is not the default one may not drive the default
# server. The rule needs no notion of "production": it is an inconsistency.
_TMUX = shlex.split(os.environ.get("MESH_TMUX", "") or "tmux")
_DEFAULT_MESH_HOME = os.path.expanduser("~/mesh")


def _targets_shared_server() -> bool:
    """True when the tmux command names no server of its own (-L / -S).

    A private server is one this deployment created; whatever sessions it holds,
    they are not the operator's agents. The shared default server is the only
    dangerous target.
    """
    if {"-L", "-S"} & set(_TMUX):
        return False
    # Only the tmux binary itself reaches the shared server: pointing MESH_TMUX
    # at a wrapper or a stub is as deliberate as naming a socket.
    return bool(_TMUX) and os.path.basename(_TMUX[0]) == "tmux"


def _tmux_scope_is_consistent() -> bool:
    # Declaring a server used to be enough, and `MESH_TMUX=tmux` is a declaration
    # that names the SHARED one — so a bench was cleared to dispatch into live
    # agent panes, with an Enter at the end. Measured 2026-09-09; see watcher.sh,
    # which carries the same rule.
    if not _targets_shared_server():
        return True                              # a private server: never the agents
    # Resolved, so a trailing slash or a symlinked home is still the same mesh.
    if os.path.realpath(_MESH_HOME) == os.path.realpath(_DEFAULT_MESH_HOME):
        return True                              # the installed mesh, at its default home
    return os.environ.get("MESH_ALLOW_SHARED_TMUX") == "1"


def _scope_refusal() -> str:
    return (
        f"refusing to drive the default tmux server: this mesh has not declared one "
        f"(MESH_HOME={_MESH_HOME}, default {_DEFAULT_MESH_HOME}). _push_local ends "
        f"with Enter, so it RUNS the ticket in a pane. An installed mesh declares its "
        f"server: bootstrap.sh writes Environment=MESH_TMUX into the units — re-run it, "
        f'or set MESH_TMUX=tmux for the shared default server. Use "tmux -L NAME" only '
        f"if the agent sessions live on that socket too, otherwise every dispatch is "
        f"silently skipped into an empty server."
    )


def _dispatch_agents() -> list[str]:
    """Agents this dispatcher hands tickets to.

    ``MESH_DISPATCH_AGENTS`` (comma-separated) wins when set — it is how you
    narrow a dispatcher to a subset. Otherwise every agent in the deployment's
    roster is dispatched to, which is what someone who just ran ``bootstrap.sh``
    expects, and what makes the guide's ticket example work.
    """
    explicit = [a.strip() for a in os.environ.get("MESH_DISPATCH_AGENTS", "").split(",") if a.strip()]
    if explicit:
        return explicit
    try:
        import importlib.util

        for candidate in (Path(_MESH_HOME) / "roster.py",
                          Path(__file__).resolve().parent.parent / "bus" / "roster.py"):
            if not candidate.is_file():
                continue
            spec = importlib.util.spec_from_file_location("_loom_roster_dispatch", candidate)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            agents = sorted(getattr(mod, "INBOX_PEERS", ()) or ())
            if agents:
                return agents
    except Exception:
        pass
    return []


DEFAULT_CONFIG: dict = {
    "idle_min_minutes": 5,
    "running_timeout_min": 30,
    "tick_interval_sec": 10,
    # The agents this dispatcher drives. Order: the explicit list, else the
    # deployment's roster.
    #
    # It used to be the environment variable alone, defaulting to empty — which
    # meant a freshly bootstrapped mesh had a dispatcher that watched nobody,
    # started cleanly, logged nothing, and simply never dispatched. The variable
    # was documented nowhere and set by no template, so the only way to find out
    # was to wonder why tickets stayed in `queued/`.
    "agents": _dispatch_agents(),
    # Agents hosted on a remote machine: for each one, the dispatcher's config
    # file may define "<agent>_ssh" and "<agent>_tmux_session".
    # Nothing by default — with no configuration, everything is treated as local.
}

log = logging.getLogger("dispatcher")


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if path.exists():
        with path.open("rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def _agent_ssh(agent: str, config: dict) -> str | None:
    """Return SSH host for agent if it's a remote agent, else None."""
    return config.get(f"{agent}_ssh")


def _agent_tmux_session(agent: str, config: dict) -> str:
    """Return tmux session name for agent (defaults to agent name)."""
    return config.get(f"{agent}_tmux_session", agent)


def is_remote(agent: str, config: dict) -> bool:
    return _agent_ssh(agent, config) is not None


# ── Idle detection ─────────────────────────────────────────────────────────────

def _parse_idle(pane_text: str) -> bool:
    """Idle heuristic matching watcher.sh + bypass-mode awareness.

    Busy signal: spinner with elapsed-time like '(1m 31s' or '(45s'.
    Idle signals: 'shift+tab to cycle' (bypass idle) or '← for agents' (normal idle).
    """
    if re.search(r"\(\d+[ms]", pane_text):
        return False
    if "shift+tab to cycle" in pane_text or "← for agents" in pane_text:
        return True
    return bool(re.search(r"^❯", pane_text, re.MULTILINE))


def is_idle_local(agent: str) -> bool:
    alive = subprocess.run([*_TMUX, "has-session", "-t", agent], capture_output=True)
    if alive.returncode != 0:
        return False
    result = subprocess.run(
        [*_TMUX, "capture-pane", "-pt", agent],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    return _parse_idle(result.stdout)


def is_idle_remote(ssh_host: str, tmux_session: str) -> bool:
    cmd = f"tmux capture-pane -pt {tmux_session} 2>/dev/null | grep . | tail -20"
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             ssh_host, f"wsl -d Ubuntu -- bash -lc '{cmd}'"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        return _parse_idle(result.stdout)
    except Exception:
        return False


# ── Dependency check ───────────────────────────────────────────────────────────

def _all_deps_done(depends_on: list[str], agents: list[str]) -> bool:
    for dep_id in depends_on:
        done = False
        for ag in agents:
            if any((TICKETS_DIR / ag / "done").glob(f"*{dep_id}*")):
                done = True
                break
        if not done:
            return False
    return True


# ── Inbox activity guard ───────────────────────────────────────────────────────

def _inbox_age_seconds(agent: str) -> float:
    p = MESH_DIR / f"inbox-{agent}.jsonl"
    if not p.exists():
        return float("inf")
    return time.time() - p.stat().st_mtime


def _armed_ticket_age(f: Path) -> float:
    """Return age in seconds of a ticket file based on queued_at, or 0 on error."""
    try:
        data = json.loads(f.read_text())
        return time.time() - datetime.fromisoformat(data["queued_at"]).timestamp()
    except Exception:
        return 0.0


# ── Prompt composition ─────────────────────────────────────────────────────────

def _compose_dispatch_message(ticket: dict) -> str:
    tid = ticket["id"]
    priority = ticket.get("priority", "normal")
    from_ = ticket.get("from", "?")
    prompt = ticket.get("prompt", "")
    preview = prompt.replace("\n", " ")[:200]
    if len(prompt) > 200:
        preview += "…"
    complete_cmd = f"python3 {_MESH_HOME}/ticket-complete.py {tid} --tldr \"SUMMARY, 1-3 SENTENCES\""
    failed_cmd = f"python3 {_MESH_HOME}/ticket-complete.py {tid} --tldr \"SUMMARY\" --failed \"REASON\""
    return (
        f"[TICKET {tid}] Priority: {priority} - From: {from_} - {preview} - "
        f"Close with: {complete_cmd} | On failure: {failed_cmd}"
    )


# ── Send-keys helpers ──────────────────────────────────────────────────────────

def _push_local(agent: str, text: str) -> bool:
    if not _tmux_scope_is_consistent():
        log.error("REFUSED push to %s: %s", agent, _scope_refusal())
        return False
    try:
        subprocess.run([*_TMUX, "send-keys", "-t", agent, "C-u"], check=True, capture_output=True)
        time.sleep(0.3)
        subprocess.run([*_TMUX, "send-keys", "-t", agent, "-l", text], check=True, capture_output=True)
        time.sleep(0.6)
        subprocess.run([*_TMUX, "send-keys", "-t", agent, "Enter"], check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        log.error("push_local failed for %s: %s", agent, e)
        return False


def _remote_scope_is_consistent() -> bool:
    """Same question as locally, on the branch that had no guard at all.

    The script sent over SSH is a bare `tmux send-keys ... Enter`: it names no
    socket, so it always drives the REMOTE host's shared server — where that
    host's agents live — and it runs what it types. The 2026-09-09 gate proved a
    bench with a hosts.json entry reaching a remote agent that way, with a stub
    ssh. There is no private-socket escape here; what is left is whether this is
    the installation or a copy of it.
    """
    if os.path.realpath(_MESH_HOME) == os.path.realpath(_DEFAULT_MESH_HOME):
        return True
    return os.environ.get("MESH_ALLOW_SHARED_TMUX") == "1"


def _push_remote(ssh_host: str, tmux_session: str, text: str) -> bool:
    if not _remote_scope_is_consistent():
        log.error(
            "refusing to push over SSH from a mesh that is not the installed one "
            "(MESH_HOME=%s, default %s). The remote command runs what it types in "
            "whatever session that host holds under this name; an installed mesh "
            "carries MESH_ALLOW_SHARED_TMUX=1 in its units.",
            _MESH_HOME, _DEFAULT_MESH_HOME,
        )
        return False
    escaped = text.replace("'", "'\"'\"'")
    script = (
        f"tmux send-keys -t {tmux_session} C-u; sleep 0.3; "
        f"tmux send-keys -t {tmux_session} -l '{escaped}'; sleep 0.6; "
        f"tmux send-keys -t {tmux_session} Enter"
    )
    try:
        subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             ssh_host,
             "wsl -d Ubuntu -- bash -c 'cat > /tmp/dispatch-push.sh && bash /tmp/dispatch-push.sh'"],
            input=script.encode(),
            timeout=15,
            check=True,
            capture_output=True,
        )
        return True
    except Exception as e:
        log.error("push_remote failed (%s / %s): %s", ssh_host, tmux_session, e)
        return False


# ── Core dispatch ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_brief_to_working(ticket_id: str, agent: str, prompt: str) -> None:
    """Write prompt to brief.md in working dir at dispatch time (prompt is now final)."""
    brief_path = TICKETS_DIR / agent / "working" / ticket_id / "brief.md"
    if not brief_path.parent.exists():
        return
    if brief_path.exists() and brief_path.read_text().strip():
        return
    try:
        brief_path.write_text(prompt)
    except Exception as e:
        log.warning("failed to write brief.md for %s/%s: %s", agent, ticket_id, e)


def dispatch_ticket(ticket: dict, agent: str, armed_file: Path, config: dict) -> None:
    running_dir = TICKETS_DIR / agent / "running"
    running_dir.mkdir(parents=True, exist_ok=True)

    ticket["status"] = "running"
    ticket["started_at"] = _now_iso()
    ticket["position"] = None

    running_file = running_dir / f"{ticket['id']}.json"
    running_file.write_text(json.dumps(ticket, indent=2, ensure_ascii=False))
    armed_file.unlink(missing_ok=True)
    _extract_brief_to_working(ticket["id"], agent, ticket.get("prompt", ""))

    msg = _compose_dispatch_message(ticket)
    log.info("dispatching ticket %s to %s", ticket["id"], agent)

    if is_remote(agent, config):
        ok = _push_remote(
            _agent_ssh(agent, config),
            _agent_tmux_session(agent, config),
            msg,
        )
    else:
        ok = _push_local(agent, msg)

    if not ok:
        log.warning("send-keys failed for ticket %s / agent %s", ticket["id"], agent)


# ── Tick logic ─────────────────────────────────────────────────────────────────

def process_blocked(agent: str, config: dict) -> None:
    blocked_dir = TICKETS_DIR / agent / "blocked"
    armed_dir = TICKETS_DIR / agent / "armed"
    if not blocked_dir.exists():
        return

    for f in sorted(blocked_dir.glob("*.json")):
        try:
            ticket = json.loads(f.read_text())
        except Exception:
            continue

        deps = ticket.get("depends_on", [])
        if not deps or _all_deps_done(deps, config["agents"]):
            ticket["status"] = "armed"
            armed_dir.mkdir(parents=True, exist_ok=True)
            armed_file = armed_dir / f.name
            armed_file.write_text(json.dumps(ticket, indent=2, ensure_ascii=False))
            f.unlink(missing_ok=True)
            log.info("ticket %s re-promoted from blocked → armed", ticket["id"])


def process_armed(agent: str, config: dict) -> None:
    armed_dir = TICKETS_DIR / agent / "armed"
    blocked_dir = TICKETS_DIR / agent / "blocked"
    running_dir = TICKETS_DIR / agent / "running"
    if not armed_dir.exists():
        return

    if running_dir.exists() and list(running_dir.glob("*.json")):
        return

    # Check agent idle
    if is_remote(agent, config):
        if not is_idle_remote(
            _agent_ssh(agent, config),
            _agent_tmux_session(agent, config),
        ):
            return
    else:
        if not is_idle_local(agent):
            return

    # Check last inbox activity — bypass if a stale armed ticket has been waiting longer.
    age = _inbox_age_seconds(agent)
    min_age = config["idle_min_minutes"] * 60
    if age < min_age:
        stale = any(_armed_ticket_age(f) >= min_age for f in armed_dir.glob("*.json"))
        if not stale:
            log.debug("skip %s — inbox active %ds ago (need >%ds)", agent, int(age), int(min_age))
            return
        log.info("bypass inbox guard for %s — stale armed ticket exists (inbox: %ds)", agent, int(age))

    for f in sorted(armed_dir.glob("*.json")):
        try:
            ticket = json.loads(f.read_text())
        except Exception:
            continue

        deps = ticket.get("depends_on", [])
        if deps and not _all_deps_done(deps, config["agents"]):
            ticket["status"] = "blocked"
            blocked_dir.mkdir(parents=True, exist_ok=True)
            blocked_file = blocked_dir / f.name
            blocked_file.write_text(json.dumps(ticket, indent=2, ensure_ascii=False))
            f.unlink(missing_ok=True)
            log.info("ticket %s moved armed → blocked (unresolved deps)", ticket["id"])
            continue

        dispatch_ticket(ticket, agent, f, config)
        break


def check_timeouts(config: dict) -> None:
    now = time.time()
    timeout_sec = config["running_timeout_min"] * 60

    for agent in config["agents"]:
        running_dir = TICKETS_DIR / agent / "running"
        failed_dir = TICKETS_DIR / agent / "failed"
        if not running_dir.exists():
            continue

        for f in running_dir.glob("*.json"):
            try:
                ticket = json.loads(f.read_text())
            except Exception:
                continue

            started_at = ticket.get("started_at")
            if not started_at:
                continue

            try:
                started_ts = datetime.fromisoformat(started_at).timestamp()
            except ValueError:
                continue

            if now - started_ts > timeout_sec:
                ticket["status"] = "failed"
                ticket["completed_at"] = _now_iso()
                ticket["tldr"] = f"Timeout after {config['running_timeout_min']}min (no ticket-complete call)"
                failed_dir.mkdir(parents=True, exist_ok=True)
                failed_file = failed_dir / f.name
                failed_file.write_text(json.dumps(ticket, indent=2, ensure_ascii=False))
                f.unlink(missing_ok=True)
                log.warning("ticket %s timed out for agent %s", ticket["id"], agent)


def _notify_fcm_safe(ticket: dict, agent: str, failed: bool) -> None:
    """Best-effort FCM push — silently skips if Firebase not configured."""
    try:
        from mesh_api.notifications.fcm import notify_ticket_done
        tldr = ticket.get("tldr") or ticket.get("prompt", "")[:120]
        notify_ticket_done(ticket["id"], agent, tldr or "", failed=failed)
    except Exception as e:
        log.warning("FCM notify failed for ticket %s: %s", ticket.get("id"), e)


def process_done(agent: str) -> None:
    """Notify FCM for newly completed/failed tickets (marks fcm_notified to avoid double-push)."""
    for state, is_failed in (("done", False), ("failed", True)):
        state_dir = TICKETS_DIR / agent / state
        if not state_dir.exists():
            continue
        for f in state_dir.glob("*.json"):
            try:
                ticket = json.loads(f.read_text())
            except Exception:
                continue
            if ticket.get("fcm_notified"):
                continue
            _notify_fcm_safe(ticket, agent, failed=is_failed)
            ticket["fcm_notified"] = True
            f.write_text(json.dumps(ticket, indent=2, ensure_ascii=False))


def tick(config: dict) -> None:
    for agent in config["agents"]:
        process_blocked(agent, config)
        process_armed(agent, config)
        process_done(agent)
    check_timeouts(config)


# ── Watch thread ───────────────────────────────────────────────────────────────

def _start_watcher(config: dict, wake: threading.Event) -> None:
    watch_paths = []
    for agent in config["agents"]:
        for state in ("armed", "done"):
            p = TICKETS_DIR / agent / state
            p.mkdir(parents=True, exist_ok=True)
            watch_paths.append(p)

    def _run():
        try:
            from watchfiles import watch
            for _ in watch(*watch_paths, stop_event=threading.Event()):
                wake.set()
        except Exception as e:
            log.warning("watchfiles watcher failed (%s), relying on polling only", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Mesh ticket dispatcher")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()

    _setup_logging()
    config = load_config(args.config)

    for agent in config["agents"]:
        for state in ALL_STATES:
            (TICKETS_DIR / agent / state).mkdir(parents=True, exist_ok=True)

    wake = threading.Event()
    _start_watcher(config, wake)

    remote = [a for a in config["agents"] if is_remote(a, config)]
    local = [a for a in config["agents"] if not is_remote(a, config)]
    log.info(
        "ticket-dispatcher started — local=%s remote=%s tick=%ds idle_min=%dm timeout=%dm",
        local, remote, config["tick_interval_sec"],
        config["idle_min_minutes"], config["running_timeout_min"],
    )

    while True:
        wake.clear()
        try:
            tick(config)
        except Exception:
            log.exception("unhandled error in tick")
        wake.wait(timeout=config["tick_interval_sec"])


if __name__ == "__main__":
    main()
