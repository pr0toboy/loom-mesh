"""Heuristics for agent alive/idle detection and host stats."""
from __future__ import annotations

import json
import os
import shlex
import re
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

MESH_DIR = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
HEARTBEATS_DIR = MESH_DIR / "heartbeats"
# Which tmux server to LOOK AT. Reading the wrong one is not dangerous, it is
# worse than it sounds: every agent then reports as down and the dashboard shows
# a dead mesh. The writers (bus watcher, ticket dispatcher, night reconcile) take
# the server from MESH_TMUX; the readers have to look at the same one, or the
# status of a deployment with its own socket is simply wrong.
_TMUX = shlex.split(os.environ.get("MESH_TMUX", "") or "tmux")
HOST_DOWN_DIR = MESH_DIR / "state" / "host-down"
ONDEMAND_ASLEEP_DIR = MESH_DIR / "state" / "ondemand-asleep"
# Agent categories, ALL supplied by configuration: this module hardcodes no agent.
# Each variable is a comma-separated list.
#   MESH_ONDEMAND_AGENTS  agents driven by the on-demand daemon — an
#                         ondemand-asleep sentinel counts as "sleeping" only for
#                         these, and an orphan sentinel is ignored;
#   MESH_REMOTE_AGENTS    agents tracked by heartbeat, with no local tmux;
#   MESH_PHONE_AGENTS     agents on a phone (neither tmux nor heartbeat);
#   MESH_DAEMON_AGENTS    agents running as a service; presence = unit is active.
# Empty by default everywhere: with no configuration, no category is populated.


def _env_agents(var: str) -> tuple[str, ...]:
    return tuple(a.strip() for a in os.environ.get(var, "").split(",") if a.strip())


ONDEMAND_AGENTS = _env_agents("MESH_ONDEMAND_AGENTS")
# The daemon refreshes the sentinel's mtime on every pass; past this threshold we
# take the daemon to be dead and the sentinel stale.
ONDEMAND_SENTINEL_MAX_AGE_S = 180
TICKETS_DIR = Path(os.environ.get("MESH_TICKETS_DIR", str(MESH_DIR / "tickets")))
PHONE_LOG = Path(os.environ.get("MESH_PHONE_LOG", ""))  # empty = phone probe disabled
# How presence is decided. Local agents — the ones with a tmux session on this
# machine — fall through to the default branch of get_agent_status(), so only
# REMOTE, PHONE and DAEMON need listing here: their presence is read from a
# heartbeat, a monitor or a service unit rather than from tmux. (There used to be
# a LOCAL_AGENTS list too. Nothing ever read it, so it drifted out of date
# unnoticed and was removed: a list that is not consumed is documentation that
# lies.)
REMOTE_AGENTS = _env_agents("MESH_REMOTE_AGENTS")
PHONE_AGENTS = _env_agents("MESH_PHONE_AGENTS")
DAEMON_AGENTS = _env_agents("MESH_DAEMON_AGENTS")


def _tmux_session_alive(name: str) -> bool:
    result = subprocess.run(
        [*_TMUX, "has-session", "-t", name],
        capture_output=True
    )
    return result.returncode == 0


