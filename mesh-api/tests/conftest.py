"""Isolate the suite from the machine it runs on: a throwaway MESH_HOME.

Every module under ``mesh_api`` resolves ``$MESH_HOME`` **at import time**
(inboxes, tickets, roster, tokens, usage snapshots). With the variable unset it
falls back to ``~/mesh`` — the live installation. Two consequences, both real:

* the suite read (and could write) the operator's own bus: ``test_inbox_all_agents``
  was listing real inboxes, so its result depended on who had sent what that day;
* peer validation loaded the operator's generated roster, so *which names the API
  accepts* was a property of the host, not of the test. Once the roster stopped
  being committed, the same tests started returning 422 everywhere — the failure
  was in the fixture, not in the routes.

So the roster the tests validate against is written here, with neutral names, and
every path lands in a temp directory that goes away with the run.
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="loom-mesh-tests-"))
os.environ["MESH_HOME"] = str(_TMP)

# And a tmux server of our own, for the same reason the mesh home is a temp dir.
# Several modules shell out to tmux to decide whether an agent is alive. With no
# socket named, they ask the DEVELOPER's tmux server about sessions called
# "alice" or "bob" — so a contributor who happens to run a session by that name
# gets different results from the suite, and the runs are not comparable.
# Reading someone else's server is harmless in itself; depending on it is not.
os.environ.setdefault("MESH_TMUX", f"tmux -L loom-tests-{os.getpid()}")

# The roster is normally produced from the deployment's registry. The suite
# ships its own so it never depends on a deployed mesh.
(_TMP / "mesh_roster.py").write_text(
    'REAL = ("alice", "bob", "yara", "dave", "zoe", "auto")\n'
    'INBOX_PEERS = REAL\n'
    'FACADE_PEERS = ("user-web", "pilot-matrix")\n'
    'ALL_PEERS = REAL + FACADE_PEERS\n'
    'SEND_PEERS = ALL_PEERS\n'
)

# Install the bus the way a deployment does. The API shells out to
# ``$MESH_HOME/mesh-send-checked.sh`` and ``read.py``: with no bus installed,
# every write route answered 502 and the suite proved nothing about them. Using
# the repository's own bus here also makes these tests exercise the real write
# path — lock, fsync, roster validation — instead of a mock of it.
_BUS_SRC = Path(__file__).resolve().parents[2] / "bus"
for _f in _BUS_SRC.glob("*"):
    if _f.is_file():
        shutil.copy2(_f, _TMP / _f.name)

atexit.register(shutil.rmtree, _TMP, True)
