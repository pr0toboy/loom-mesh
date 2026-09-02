"""The default sender must be validated like any explicit one.

`models.py` carries `Field(DEFAULT_SENDER, alias="from", validate_default=True)`.
Without that flag pydantic does not run the validator on a default, so the API
accepts a sender it would refuse if the client had typed it — and the value then
reaches the bus, which answers 502 with "unknown from peer". That is the defect
this repository shipped: a `POST /send` with no `from` failed on every freshly
bootstrapped mesh.

The integration test next door posts without `from` and expects 200. It proves
the default is *a peer the installer writes* — it does NOT prove the default is
*validated*: removing `validate_default` leaves it green, because `user-web` is
valid on that mesh either way. So this test does the other half, on a roster
that does not know the default: the refusal must happen at the door (422), with
a message naming what the mesh does know, rather than several layers down.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PROBE = r'''
import json, os, sys
sys.path.insert(0, os.environ["MESH_API_SRC"])
from mesh_api.peers import ALL_PEERS, DEFAULT_SENDER
from mesh_api import models

out = {"roster": sorted(ALL_PEERS), "default": DEFAULT_SENDER, "results": {}}
for name in ("SendRequest", "TicketCreateRequest", "BulkTicketCreateRequest"):
    cls = getattr(models, name)
    kw = {"to": "builder"}
    if name == "SendRequest":
        kw["body"] = "x"
    elif name == "TicketCreateRequest":
        kw["prompt"] = "x"
    else:
        kw["tickets"] = [{"prompt": "x"}]
    try:
        obj = cls(**kw)
        out["results"][name] = {"accepted": True, "from": obj.from_}
    except Exception as exc:
        out["results"][name] = {"accepted": False, "error": str(exc)}
print(json.dumps(out))
'''


def _probe_with_roster(tmp_path: Path, agents: str, facades: str) -> dict:
    """Build a bootstrap-shaped mesh home, then ask the models what they accept."""
    mesh = tmp_path / "mesh"
    mesh.mkdir(parents=True, exist_ok=True)
    (mesh / "peers.py").write_text(
        f"AGENTS = {{{agents}}}\nPILOT_PEERS = {{{facades}}}\n"
        "ALL_PEERS = sorted(AGENTS | PILOT_PEERS)\n")
    # bootstrap.sh copies the bus roster module into the mesh home; the API reads it.
    shutil.copy2(REPO / "bus" / "roster.py", mesh / "roster.py")

    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    env = {**os.environ,
           "MESH_HOME": str(mesh),
           "MESH_API_SRC": str(REPO / "mesh-api")}
    r = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 0, f"probe failed:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_roster_without_the_default_sender_refuses_at_the_door(tmp_path):
    out = _probe_with_roster(tmp_path, '"builder"', '"chat-front"')
    assert out["default"] not in out["roster"], "fixture is wrong: the default IS in the roster"

    for name, res in out["results"].items():
        assert not res["accepted"], (
            f"{name} accepted a default sender the roster does not know "
            f"({out['default']!r}); it would reach the bus and come back as a 502. "
            f"This is what validate_default=True prevents."
        )
        assert out["default"] in res["error"] and "builder" in res["error"], (
            f"{name}: the error must name the rejected value and what the mesh does "
            f"know, or the operator has nothing to act on: {res['error']!r}")


def test_a_roster_with_the_default_sender_accepts_it(tmp_path):
    """Positive control: a validator that rejects everything would pass the test above."""
    out = _probe_with_roster(tmp_path, '"builder", "ops"', '"user-web", "pilot-matrix"')
    assert out["default"] in out["roster"], "fixture is wrong: the default is missing"

    for name, res in out["results"].items():
        assert res["accepted"], (
            f"{name} refused the default sender on a mesh that knows it: {res.get('error')!r}")
        assert res["from"] == out["default"]