def _tmux_is_idle(name: str) -> bool:
    result = subprocess.run(
        [*_TMUX, "capture-pane", "-pt", name],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return False
    txt = result.stdout
    if not txt.strip():
        return True
    # A spinner with elapsed-time means Claude is actively working.
    # Activity bar shows timing like "(1m 31s" or "(45s".
    if re.search(r"\(\d+[ms]", txt):
        return False
    # In bypass mode "esc to interrupt" is always shown; the reliable
    # idle indicator is "shift+tab to cycle" (bypass idle) or "← for agents".
    if "shift+tab to cycle" in txt or "← for agents" in txt:
        return True
    # Fallback: bare ❯ prompt line with no spinner above it.
    return bool(re.search(r"^❯", txt, re.MULTILINE))


# A deployment that renames its agents can still hold heartbeat files under the
# old name: this table falls back to them when the file under the current name is
# missing. Format: "old:new,old2:new2". Empty by default.
HEARTBEAT_ALIASES = {
    k.strip(): v.strip()
    for k, _, v in (p.partition(":") for p in os.environ.get("MESH_HEARTBEAT_ALIASES", "").split(","))
    if k.strip() and v.strip()
}


def _heartbeat_path(agent: str) -> "Path | None":
    """Path to the agent's heartbeat: the file named after its id first, then the
    file named after a known alias. None when neither exists."""
    p = HEARTBEATS_DIR / f"{agent}.json"
    if p.exists():
        return p
    alias = HEARTBEAT_ALIASES.get(agent)
    if alias:
        ap = HEARTBEATS_DIR / f"{alias}.json"
        if ap.exists():
            return ap
    return None


def _remote_heartbeat(agent: str) -> tuple[bool, bool, str | None, str | None]:
    hb_path = _heartbeat_path(agent)
    if hb_path is None:
        return False, False, None, None
    try:
        data = json.loads(hb_path.read_text())
        ts = data.get("ts", "")
        last_activity = data.get("last_activity") or None
        dt = datetime.fromisoformat(ts)
        if datetime.now(timezone.utc) - dt > timedelta(minutes=5):
            return False, False, ts, last_activity
        return True, data.get("idle", True), ts, last_activity
    except Exception:
        return False, False, None, None


def _phone_reading() -> dict:
    """The phone monitor's latest reading, from the log named by MESH_PHONE_LOG.

    Returns {reachable, last_seen, battery_pct, battery_temp_c, plugged, status,
    health}. `reachable` means the reading is fresh (under 3 minutes) and is not
    an UNREACHABLE line.
    """
    out = {"reachable": False, "last_seen": None, "battery_pct": None,
           "battery_temp_c": None, "plugged": None, "status": None, "health": None,
           "disk_pct": None, "load_1m": None, "cores": None}
    try:
        with PHONE_LOG.open() as fh:
            last = ""
            for line in fh:
                if line.strip():
                    last = line.strip()
    except Exception:
        return out
    if not last:
        return out

    ts_str, _, rest = last.partition(" ")
    out["last_seen"] = ts_str
    try:
        dt = datetime.fromisoformat(ts_str)
        fresh = datetime.now(timezone.utc) - dt < timedelta(minutes=3)
    except Exception:
        fresh = False

    if "UNREACHABLE" in rest or not fresh:
        return out

    kv = {}
    for part in rest.split():
        if "=" in part:
            k, v = part.split("=", 1)
            kv[k] = v

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    pct = _f(kv.get("pct"))
    disk = _f(kv.get("disk_pct"))
    cores = _f(kv.get("cores"))
    out.update({
        "reachable": True,
        "battery_pct": int(pct) if pct is not None else None,
        "battery_temp_c": _f(kv.get("temp")),
        "plugged": kv.get("plug"),
        "status": kv.get("status"),
        "health": kv.get("health"),
        "disk_pct": int(disk) if disk is not None else None,
        "load_1m": _f(kv.get("load")),
        "cores": int(cores) if cores is not None else None,
    })
    return out


def _count_tickets(agent: str) -> tuple[int, int]:
    if not TICKETS_DIR.exists():
        return 0, 0
    pending = 0
    running = 0
    for state in ("draft", "armed", "blocked", "queued"):
        d = TICKETS_DIR / agent / state
        if d.exists():
            pending += len(list(d.glob("*.json")))
    run_dir = TICKETS_DIR / agent / "running"
    if run_dir.exists():
        running = len(list(run_dir.glob("*.json")))
    return pending, running


def _daemon_active(agent: str) -> bool:
    """Presence of an agent that runs as a service unit.
    Same mechanism as get_services(): systemctl --user is-active <agent>.service."""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", f"{agent}.service"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _ondemand_standby(agent: str) -> bool:
    """True when `agent` is an on-demand agent the daemon has put to sleep.

    The evidence is a sentinel at $MESH_HOME/state/ondemand-asleep/<agent> that
    exists AND is fresh (mtime under ONDEMAND_SENTINEL_MAX_AGE_S). A stale
    sentinel means the daemon itself is dead, so we do not claim "sleeping" —
    the agent is then simply offline, which is the honest answer."""
    if agent not in ONDEMAND_AGENTS:
        return False
    f = ONDEMAND_ASLEEP_DIR / agent
    try:
        age = datetime.now(timezone.utc).timestamp() - f.stat().st_mtime
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return age < ONDEMAND_SENTINEL_MAX_AGE_S


def get_agent_status(agent: str) -> dict:
    if agent in DAEMON_AGENTS:
        # A service-run agent: no tmux, no heartbeat. Presence is simply whether
        # the unit is active. idle stays True: it is a standby, and it only really
        # works when it takes over from the primary provider.
        pending, running = _count_tickets(agent)
        return {"alive": _daemon_active(agent), "idle": True,
                "last_activity": None, "last_heartbeat": None,
                "tickets_pending": pending, "tickets_running": running}
    if agent in PHONE_AGENTS:
        # No tmux and no heartbeat, so presence comes from the phone monitor.
        # 'idle' stays True: we know the phone answers, not whether the session on
        # it is working.
        r = _phone_reading()
        pending, running = _count_tickets(agent)
        return {"alive": r["reachable"], "idle": True,
                "last_heartbeat": r["last_seen"], "last_activity": r["last_seen"],
                "tickets_pending": pending, "tickets_running": running}
    if agent in REMOTE_AGENTS:
        alive, idle, ts, last_activity = _remote_heartbeat(agent)
        pending, running = _count_tickets(agent)
        return {"alive": alive, "idle": idle, "last_heartbeat": ts,
                "last_activity": last_activity,
                "tickets_pending": pending, "tickets_running": running}
    alive = _tmux_session_alive(agent)
    idle = _tmux_is_idle(agent) if alive else False
    # An on-demand agent that is offline but asleep by the daemon's own doing is
    # "standby, wakes on a message" — a different thing from plainly offline. Only
    # meaningful when NOT alive.
    standby = _ondemand_standby(agent) if not alive else False
    pending, running = _count_tickets(agent)
    return {"alive": alive, "idle": idle, "standby": standby, "last_activity": None,
            "tickets_pending": pending, "tickets_running": running}


def _http_service_status(url: str) -> str:
    """Liveness of a service that answers HTTP rather than systemd.

    Some things worth watching are containers, not units. The URLs come from
    the deployment (``MESH_WATCHED_HTTP``): this used to probe one specific
    container on localhost and publish its state in ``/status`` for every
    installation — a request every caller paid for, about a service they very
    likely do not run.
    """
    try:
        import urllib.request
        req = urllib.request.urlopen(url, timeout=2)
        return "active" if req.status == 200 else "inactive"
    except Exception:
        return "inactive"


def get_services() -> dict[str, str]:
    # Watched units: the mesh's own, plus whatever agents the configuration
    # declares (MESH_WATCHED_SERVICES, comma-separated).
    services = ["mesh-watcher", "mesh-api", "ticket-dispatcher"] + list(
        _env_agents("MESH_WATCHED_SERVICES")
    )
    result = {}
    for svc in services:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", svc],
            capture_output=True, text=True
        )
        result[svc] = r.stdout.strip() or "unknown"
    # name=url pairs, comma-separated: "n8n=http://localhost:5678/healthz"
    for entry in _env_agents("MESH_WATCHED_HTTP"):
        name, _, url = entry.partition("=")
        if name and url:
            result[name] = _http_service_status(url)
    return result


