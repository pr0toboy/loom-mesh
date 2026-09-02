"""Who exists on this mesh, as the API sees it.

There is exactly one place that answers this question — ``bus/roster.py``, the
module the bus itself uses — and this file loads it rather than reimplementing
it. That is not tidiness: the API used to read *only* ``$MESH_HOME/mesh_roster.py``
while ``bootstrap.sh`` writes ``$MESH_HOME/peers.py``, so a freshly installed
mesh had an API that knew no agents at all. ``GET /status`` returned an empty
roster, every ``POST /send`` answered 422 "unknown peer", and the log advised
running a generator that does not ship with the project. The installation
guide's step 7 was dead on arrival, and no test saw it because the test suite
writes its own roster file.

Load order, and why:

1. ``$MESH_HOME/roster.py`` — what ``bootstrap.sh`` installs. This is the
   deployed case, and it is first because a deployment's own copy must win over
   whatever checkout happens to sit next to it.
2. ``<checkout>/bus/roster.py`` — running the API straight from a clone, before
   anything is installed.
3. Nothing — an empty roster. The API still starts and still serves; routes that
   validate a peer answer as they would for an unknown one. A missing roster is
   a deployment that is not finished, not a reason to refuse to boot.
"""
from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

MESH_HOME = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
_CHECKOUT_BUS = Path(__file__).resolve().parents[2] / "bus" / "roster.py"


def _load_module(path: Path):
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"_loom_roster_{path.parent.name}", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        log.warning("roster module %s unreadable (%s) — ignored", path, exc)
        return None
    return mod


def _resolve():
    for candidate in (MESH_HOME / "roster.py", _CHECKOUT_BUS):
        mod = _load_module(candidate)
        if mod is None:
            continue
        agents = frozenset(getattr(mod, "INBOX_PEERS", ()) or ())
        peers = frozenset(getattr(mod, "SEND_PEERS", ()) or ()) | agents
        if agents or peers:
            return agents, peers
        # A roster module that resolves to nothing means the mesh home has no
        # peer file yet — say so once, with the path, instead of leaving an
        # empty API to be diagnosed from 422s.
        log.warning("roster at %s resolves to no peers: run bootstrap.sh so it "
                    "writes %s/peers.py (or mesh_roster.py)", candidate, MESH_HOME)
        return agents, peers

    log.warning("no roster module found (looked in %s and %s): the API knows no "
                "peers, so every peer-validating route will answer 422",
                MESH_HOME / "roster.py", _CHECKOUT_BUS)
    return frozenset(), frozenset()


AGENTS, ALL_PEERS = _resolve()

#: Sender assumed when a request carries no ``from``. It is the web UI's peer
#: name, because that is the only client that posts without one — ``bootstrap.sh``
#: writes it into ``PILOT_PEERS`` ("the webui POSTs as user-web"). A deployment
#: whose roster names its facade otherwise must send ``from`` explicitly: the
#: default is validated like any explicit value, so it is refused at the door
#: (422) instead of reaching the bus as a peer nobody knows (502).
DEFAULT_SENDER = "user-web"
