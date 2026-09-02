"""Who exists on this mesh — loaded from the deployment, never hardcoded.

Three files can answer that question, and they are tried in this order:

1. ``$MESH_HOME/mesh_roster.py`` — generated from a richer registry (the shape a
   grown deployment ends up with: display names, hosts, tiers). It exposes the
   peer sets directly.
2. ``$MESH_HOME/peers.py`` — what ``bootstrap.sh`` writes on a fresh install. It
   only knows ``AGENTS``, so the facades come from the environment.
3. Nothing — an empty roster. The bus then refuses every send with "unknown
   peer" instead of guessing, which is the safe direction: a message written to
   an inbox is executed by the agent reading it.

The sets are deliberately separate. ``INBOX_PEERS`` are endpoints with a file to
read; ``FACADES`` are humans reached through a chat client, who never run
``read.py`` and must be answered *through the bus* rather than on a terminal;
``SEND_PEERS`` is the union — every valid ``from``/``to``.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

MESH_HOME = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))


def _load_module(path: Path):
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"_loom_{path.stem}", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _from_env(var: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get(var, "").split(",") if x.strip())


def _resolve() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (agents, facades)."""
    roster = _load_module(MESH_HOME / "mesh_roster.py")
    if roster is not None:
        agents = tuple(getattr(roster, "INBOX_PEERS", None) or getattr(roster, "REAL", ()))
        facades = tuple(getattr(roster, "FACADE_PEERS", ()) or _from_env("MESH_HUMAN_FACADES"))
        return agents, facades

    peers = _load_module(MESH_HOME / "peers.py")
    if peers is not None:
        # bootstrap.sh writes PILOT_PEERS there: the endpoints a human reaches
        # the mesh through (a browser, a chat bridge). They send and receive but
        # run no agent, so they belong in the facades, not in the agents.
        facades = tuple(getattr(peers, "PILOT_PEERS", ())) or _from_env("MESH_HUMAN_FACADES")
        return tuple(getattr(peers, "AGENTS", ())), facades

    return (), _from_env("MESH_HUMAN_FACADES")


AGENTS, FACADES = _resolve()
#: Every valid endpoint of a message, whichever side it is on.
SEND_PEERS: frozenset[str] = frozenset(AGENTS) | frozenset(FACADES)
#: Endpoints that own an inbox file and can run ``read.py <self>``.
INBOX_PEERS: frozenset[str] = frozenset(AGENTS)
#: Humans behind a chat client: they read elsewhere, so a reply must be sent.
FACADE_PEERS: frozenset[str] = frozenset(FACADES)