def get_host() -> dict:
    temp = None
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        temp = round(int(raw) / 1000, 1)
    except Exception:
        pass

    disk_pct = None
    try:
        usage = shutil.disk_usage("/")
        disk_pct = int(usage.used / usage.total * 100)
    except Exception:
        pass

    uptime_h = None
    try:
        with open("/proc/uptime") as fh:
            uptime_h = round(float(fh.read().split()[0]) / 3600, 1)
    except Exception:
        pass

    load_1m = None
    try:
        load_1m = round(os.getloadavg()[0], 2)
    except Exception:
        pass

    cores = None
    try:
        cores = os.cpu_count()
    except Exception:
        pass

    return {"cpu_temp_c": temp, "disk_used_pct": disk_pct,
            "uptime_h": uptime_h, "load_1m": load_1m, "cpu_cores": cores}


def _host_down_sentinel(host: str) -> dict | None:
    """The sentinel's contents when the host is marked intentionally down
    (mesh-host down <host>), otherwise None."""
    f = HOST_DOWN_DIR / host
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return {"reason": "down (intentional)"}


def _remote_host_metrics() -> dict:
    """Metrics for the remote host — the same fields as the local one: cpu_temp_c,
    disk_used_pct, uptime_h, load_1m.

    They are reported by the collector running on that host, which writes
    heartbeats/<host>-host.json over the same SSH transport as the agent
    heartbeats. Returns {} when the file is missing, stale (over 5 minutes) or
    unreadable — the card then falls back to plain on/off with no gauges. Only
    non-null fields are reported: CPU temperature, for instance, is often
    unavailable inside a virtualised environment."""
    p = HEARTBEATS_DIR / f"{host_key}-host.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        ts = data.get("ts")
        if ts:
            dt = datetime.fromisoformat(ts)
            if datetime.now(timezone.utc) - dt > timedelta(minutes=5):
                return {}
        return {k: data[k] for k in ("cpu_temp_c", "disk_used_pct",
                                     "uptime_h", "load_1m", "cpu_cores")
                if data.get(k) is not None}
    except Exception:
        return {}


