"""bootstrap.sh must not drive a systemd unit that belongs to another mesh.

`systemctl --user` reaches the user manager over D-Bus, and that manager resolves
unit names from its own environment — not from the caller's `$HOME`. So a
bootstrap run inside a sandbox writes `tmux-server.service` into the sandbox,
where the manager never looks, and then enables *the live deployment's* unit of
the same name. `tmux-server.service` stops by running `tmux kill-server`, so
acting on the wrong one takes down every agent session on the machine: on
2026-09-03 the fleet was down for 3h30 after exactly that.

Nothing in the suite covered the step where a name becomes a running service.
These tests install a harmless probe unit in the real user directory and ask the
guard the question bootstrap now asks before enabling anything.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "bootstrap.sh"
USER_UNITS = Path.home() / ".config" / "systemd" / "user"


from _systemd import user_systemd_available as _systemd_available  # noqa: E402


pytestmark = pytest.mark.skipif(not _systemd_available(),
                                reason="no user systemd (CI container)")


def _ask_guard(unit: str, unit_dir: Path) -> int:
    """Run bootstrap's own guard, sourced, without running an installation."""
    # The arguments travel in the environment, not as positionals: bootstrap.sh
    # parses its own options with `shift`, so sourcing it consumes $1 and $2 and
    # the call would fail with "unbound variable" — an rc of 127 that every
    # "must refuse" assertion would happily accept.
    return subprocess.run(
        ["bash", "-c",
         f'source "{BOOTSTRAP}" >/dev/null 2>&1; _unit_is_ours "$PROBE_UNIT" "$PROBE_DIR"'],
        capture_output=True, text=True,
        env={**os.environ, "PROBE_UNIT": unit, "PROBE_DIR": str(unit_dir)},
    ).returncode


@pytest.fixture()
def probe_unit():
    """A unit that does nothing, installed where the manager really looks."""
    name = f"loom-ownership-probe-{os.getpid()}.service"
    path = USER_UNITS / name
    USER_UNITS.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Unit]\nDescription=Loom ownership probe (test, does nothing)\n"
        "[Service]\nType=oneshot\nExecStart=/bin/true\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    try:
        yield name
    finally:
        path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def test_a_unit_installed_elsewhere_is_refused(probe_unit, tmp_path):
    """The sandbox case: our file is in tmp, the manager runs the real one."""
    (tmp_path / probe_unit).write_text("[Service]\nExecStart=/bin/true\n")
    assert _ask_guard(probe_unit, tmp_path) != 0, (
        "the guard accepted a unit the manager resolves to another directory — "
        "this is the path that took the fleet down on 2026-09-03"
    )


def test_our_own_unit_is_accepted(probe_unit):
    """The control: without it, a guard that always refuses would pass above."""
    assert _ask_guard(probe_unit, USER_UNITS) == 0, (
        "the guard refused a unit the manager resolves to exactly the file we wrote; "
        "bootstrap would never enable anything"
    )


def test_a_unit_the_manager_cannot_see_is_refused(tmp_path):
    name = f"loom-absent-{os.getpid()}.service"
    (tmp_path / name).write_text("[Service]\nExecStart=/bin/true\n")
    assert _ask_guard(name, tmp_path) != 0, (
        "a unit the manager does not know cannot be enabled; the guard must say so "
        "rather than let systemctl fail with a bare exit code"
    )
