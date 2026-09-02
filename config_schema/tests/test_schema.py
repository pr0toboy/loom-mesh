"""Tests for config_schema.schema — Pydantic validation of mesh.toml."""
from __future__ import annotations

import os
import sys
import textwrap
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

# Ensure repo root is on the path regardless of how pytest is invoked
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config_schema.schema import load_config, MeshToml


# ── Helpers ──────────────────────────────────────────────────────────────────

def _write_toml(content: str) -> Path:
    """Write content to a temp .toml file and return its path."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".toml", delete=False, encoding="utf-8"
    )
    f.write(textwrap.dedent(content))
    f.close()
    return Path(f.name)


VALID_TOML = """
[mesh]
home = "~/mesh"
api_port = 8765

[hosts.primary]
user = "alice"
host = "localhost"

[[agents]]
name = "worker"
role = "does stuff"
model = "claude-sonnet-4-6"
workdir = "~/worker"
charter_template = "worker"
host = "primary"

[[agents]]
name = "supervisor"
role = "watches over"
model = "claude-haiku-4-5-20251001"
workdir = "~/supervisor"
charter_template = "supervisor"
host = "primary"
"""


# ── Valid config ─────────────────────────────────────────────────────────────

def test_valid_config_loads():
    p = _write_toml(VALID_TOML)
    cfg = load_config(p)
    assert cfg.mesh.api_port == 8765
    assert len(cfg.agents) == 2
    assert cfg.agents[0].name == "worker"


def test_defaults_applied():
    p = _write_toml("""
        [[agents]]
        name = "solo"
        workdir = "~/solo"
        charter_template = "worker"
        host = "primary"
    """)
    cfg = load_config(p)
    assert cfg.mesh.api_port == 8765
    assert cfg.mesh.api_bind == "127.0.0.1"
    assert cfg.agents[0].model == "claude-sonnet-4-6"
    assert cfg.agents[0].session_type == "tmux"


def test_example_toml_is_valid():
    """The bundled mesh.example.toml must pass validation."""
    example = REPO_ROOT / "config_schema" / "mesh.example.toml"
    assert example.exists(), "mesh.example.toml not found"
    cfg = load_config(example)
    assert len(cfg.agents) >= 1


def test_remote_host_accepted():
    p = _write_toml("""
        [mesh]
        home = "~/mesh"

        [hosts.primary]
        user = "alice"
        host = "localhost"

        [[hosts.remote]]
        name = "workstation"
        user = "bob"
        host = "100.100.100.100"
        wsl_distro = "Ubuntu"

        [[agents]]
        name = "remote-worker"
        workdir = "~/remote-worker"
        charter_template = "worker"
        host = "workstation"
    """)
    cfg = load_config(p)
    assert cfg.agents[0].host == "workstation"
    assert cfg.hosts.get_remote("workstation").wsl_distro == "Ubuntu"


def test_env_var_interpolation(monkeypatch):
    monkeypatch.setenv("MYHOST", "10.0.0.1")
    p = _write_toml("""
        [hosts.primary]
        user = "$USER"
        host = "$MYHOST"

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    cfg = load_config(p)
    assert cfg.hosts.primary.host == "10.0.0.1"
    assert cfg.hosts.primary.user == os.environ["USER"]


def test_api_port_range_valid():
    for port in [1024, 8080, 65535]:
        p = _write_toml(f"""
            [mesh]
            api_port = {port}

            [[agents]]
            name = "worker"
            workdir = "~/worker"
            charter_template = "worker"
            host = "primary"
        """)
        cfg = load_config(p)
        assert cfg.mesh.api_port == port


# ── Duplicate agent names ─────────────────────────────────────────────────────

def test_duplicate_agent_name_raises():
    p = _write_toml("""
        [[agents]]
        name = "alice"
        workdir = "~/alice"
        charter_template = "worker"
        host = "primary"

        [[agents]]
        name = "alice"
        workdir = "~/alice2"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="Duplicate agent name"):
        load_config(p)


# ── Unknown host ──────────────────────────────────────────────────────────────

def test_unknown_host_raises():
    p = _write_toml("""
        [hosts.primary]
        user = "alice"
        host = "localhost"

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "ghost-host"
    """)
    with pytest.raises(ValidationError, match="unknown host"):
        load_config(p)


# ── Charter template not found ────────────────────────────────────────────────

def test_missing_charter_template_raises():
    p = _write_toml("""
        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "nonexistent-template-xyz"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="not found in templates/charter"):
        load_config(p)


def test_absolute_charter_path_nonexistent_raises():
    p = _write_toml("""
        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "/tmp/this-does-not-exist-12345.md"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="does not exist"):
        load_config(p)


def test_absolute_charter_path_existing_ok(tmp_path):
    charter = tmp_path / "my-charter.md"
    charter.write_text("# My charter\n")
    p = _write_toml(f"""
        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "{charter}"
        host = "primary"
    """)
    cfg = load_config(p)
    assert cfg.agents[0].charter_template == str(charter)


# ── Agent name validation ─────────────────────────────────────────────────────

def test_invalid_agent_name_raises():
    for bad_name in ["Alice", "123agent", "a" * 33, "my agent", ""]:
        p = _write_toml(f"""
            [[agents]]
            name = "{bad_name}"
            workdir = "~/worker"
            charter_template = "worker"
            host = "primary"
        """)
        with pytest.raises(ValidationError):
            load_config(p)


def test_valid_agent_name_variants():
    for name in ["a", "alice", "alice-1", "helper-1", "agent99"]:
        p = _write_toml(f"""
            [[agents]]
            name = "{name}"
            workdir = "~/worker"
            charter_template = "worker"
            host = "primary"
        """)
        cfg = load_config(p)
        assert cfg.agents[0].name == name


# ── API port validation ───────────────────────────────────────────────────────

def test_api_port_too_low_raises():
    p = _write_toml("""
        [mesh]
        api_port = 80

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError):
        load_config(p)


