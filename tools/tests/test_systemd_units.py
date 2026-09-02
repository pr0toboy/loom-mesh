"""The shipped systemd units must actually let the services work.

`bootstrap.sh` installs and enables these units, so whatever they contain is
what a new deployment runs. They carried `PrivateTmp=true` and
`ProtectHome=read-only` — hardening that reads well in review and had never been
executed. `PrivateTmp` hides the tmux socket, so the watcher has no pane to push
into and the API reports every agent as down; `ProtectHome=read-only` makes the
mesh home unwritable, so the watcher logs "Read-only file system" and delivers
nothing. Both services start, stay `active`, and do nothing.

That is why the second test here launches the real watcher under the real
options instead of reading the file: a unit file is only correct if the service
under it can still do its job.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

#: The shell these tests drive with send-keys. `--norc` skips the operator's
#: .bashrc — it does NOT touch HISTFILE, which stays ~/.bash_history, *theirs*.
#: An interactive bash appends its history on exit, so every line typed into a
#: test pane landed in the operator's own history file, on a private socket, with
#: nothing ever written to their terminal. Measured 2026-09-09, three times over
#: two days, 29 lines; found from their `history` output, not from ours. The
#: channel was a FILE, which is why every tmux measurement came back clean.
HARNESS_SHELL = "env HISTFILE=/dev/null bash --norc -i"

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "templates" / "systemd"
INFRA = ["mesh-watcher.service", "mesh-api.service", "ticket-dispatcher.service"]


def service_directives(text: str) -> list[tuple[str, str]]:
    """The ``[Service]`` section as (key, value) pairs, in file order."""
    out, in_service = [], False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_service = line == "[Service]"
            continue
        if in_service and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out.append((key.strip(), value.strip()))
    return out


def render(unit: str, mesh: Path) -> str:
    """The unit as ``bootstrap.sh`` renders it, for a mesh under ``mesh``.

    The placeholders are the ones bootstrap substitutes for the infrastructure
    units. Any other ``{{...}}`` left standing fails the test rather than being
    passed to systemd verbatim: a new placeholder must be handled here too, or
    the dynamic test below would silently run a unit that is not the shipped one.
    """
    subs = {
        "{{mesh.home}}": str(mesh),
        "{{mesh.log_dir}}": str(mesh / "logs"),
        # What the deployment declares as its tmux server. The test overrides it
        # below with a private socket; here it only has to render.
        "{{mesh.tmux}}": "tmux",
        "{{mesh.api_port}}": "8765",
        "{{mesh.api_bind}}": "127.0.0.1",
        "{{mesh_api_base}}": str(REPO / "mesh-api"),
        "{{mesh_api_python}}": "python3",
        "{{mesh_api_uvicorn}}": "python3 -m uvicorn",
    }
    text = (TEMPLATES / unit).read_text()
    for k, v in subs.items():
        text = text.replace(k, v)
    assert "{{" not in text, (
        f"{unit}: unrendered placeholder — add it to render() so the dynamic "
        f"test runs the unit the installer actually writes: "
        f"{[l for l in text.splitlines() if '{{' in l]}"
    )
    return text


def directives(unit: str) -> str:
    """The unit's actual directives, comments stripped.

    The comments explain which options were removed and why, and name them —
    so a check on the raw text flags the explanation as if it were the defect.
    """
    return "\n".join(l for l in (TEMPLATES / unit).read_text().splitlines()
                      if not l.lstrip().startswith("#"))


#: Option families that hide the two directories every service here depends on:
#: /tmp (the tmux socket lives in /tmp/tmux-<uid>) and ~ (the mesh home). Named
#: by family rather than by value, because the defect came back under a
#: different spelling: `ProtectHome=read-only` was removed, and
#: `ProtectHome=tmpfs` or `TemporaryFileSystem=/tmp` blind the service exactly
#: the same way while passing a check that looks for the old string.
BLINDING = {
    "PrivateTmp": lambda v: v.lower() in ("true", "yes", "on", "1", "disconnected"),
    "ProtectHome": lambda v: v.lower() not in ("false", "no", "off", "0"),
    "TemporaryFileSystem": lambda v: True,
    "InaccessiblePaths": lambda v: True,
    "PrivateUsers": lambda v: v.lower() in ("true", "yes", "on", "1", "full"),
}


#: Directives the transient unit below must not inherit from the template.
#: ``ExecStart`` becomes the command line; ``Type`` is what systemd-run already
#: does; ``Restart`` would resurrect the unit while the test tears it down; and
#: ``EnvironmentFile`` points at the operator's own
#: ``~/.config/systemd/user/mesh.env`` — loading it would point the test watcher
#: at the live mesh home. Everything else is passed through verbatim, including
#: options added to the template after this test was written.
NOT_INHERITED = {"ExecStart", "Type", "Restart", "RestartSec", "EnvironmentFile"}


@pytest.mark.parametrize("unit", INFRA)
def test_no_sandbox_option_that_blinds_the_service(unit):
    for key, value in service_directives(directives(unit)):
        blinds = BLINDING.get(key)
        assert not (blinds and blinds(value)), (
            f"{unit}: {key}={value} hides /tmp/tmux-<uid> or the mesh home under ~, "
            f"so the service starts, stays active, and delivers nothing"
        )


@pytest.mark.parametrize("unit", INFRA)
def test_read_only_system_still_grants_the_paths_the_service_writes(unit):
    text = directives(unit)
    if "ProtectSystem=strict" not in text:
        pytest.skip(f"{unit} does not use ProtectSystem=strict")
    assert "ReadWritePaths=" in text, (
        f"{unit}: ProtectSystem=strict mounts everything read-only; without "
        f"ReadWritePaths the service cannot write its own state")
    assert "{{mesh.home}}" in text.split("ReadWritePaths=", 1)[1].splitlines()[0], (
        f"{unit}: the mesh home must be writable")


@pytest.mark.parametrize("unit", INFRA)
def test_every_infra_unit_renders_with_no_placeholder_left(unit, tmp_path):
    """render() must know every placeholder bootstrap.sh substitutes.

    Only the watcher goes through the dynamic test below, so a placeholder
    added to one of the other two units would otherwise be discovered by a
    deployment rather than here — as a literal ``{{...}}`` in an installed unit.
    """
    render(unit, tmp_path / "mesh")


from _systemd import user_systemd_available as _systemd_available  # noqa: E402


@pytest.mark.skipif(not _systemd_available(), reason="no user systemd (CI container)")
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")
def test_the_watcher_still_delivers_under_the_units_own_hardening():
    """Run the real watcher with the real options and check a message lands.

    This is the test that would have caught the defect: every static check on
    the unit passed while the service delivered nothing.

    The mesh home goes under ``~`` rather than under pytest's tmp_path, because
    that is where ``bootstrap.sh`` puts it and because the option that started
    all this — ``ProtectHome`` — only hides ``~``. Run from ``/tmp``, this test
    stays green with ``ProtectHome=tmpfs`` in the template while a real
    deployment delivers nothing: the harness has to live where the thing under
    test lives. It is removed again in the ``finally`` below.
    """
    root = Path.home() / ".cache" / f"loom-mesh-unit-test-{os.getpid()}"
    mesh = root / "mesh"
    socket = f"loomunit-{os.getpid()}"
    tmux = ["tmux", "-L", socket]
    unit = f"loom-test-watcher-{os.getpid()}"
    try:
        _run_watcher_under_its_own_unit(mesh, socket, tmux, unit)
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)
        subprocess.run([*tmux, "kill-server"], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


def _run_watcher_under_its_own_unit(mesh, socket, tmux, unit):
    (mesh / "logs").mkdir(parents=True)
    (mesh / "mesh_roster.py").write_text(
        'INBOX_PEERS = ("alice", "bob")\nFACADE_PEERS = ()\n')
    for f in (REPO / "bus").glob("*"):
        if f.is_file():
            shutil.copy2(f, mesh / f.name)

    # The options come from the template, not from a list written here. A test
    # that restates its own hardening proves nothing about the file that ships:
    # with the options hardcoded, adding `ProtectHome=tmpfs` — or
    # `TemporaryFileSystem=/tmp`, which blinds the watcher exactly as
    # `PrivateTmp` did — left this test green while the service delivered
    # nothing.
    hardening: list[str] = []
    execstart = ""
    for key, value in service_directives(render("mesh-watcher.service", mesh)):
        if key == "ExecStart":
            execstart = value
        elif key == "Environment" and value.startswith("MESH_TMUX="):
            # The unit declares the shared default server. This test must not
            # touch it, so the -E below points the watcher at its own socket;
            # what matters here is that the unit DOES declare one.
            continue
        elif key not in NOT_INHERITED:
            hardening += ["-p", f"{key}={value}"]
    assert execstart, "mesh-watcher.service has no ExecStart to run"
    subprocess.run([*tmux, "new-session", "-d", "-s", "bob", HARNESS_SHELL],
                   capture_output=True, check=True)
    subprocess.run(
        ["systemd-run", "--user", f"--unit={unit}",
         *hardening,
         "-E", f"MESH_HOME={mesh}",
         "-E", f"MESH_TMUX=tmux -L {socket}",
         *shlex.split(execstart)],
        capture_output=True, text=True, check=True)
    time.sleep(3)

    subprocess.run(["python3", str(mesh / "send.py"), "alice", "bob", "normal",
                    "delivered under hardening"],
                   env={**os.environ, "MESH_HOME": str(mesh)},
                   capture_output=True, check=True)
    deadline = time.time() + 20
    log = mesh / "logs" / "watcher.log"
    while time.time() < deadline:
        if log.exists() and "push local OK" in log.read_text():
            break
        time.sleep(1)

    assert log.exists(), "the watcher could not even write its log under these options"
    text = log.read_text()
    assert "Read-only file system" not in text, text
    assert "push local OK" in text, (
        f"the watcher started but delivered nothing under the shipped hardening:\n{text}")

    # A log line is not the effect. `ReadOnlyPaths={{mesh.home}}` added to the
    # template left this test green: the watcher still logged "push local OK"
    # while its own stderr said "Read-only file system" and the de-duplication
    # marker was never written — so every message would be re-typed forever.
    # (systemd resolves ReadOnly over ReadWrite for the same path, verified with
    # systemd-run.) Check what the service PRODUCES, on every writable path it
    # depends on.
    stderr_log = mesh / "logs" / "watcher.stderr.log"
    if stderr_log.exists():
        assert "Read-only file system" not in stderr_log.read_text(), (
            "the watcher could not write somewhere it needs to:\n"
            + stderr_log.read_text()[-800:])
    markers = list(mesh.glob(".watcher-pushed-*"))
    assert markers, (
        "the de-duplication marker was never written, so the same message would be "
        "typed again on every inotify event — the mesh home is not actually writable "
        f"under these options. Log said:\n{text}")
