"""The API refuses to start with no auth on a network-reachable address.

Neither half is a defect on its own — the bypass is a deliberate convenience,
and binding to a VPN address is how the dashboard is reached from a phone. Put
together they publish every agent's inbox to whatever can route to the port,
and nothing about the running service looks wrong. That combination is reached
by copying a bind address from an example, not by deciding to.
"""
from __future__ import annotations

import pytest

from mesh_api.main import check_startup_safety


def test_loopback_with_no_auth_is_allowed(monkeypatch):
    monkeypatch.setenv("MESH_API_NO_AUTH", "1")
    monkeypatch.setenv("MESH_API_BIND", "127.0.0.1")
    check_startup_safety()          # the case the bypass was designed for


def test_open_bind_with_auth_is_allowed(monkeypatch):
    monkeypatch.delenv("MESH_API_NO_AUTH", raising=False)
    monkeypatch.setenv("MESH_API_BIND", "0.0.0.0")
    check_startup_safety()          # tokens are doing their job


# RFC 5737 documentation addresses: they stand for "a real address on a real
# network" without ever being one. Writing a plausible LAN or VPN address here
# would also trip the guard that keeps operators' own addresses out of the repo,
# and an exception carved for a test is an exception that outlives it.
@pytest.mark.parametrize("addr", ["0.0.0.0", "192.0.2.10", "198.51.100.1", "::"])
def test_open_bind_without_auth_refuses_to_start(monkeypatch, addr):
    monkeypatch.setenv("MESH_API_NO_AUTH", "1")
    monkeypatch.setenv("MESH_API_BIND", addr)
    with pytest.raises(RuntimeError) as exc:
        check_startup_safety()
    assert "refusing to start" in str(exc.value)
    assert "MESH_API_ALLOW_OPEN_BIND" in str(exc.value), "say how to override it"


def test_the_override_is_honoured(monkeypatch):
    monkeypatch.setenv("MESH_API_NO_AUTH", "1")
    monkeypatch.setenv("MESH_API_BIND", "0.0.0.0")
    monkeypatch.setenv("MESH_API_ALLOW_OPEN_BIND", "1")
    check_startup_safety()


def test_an_unknown_bind_is_assumed_private(monkeypatch):
    """Guessing 'open' would refuse to start a service that is actually private."""
    monkeypatch.setenv("MESH_API_NO_AUTH", "1")
    monkeypatch.delenv("MESH_API_BIND", raising=False)
    check_startup_safety()
