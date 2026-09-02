"""A mesh that is not the installed one may not drive the SHARED tmux server.

Three components type into panes and end with Enter — the bus watcher, the
ticket dispatcher, night_reconcile — and each carries its own copy of the same
predicate. Every copy used to accept *any* declared server::

    if os.environ.get("MESH_TMUX"): return True

`MESH_TMUX=tmux` is such a declaration, and it names the shared default server:
the one holding the operator's agents, in a deployment where the sessions ARE
named after them. Measured on 2026-09-09, before the fix::

    MESH_HOME=/tmp/bench-mesh MESH_TMUX=tmux   ->  accepted

That is a bench cleared to type a command into a live agent's pane and press
Enter. The refusal message each component prints had always described the
correct rule — "a mesh home that is not the default one, driving the default
tmux server, is an inconsistency" — the code applied a weaker one.

Sibling of `test_no_default_socket.py`, which stops a *test* from calling tmux
without a socket. This one is about what the *shipped code* agrees to do, and it
runs each guard rather than reading it: the hole was invisible to review for two
gates in a row.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEFAULT_HOME = os.path.expanduser("~/mesh")
BENCH_HOME = "/tmp/loom-bench-mesh-that-does-not-exist"

#: (mesh home, MESH_TMUX, extra env, expected verdict, why)
CASES = [
    (BENCH_HOME,   "tmux",          {}, False,
     "a bench mesh aimed at the shared server is the injection case itself"),
    (BENCH_HOME,   "",              {}, False,
     "no declaration at all, from a non-default home"),
    (BENCH_HOME,   "tmux -L bench", {}, True,
     "a private server holds no agent of the operator's"),
    (DEFAULT_HOME, "tmux",          {}, True,
     "the installed mesh at its default home may use the shared server"),
    (BENCH_HOME,   "tmux",          {"MESH_ALLOW_SHARED_TMUX": "1"}, True,
     "an install elsewhere, saying so out loud — the units bootstrap writes carry this"),
    # A first attempt at this guard asked only "is there a -L?", which refused
    # every MESH_TMUX pointing at something that is not tmux — including the
    # stub the bus tests drive the watcher with. Substituting the program is as
    # deliberate as naming a socket, and the shared server is not what it
    # reaches.
    (BENCH_HOME,   "bash /tmp/fake-tmux.sh", {}, True,
     "a substituted tmux command reaches no shared server"),
    (BENCH_HOME,   "/usr/bin/tmux",  {}, False,
     "an absolute path to the real binary is still the shared server"),
]
IDS = [f"{'default' if h == DEFAULT_HOME else 'bench'}-{t or 'unset'}"
       f"{'-allowed' if e else ''}" for h, t, e, _, _ in CASES]


def _env(home: str, tmux: str, extra: dict) -> dict:
    env = {**os.environ, "MESH_HOME": home, **extra}
    env.pop("MESH_ALLOW_SHARED_TMUX", None)
    env.update(extra)
    if tmux:
        env["MESH_TMUX"] = tmux
    else:
        env.pop("MESH_TMUX", None)
    return env


def _shell_guard(env: dict) -> bool:
    r = subprocess.run(
        ["bash", "-c",
         f'source "{REPO}/bus/watcher.sh" >/dev/null 2>&1; _tmux_scope_is_consistent'],
        env=env, capture_output=True)
    return r.returncode == 0


def _python_guard(module_dir: str, module: str, env: dict) -> bool:
    r = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(REPO / module_dir)!r}); "
         f"import {module} as m; print(int(m._tmux_scope_is_consistent()))"],
        env=env, capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, f"{module} could not be probed: {r.stderr[-600:]}"
    return r.stdout.strip().endswith("1")


GUARDS = {
    "bus/watcher.sh": lambda env: _shell_guard(env),
    "ticket_dispatcher": lambda env: _python_guard("mesh-api", "ticket_dispatcher", env),
    "night_reconcile": lambda env: _python_guard(".", "autonomy.night_reconcile", env),
}


@pytest.mark.parametrize("component", list(GUARDS))
@pytest.mark.parametrize("home,tmux,extra,expected,why", CASES, ids=IDS)
def test_guard_verdict(component, home, tmux, extra, expected, why):
    verdict = GUARDS[component](_env(home, tmux, extra))
    assert verdict is expected, (
        f"{component}: MESH_HOME={home} MESH_TMUX={tmux or '<unset>'} "
        f"{extra or ''} -> {'accepted' if verdict else 'refused'}, "
        f"expected {'accepted' if expected else 'refused'} — {why}"
    )


def test_the_shipped_units_carry_the_opt_out():
    """Whatever the guard refuses, a real installation must still work.

    The refusal only spares a bench because the units bootstrap.sh installs say
    they are the installation. If that line goes missing from a template, every
    deployment whose mesh home is not ~/mesh stops delivering — silently, since
    the watcher logs the refusal and stays active.

    Judged on the PARSED directives, not on a substring: a commented-out line, or
    a later ``=0`` overriding an earlier ``=1``, both satisfy `in text` while the
    unit does the opposite. Raised by the 2026-09-09 gate against the substring
    version of this very test.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from test_systemd_units import service_directives   # noqa: E402

    for name in ("mesh-watcher.service", "ticket-dispatcher.service", "mesh-api.service"):
        text = (REPO / "templates" / "systemd" / name).read_text()
        # Resolved the way systemd resolves it, not by scanning for a line. The
        # re-gate proved three ways past a simpler reading: an empty
        # `Environment=` RESETS the whole list, `UnsetEnvironment=` removes the
        # variable, and the perfectly valid quoted form was read as absent.
        value = None
        for key, raw in service_directives(text):
            if key == "Environment":
                if not raw.strip():                     # `Environment=` alone: reset
                    value = None
                    continue
                for token in shlex.split(raw):          # strips the quoted form
                    if token.startswith("MESH_ALLOW_SHARED_TMUX="):
                        value = token.split("=", 1)[1]
            elif key == "UnsetEnvironment" and "MESH_ALLOW_SHARED_TMUX" in raw.split():
                value = None
        assert value == "1", (
            f"{name} does not declare itself an installation (effective value: {value!r}): "
            "a mesh home outside ~/mesh would be refused the shared tmux server and "
            "deliver nothing, while the unit stays active"
        )