def test_api_port_too_high_raises():
    p = _write_toml("""
        [mesh]
        api_port = 99999

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError):
        load_config(p)


# ── Hosts summary helper ──────────────────────────────────────────────────────

def test_hosts_all_names_includes_primary_and_remotes():
    p = _write_toml("""
        [[hosts.remote]]
        name = "ws1"
        user = "bob"
        host = "10.0.0.2"

        [[hosts.remote]]
        name = "ws2"
        user = "carol"
        host = "10.0.0.3"

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    cfg = load_config(p)
    assert cfg.hosts.all_names() == {"primary", "ws1", "ws2"}


# ── Path / distro injection hardening ────────────────────────────────────────

def test_workdir_with_quote_rejected():
    p = _write_toml("""
        [[agents]]
        name = "worker"
        workdir = "/tmp/foo'"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="invalid characters"):
        load_config(p)


def test_workdir_traversal_rejected():
    p = _write_toml("""
        [[agents]]
        name = "worker"
        workdir = "/home/x/../../etc"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="path traversal"):
        load_config(p)


def test_workdir_with_semicolon_rejected():
    p = _write_toml("""
        [[agents]]
        name = "worker"
        workdir = "/tmp/x;rm"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="invalid characters"):
        load_config(p)


def test_ssh_key_with_metachar_rejected():
    p = _write_toml("""
        [[hosts.remote]]
        name = "ws"
        user = "bob"
        host = "10.0.0.2"
        ssh_key = "~/.ssh/id$(whoami)"

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="invalid characters"):
        load_config(p)


def test_wsl_distro_with_space_rejected():
    p = _write_toml("""
        [[hosts.remote]]
        name = "ws"
        user = "bob"
        host = "10.0.0.2"
        wsl_distro = "Ubuntu; rm -rf /"

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="invalid characters"):
        load_config(p)


def test_valid_paths_accepted():
    for workdir in ["~/mesh", "/home/user/mesh", "/opt/mesh"]:
        p = _write_toml(f"""
            [[agents]]
            name = "worker"
            workdir = "{workdir}"
            charter_template = "worker"
            host = "primary"
        """)
        cfg = load_config(p)
        assert cfg.agents[0].workdir == workdir


def test_mesh_home_with_metachar_rejected():
    p = _write_toml("""
        [mesh]
        home = "/home/user/mesh;rm"

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="invalid characters"):
        load_config(p)


def test_valid_ssh_key_accepted():
    p = _write_toml("""
        [[hosts.remote]]
        name = "ws"
        user = "bob"
        host = "10.0.0.2"
        ssh_key = "~/.ssh/id_ed25519"

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    cfg = load_config(p)
    assert cfg.hosts.get_remote("ws").ssh_key == "~/.ssh/id_ed25519"


def test_valid_wsl_distro_variants_accepted():
    for distro in ["Ubuntu", "Debian-12", "openSUSE-Leap"]:
        p = _write_toml(f"""
            [[hosts.remote]]
            name = "ws"
            user = "bob"
            host = "10.0.0.2"
            wsl_distro = "{distro}"

            [[agents]]
            name = "worker"
            workdir = "~/worker"
            charter_template = "worker"
            host = "primary"
        """)
        cfg = load_config(p)
        assert cfg.hosts.get_remote("ws").wsl_distro == distro


# ── Model / role / user / host injection hardening ───────────────────────────

def test_model_with_metachar_rejected():
    p = _write_toml("""
        [[agents]]
        name = "worker"
        model = "claude-opus-4-7; touch /tmp/X"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="invalid model identifier"):
        load_config(p)


def test_model_valid_variants_accepted():
    for model in ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]:
        p = _write_toml(f"""
            [[agents]]
            name = "worker"
            model = "{model}"
            workdir = "~/worker"
            charter_template = "worker"
            host = "primary"
        """)
        cfg = load_config(p)
        assert cfg.agents[0].model == model


def test_role_with_newline_rejected():
    p = _write_toml("""
        [[agents]]
        name = "worker"
        role = "X\\nMALICIOUS"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="single line"):
        load_config(p)


def test_role_valid_accepted():
    p = _write_toml("""
        [[agents]]
        name = "worker"
        role = "Curates the knowledge base"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    cfg = load_config(p)
    assert cfg.agents[0].role == "Curates the knowledge base"


def test_user_with_metachar_rejected():
    p = _write_toml("""
        [[hosts.remote]]
        name = "ws"
        user = "alice;rm"
        host = "10.0.0.2"

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="invalid characters"):
        load_config(p)


def test_host_with_metachar_rejected():
    p = _write_toml("""
        [[hosts.remote]]
        name = "ws"
        user = "bob"
        host = "localhost;rm"

        [[agents]]
        name = "worker"
        workdir = "~/worker"
        charter_template = "worker"
        host = "primary"
    """)
    with pytest.raises(ValidationError, match="invalid characters"):
        load_config(p)
