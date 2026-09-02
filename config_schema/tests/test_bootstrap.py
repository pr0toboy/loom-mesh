"""Tests for bootstrap.sh — dry-run and E2E file-generation tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap.sh"


def _write_toml(content: str, directory: Path) -> Path:
    p = directory / "mesh.toml"
    p.write_text(textwrap.dedent(content))
    return p


# ── Dry-run tests ─────────────────────────────────────────────────────────────

class TestDryRun:
    """bootstrap.sh --dry-run must print what it would do and write nothing."""

    def _run(self, toml_content: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_toml(toml_content, Path(tmp))
            cmd = [str(BOOTSTRAP), "--dry-run", "--no-systemd", "--skip-health", str(p)]
            if extra_args:
                cmd.extend(extra_args)
            return subprocess.run(cmd, capture_output=True, text=True)

    def test_dry_run_exits_zero(self):
        r = self._run("""
            [[agents]]
            name = "alice"
            workdir = "~/alice"
            charter_template = "worker"
            host = "primary"
        """)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_dry_run_emits_dry_markers(self):
        r = self._run("""
            [[agents]]
            name = "alice"
            workdir = "~/alice"
            charter_template = "worker"
            host = "primary"
        """)
        assert "[DRY]" in r.stdout

    def test_dry_run_writes_nothing(self, tmp_path):
        toml = _write_toml("""
            [mesh]
            home = "{tmp}/mesh"

            [[agents]]
            name = "alice"
            workdir = "{tmp}/alice"
            charter_template = "worker"
            host = "primary"
        """.replace("{tmp}", str(tmp_path)), tmp_path)
        before = set(tmp_path.rglob("*"))
        subprocess.run(
            [str(BOOTSTRAP), "--dry-run", "--no-systemd", "--skip-health", str(toml)],
            capture_output=True,
        )
        after = set(tmp_path.rglob("*"))
        # Only the mesh.toml we wrote should exist; nothing else created
        new_files = after - before
        assert not new_files, f"Dry-run wrote unexpected files: {new_files}"

    def test_dry_run_invalid_config_exits_nonzero(self):
        r = self._run("""
            [[agents]]
            name = "Alice"   # invalid: uppercase
            workdir = "~/alice"
            charter_template = "worker"
            host = "primary"
        """)
        assert r.returncode != 0

    def test_dry_run_missing_config_exits_nonzero(self):
        r = subprocess.run(
            [str(BOOTSTRAP), "--dry-run", "--no-systemd", "/tmp/no-such-file-12345.toml"],
            capture_output=True, text=True,
        )
        assert r.returncode != 0


# ── E2E file-generation test ──────────────────────────────────────────────────

class TestE2EGeneration:
    """Deploy a 2-agent mesh into /tmp and verify generated files."""

    TOML = """
        [mesh]
        home    = "{mesh_home}"
        api_port = 9876

        [[agents]]
        name             = "alpha"
        role             = "Test agent alpha"
        model            = "claude-sonnet-4-6"
        workdir          = "{workdir_alpha}"
        charter_template = "worker"
        host             = "primary"

        [[agents]]
        name             = "beta"
        role             = "Test agent beta"
        model            = "claude-haiku-4-5-20251001"
        workdir          = "{workdir_beta}"
        charter_template = "supervisor"
        host             = "primary"
    """

    def _deploy(self, tmp: Path) -> subprocess.CompletedProcess:
        mesh_home = tmp / "mesh"
        workdir_alpha = tmp / "alpha"
        workdir_beta  = tmp / "beta"
        toml = _write_toml(
            self.TOML.format(
                mesh_home=str(mesh_home),
                workdir_alpha=str(workdir_alpha),
                workdir_beta=str(workdir_beta),
            ),
            tmp,
        )
        return subprocess.run(
            [str(BOOTSTRAP), "--no-systemd", "--no-ssh", "--skip-health", str(toml)],
            capture_output=True, text=True,
        ), mesh_home, workdir_alpha, workdir_beta

    def test_e2e_exits_zero(self, tmp_path):
        r, *_ = self._deploy(tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_peers_py_generated(self, tmp_path):
        r, mesh_home, *_ = self._deploy(tmp_path)
        assert r.returncode == 0
        peers_py = mesh_home / "peers.py"
        assert peers_py.exists(), "peers.py not generated"
        content = peers_py.read_text()
        assert "alpha" in content
        assert "beta" in content

    def test_peers_sh_generated(self, tmp_path):
        r, mesh_home, *_ = self._deploy(tmp_path)
        assert r.returncode == 0
        peers_sh = mesh_home / "peers.sh"
        assert peers_sh.exists(), "peers.sh not generated"

    def test_api_tokens_generated(self, tmp_path):
        r, mesh_home, *_ = self._deploy(tmp_path)
        assert r.returncode == 0
        tokens_path = mesh_home / "api-tokens.json"
        assert tokens_path.exists(), "api-tokens.json not generated"
        tokens = json.loads(tokens_path.read_text())
        assert len(tokens) >= 1
        assert len(tokens[0]["token"]) >= 32  # 64 hex chars from secrets.token_hex(32)

    def test_api_tokens_idempotent(self, tmp_path):
        """Running bootstrap twice must not regenerate tokens."""
        r, mesh_home, *_ = self._deploy(tmp_path)
        assert r.returncode == 0
        tokens_before = (mesh_home / "api-tokens.json").read_text()
        # Run again
        self._deploy(tmp_path)
        tokens_after = (mesh_home / "api-tokens.json").read_text()
        assert tokens_before == tokens_after, "api-tokens.json was overwritten on second run"

    def test_workspace_dirs_created(self, tmp_path):
        r, _, workdir_alpha, workdir_beta = self._deploy(tmp_path)
        assert r.returncode == 0
        assert workdir_alpha.is_dir(), f"{workdir_alpha} not created"
        assert workdir_beta.is_dir(), f"{workdir_beta} not created"

    def test_claude_md_rendered(self, tmp_path):
        r, _, workdir_alpha, workdir_beta = self._deploy(tmp_path)
        assert r.returncode == 0
        for wdir in [workdir_alpha, workdir_beta]:
            claude_md = wdir / "CLAUDE.md"
            assert claude_md.exists(), f"{claude_md} not rendered"
            content = claude_md.read_text()
            assert "Charter" in content

    def test_claude_md_not_overwritten(self, tmp_path):
        """Pre-existing CLAUDE.md must be preserved."""
        mesh_home = tmp_path / "mesh"
        workdir_alpha = tmp_path / "alpha"
        workdir_alpha.mkdir(parents=True)
        (workdir_alpha / "CLAUDE.md").write_text("# My custom charter\n")

        toml = _write_toml(
            self.TOML.format(
                mesh_home=str(mesh_home),
                workdir_alpha=str(workdir_alpha),
                workdir_beta=str(tmp_path / "beta"),
            ),
            tmp_path,
        )
        subprocess.run(
            [str(BOOTSTRAP), "--no-systemd", "--no-ssh", "--skip-health", str(toml)],
            capture_output=True,
        )
        content = (workdir_alpha / "CLAUDE.md").read_text()
        assert content == "# My custom charter\n", "CLAUDE.md was overwritten"

    def test_api_tokens_chmod_0600(self, tmp_path):
        """api-tokens.json must be created with mode 0600."""
        r, mesh_home, *_ = self._deploy(tmp_path)
        assert r.returncode == 0
        tokens_path = mesh_home / "api-tokens.json"
        assert tokens_path.exists()
        mode = tokens_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0600 but got {oct(mode)}"

    def test_bootstrap_workdir_special_chars_safe(self, tmp_path):
        """A workdir with shell metacharacters must be rejected at validation."""
        for bad_workdir in ["/tmp/foo'bar", "/tmp/x;rm", "/tmp/$(whoami)"]:
            toml = _write_toml(f"""
                [mesh]
                home = "{tmp_path}/mesh"

                [[agents]]
                name = "worker"
                workdir = "{bad_workdir}"
                charter_template = "worker"
                host = "primary"
            """, tmp_path)
            r = subprocess.run(
                [str(BOOTSTRAP), "--no-systemd", "--no-ssh", "--skip-health", str(toml)],
                capture_output=True, text=True,
            )
            assert r.returncode != 0, (
                f"Bootstrap should have rejected workdir {bad_workdir!r} but exited 0"
            )

    def test_duplicate_agent_bootstrap_fails(self, tmp_path):
        toml = _write_toml("""
            [mesh]
            home = "{tmp}/mesh"

            [[agents]]
            name = "dupe"
            workdir = "{tmp}/dupe1"
            charter_template = "worker"
            host = "primary"

            [[agents]]
            name = "dupe"
            workdir = "{tmp}/dupe2"
            charter_template = "worker"
            host = "primary"
        """.replace("{tmp}", str(tmp_path)), tmp_path)
        r = subprocess.run(
            [str(BOOTSTRAP), "--no-systemd", "--no-ssh", "--skip-health", str(toml)],
            capture_output=True, text=True,
        )
        assert r.returncode != 0