# ── The SSH branch, which had no guard at all ────────────────────────────────
#
# Found by the 2026-09-09 gate, outside every guard I had written: I hardened
# push_local and left push_remote open. The command sent over SSH names no
# socket, so it always reaches the remote host's shared server, and it ends with
# Enter. The test drives the real function with a stub `ssh`, and checks the
# stub was never even invoked — a return code alone would not prove that nothing
# left the machine.

_STUB_SSH = """#!/bin/sh
echo "$@" >> "$SSH_STUB_LOG"
exit 0
"""


def _push_remote_attempt(tmp_path, home: str, extra: dict) -> tuple[int, str]:
    """Run push_remote for real; return (rc, what the stub ssh received)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "ssh"
    stub.write_text(_STUB_SSH)
    stub.chmod(0o755)
    log = tmp_path / "ssh-calls.log"
    log.write_text("")
    env = {**os.environ, "MESH_HOME": home, "SSH_STUB_LOG": str(log),
           "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("MESH_ALLOW_SHARED_TMUX", None)
    env.update(extra)
    r = subprocess.run(
        ["bash", "-c",
         f'source "{REPO}/bus/watcher.sh" >/dev/null 2>&1; '
         'push_remote user@host agent-session "a notice"'],
        env=env, capture_output=True, text=True)
    return r.returncode, log.read_text()


def test_ssh_push_from_a_bench_never_reaches_the_remote_host(tmp_path):
    rc, calls = _push_remote_attempt(tmp_path, BENCH_HOME, {})
    assert rc != 0, "push_remote accepted a mesh home that is not the installed one"
    assert calls == "", (
        "ssh was invoked from a bench mesh: the remote command ends with Enter, so "
        f"this ran in a remote agent's pane. Stub received:\n{calls}"
    )


def test_ssh_push_is_allowed_once_the_deployment_says_so(tmp_path):
    """The guard must not break a real install whose mesh home is elsewhere."""
    rc, calls = _push_remote_attempt(tmp_path, BENCH_HOME, {"MESH_ALLOW_SHARED_TMUX": "1"})
    assert "user@host" in calls, (
        "an installed mesh outside ~/mesh can no longer push to its remote agents; "
        f"rc={rc}, stub received:\n{calls!r}"
    )


# ── The same deployment, spelled three ways ──────────────────────────────────
#
# `_same_path` resolves both sides before comparing, so a trailing slash or a
# symlinked mesh home is still the same installation. Nothing tested that, and
# the failure would be silent in the worst way: a real deployment refused the
# shared server, staying `active` while delivering nothing. Raised as T3 by the
# 2026-09-09 gate.

@pytest.mark.parametrize("component", list(GUARDS))
@pytest.mark.parametrize("spelling", ["plain", "trailing-slash", "symlink"])
def test_the_default_home_is_recognised_however_it_is_spelled(tmp_path, spelling, component):
    home = tmp_path / "home"
    (home / "mesh").mkdir(parents=True)
    if spelling == "symlink":
        (home / "link-to-mesh").symlink_to(home / "mesh")
    # Built as a STRING, not through Path: pathlib normalises a trailing slash
    # away, so the "trailing-slash" case was silently identical to "plain" — a
    # parametrisation that names a case it does not exercise.
    mesh_home = {
        "plain": str(home / "mesh"),
        "trailing-slash": str(home / "mesh") + "/",
        "symlink": str(home / "link-to-mesh"),
    }[spelling]

    env = {**os.environ, "HOME": str(home), "MESH_HOME": mesh_home, "MESH_TMUX": "tmux"}
    env.pop("MESH_ALLOW_SHARED_TMUX", None)
    # All three implementations, not just the shell one: the re-gate pointed out
    # that a textual comparison in the two Python copies stayed green here.
    assert GUARDS[component](env), (
        f"{component}: the default mesh home written as {spelling!r} was not recognised: this "
        "deployment would be refused the shared tmux server and deliver nothing, "
        "while every service stays active"
    )


# ── The dispatcher's SSH guard, which nothing tested ─────────────────────────
#
# The 2026-09-09 re-gate found three mutants green here — `return True`, the
# guard deleted, the LOCAL predicate used in its place — because the dispatcher
# test suite mocks `_push_remote` out. Guarding a path and testing the guard are
# two different pieces of work, and I had done the first one twice.

_DISPATCH_PROBE = """
import json, os, sys
sys.path.insert(0, {api!r})
import ticket_dispatcher as td
ok = td._push_remote("user@host", "agent-session", "a notice")
print(json.dumps({{"ok": bool(ok)}}))
"""


def _dispatch_remote_attempt(tmp_path, home: str, extra: dict) -> tuple[bool, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "ssh"
    stub.write_text(_STUB_SSH)
    stub.chmod(0o755)
    log = tmp_path / "ssh-calls.log"
    log.write_text("")
    env = {**os.environ, "MESH_HOME": home, "SSH_STUB_LOG": str(log),
           "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("MESH_ALLOW_SHARED_TMUX", None)
    env.update(extra)
    r = subprocess.run(
        [sys.executable, "-c", _DISPATCH_PROBE.format(api=str(REPO / "mesh-api"))],
        env=env, capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, f"could not probe the dispatcher: {r.stderr[-600:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])["ok"], log.read_text()


def test_dispatcher_ssh_push_from_a_bench_never_reaches_the_remote_host(tmp_path):
    ok, calls = _dispatch_remote_attempt(tmp_path, BENCH_HOME, {})
    assert ok is False, "the dispatcher pushed a ticket from a mesh that is not the installed one"
    assert calls == "", (
        "ssh was invoked from a bench mesh: the remote script ends with Enter, so this "
        f"ran a ticket in a remote agent's pane. Stub received:\n{calls}"
    )


def test_dispatcher_ssh_push_is_allowed_once_the_deployment_says_so(tmp_path):
    ok, calls = _dispatch_remote_attempt(tmp_path, BENCH_HOME, {"MESH_ALLOW_SHARED_TMUX": "1"})
    assert ok and "user@host" in calls, (
        f"an installed mesh outside ~/mesh can no longer dispatch to its remote agents; "
        f"ok={ok}, stub received:\n{calls!r}"
    )


# ── The case that tells the two predicates apart ─────────────────────────────
#
# A bench with a PRIVATE local socket: the local predicate says "private server,
# always fine" — true for send-keys on this machine, false for SSH, where the
# remote command names no socket and lands on the remote shared server. Without
# this case, swapping _remote_scope_is_consistent for the local one passes every
# other test: measured in the 2026-09-09 re-gate, that mutant survived.

@pytest.mark.parametrize("component,attempt", [
    ("bus/watcher.sh", _push_remote_attempt),
    ("ticket_dispatcher", _dispatch_remote_attempt),
])
def test_a_private_local_socket_does_not_licence_an_ssh_push(tmp_path, component, attempt):
    result, calls = attempt(tmp_path, BENCH_HOME, {"MESH_TMUX": "tmux -L bench"})
    refused = (result != 0) if component.endswith(".sh") else (result is False)
    assert refused and calls == "", (
        f"{component}: a private LOCAL socket was taken as licence to push over SSH. "
        "The remote command names no socket, so it reaches the remote shared server "
        f"where that host's agents live. Stub received:\n{calls}"
    )


def test_reading_a_remote_pane_is_also_reaching_out(tmp_path):
    """`capture_remote` is a read, and it still opens an SSH connection.

    It used to run BEFORE any scope check: a clone contacted the operator's
    remote machine, and only the write that followed was refused. Raised as R7 by
    the 2026-09-09 re-gate. Guarding the write and leaving the read open means
    the contact happens anyway.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "ssh").write_text(_STUB_SSH)
    (bindir / "ssh").chmod(0o755)
    log = tmp_path / "ssh-calls.log"
    log.write_text("")
    env = {**os.environ, "MESH_HOME": BENCH_HOME, "SSH_STUB_LOG": str(log),
           "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("MESH_ALLOW_SHARED_TMUX", None)
    subprocess.run(
        ["bash", "-c",
         f'source "{REPO}/bus/watcher.sh" >/dev/null 2>&1; '
         'capture_remote user@host agent-session'],
        env=env, capture_output=True, text=True)
    assert log.read_text() == "", (
        "a bench mesh opened an SSH connection to the remote host just to read a "
        f"pane. Stub received:\n{log.read_text()}"
    )