def _remote_host() -> dict:
    """State of the remote host that carries the MESH_REMOTE_AGENTS.

    On/off comes from the host-down sentinel first, then from the freshness of
    those agents' heartbeats: any one of them being fresh proves the machine is
    up — some may be stopped while another runs. CPU, disk, uptime and load, when
    present, come from the remote collector (`heartbeats/<host>-host.json`).

    The host's display name comes from MESH_REMOTE_HOST_LABEL (default "remote").
    """
    host_key = os.environ.get("MESH_REMOTE_HOST_LABEL", "remote")
    label = host_key
    sentinel = _host_down_sentinel(host_key)
    if sentinel:
        return {"state": "offline_intentional", "label": label,
                "note": sentinel.get("reason"), "last_seen": sentinel.get("down_since")}

    latest = None
    for ag in REMOTE_AGENTS:
        hb = _heartbeat_path(ag)
        if hb is None:
            continue
        try:
            dt = datetime.fromisoformat(json.loads(hb.read_text()).get("ts"))
            if latest is None or dt > latest:
                latest = dt
        except Exception:
            continue

    last_seen = latest.isoformat() if latest else None
    if latest and datetime.now(timezone.utc) - latest < timedelta(minutes=5):
        return {"state": "online", "label": label, "last_seen": last_seen,
                **_remote_host_metrics()}
    return {"state": "offline", "label": label, "last_seen": last_seen}


def _phone_host() -> dict:
    """State of the monitored phone: battery, temperature and health, via the monitor."""
    label = os.environ.get("MESH_PHONE_LABEL", "phone")
    r = _phone_reading()
    state = "online" if r["reachable"] else "offline"
    return {"state": state, "label": label, "last_seen": r["last_seen"],
            "battery_pct": r["battery_pct"], "battery_temp_c": r["battery_temp_c"],
            "plugged": r["plugged"], "battery_status": r["status"],
            "battery_health": r["health"],
            "disk_used_pct": r["disk_pct"], "load_1m": r["load_1m"],
            "cpu_cores": r["cores"]}


def get_hosts() -> dict:
    """Health per physical machine of the mesh: local host, remote host, phone."""
    pi = get_host()
    pi.update({"state": "online", "label": os.environ.get("MESH_LOCAL_LABEL", "local host")})
    # These three keys are the dashboard's contract, and they are named for ROLES
    # rather than for the machines one deployment happens to own. They used to be
    # `pi` / `asus` / `pixel6a`: the operator's own hardware, in the payload of a
    # repository meant to be published — and worse, only `pi` matched what the
    # front end branches on, so the secondary and phone cards rendered EMPTY.
    # Naming them after the role fixes both at once. The labels stay
    # deployment-supplied (MESH_LOCAL_LABEL / MESH_PHONE_LABEL).
    return {"primary": pi, "secondary": _remote_host(), "mobile": _phone_host()}
