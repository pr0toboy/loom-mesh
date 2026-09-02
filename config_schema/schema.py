"""Pydantic schema + loader for mesh.toml configuration files.

Usage:
    from config_schema.schema import load_config

    cfg = load_config("mesh.toml")          # raises ValidationError on bad input
    print(cfg.mesh.api_port)
    print(cfg.agents[0].name)
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator, Field

# ── Allowed models (open-ended: extra strings pass as-is) ───────────────────────
KNOWN_MODELS: set[str] = {
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
}

# Pattern for valid agent names
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# Safety patterns for path-valued and distro-valued fields
_PATH_RE = re.compile(r"^[~/][a-zA-Z0-9_./\-]+$")
_DISTRO_RE = re.compile(r"^[a-zA-Z0-9_\-.]+$")
_MODEL_RE = re.compile(r"^[a-z][a-z0-9.\-]{1,63}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-]+$")


def _check_path(v: str, label: str) -> str:
    """Reject shell metacharacters and path traversal in path-valued fields."""
    if not _PATH_RE.match(v):
        raise ValueError(f"{label} contains invalid characters: {v!r}")
    if ".." in v:
        raise ValueError(f"{label} must not contain path traversal (..): {v!r}")
    return v


# ── Sub-models ──────────────────────────────────────────────────────────────────

class MeshConfig(BaseModel):
    home: str = "~/mesh"
    api_port: int = Field(default=8765, ge=1024, le=65535)
    api_bind: str = "127.0.0.1"
    log_dir: Optional[str] = None

    @field_validator("home", "log_dir", mode="before")
    @classmethod
    def _expand(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = _interpolate(v)
        _check_path(v, "mesh.home/log_dir")
        return v

    @property
    def home_path(self) -> Path:
        return Path(self.home).expanduser()

    @property
    def log_dir_path(self) -> Path:
        if self.log_dir:
            return Path(self.log_dir).expanduser()
        return self.home_path / "logs"


class PrimaryHost(BaseModel):
    user: str = Field(default_factory=lambda: os.environ.get("USER", "user"))
    host: str = "localhost"
    ssh_key: Optional[str] = None

    @field_validator("user", mode="before")
    @classmethod
    def _expand_user(cls, v: str) -> str:
        v = _interpolate(v)
        if not _USER_RE.match(v):
            raise ValueError(f"user contains invalid characters: {v!r}")
        return v

    @field_validator("host", mode="before")
    @classmethod
    def _expand_host(cls, v: str) -> str:
        v = _interpolate(v)
        if not _HOST_RE.match(v):
            raise ValueError(f"host contains invalid characters: {v!r}")
        return v

    @field_validator("ssh_key", mode="before")
    @classmethod
    def _validate_ssh_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = _interpolate(v)
        _check_path(v, "ssh_key")
        return v


class RemoteHost(BaseModel):
    name: str
    user: str
    host: str
    ssh_key: Optional[str] = None
    wsl_distro: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"Remote host name {v!r} must match [a-z][a-z0-9-]{{0,31}}"
            )
        return v

    @field_validator("user", mode="before")
    @classmethod
    def _expand_user(cls, v: str) -> str:
        v = _interpolate(v)
        if not _USER_RE.match(v):
            raise ValueError(f"user contains invalid characters: {v!r}")
        return v

    @field_validator("host", mode="before")
    @classmethod
    def _expand_host(cls, v: str) -> str:
        v = _interpolate(v)
        if not _HOST_RE.match(v):
            raise ValueError(f"host contains invalid characters: {v!r}")
        return v

    @field_validator("ssh_key", mode="before")
    @classmethod
    def _validate_ssh_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = _interpolate(v)
        _check_path(v, "ssh_key")
        return v

    @field_validator("wsl_distro", mode="before")
    @classmethod
    def _validate_wsl_distro(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _DISTRO_RE.match(v):
            raise ValueError(f"wsl_distro contains invalid characters: {v!r}")
        return v


class Hosts(BaseModel):
    primary: PrimaryHost = Field(default_factory=PrimaryHost)
    remote: list[RemoteHost] = Field(default_factory=list)

    def all_names(self) -> set[str]:
        """Return all valid host names (primary + remote names)."""
        names = {"primary"}
        names.update(h.name for h in self.remote)
        return names

    def get_remote(self, name: str) -> Optional[RemoteHost]:
        for h in self.remote:
            if h.name == name:
                return h
        return None


class Agent(BaseModel):
    name: str
    role: str = ""
    model: str = "claude-sonnet-4-6"
    workdir: str
    charter_template: str = "worker"
    host: str = "primary"
    session_type: Literal["tmux"] = "tmux"
    wsl_distro: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"Agent name {v!r} must match [a-z][a-z0-9-]{{0,31}}"
            )
        return v

    @field_validator("model", mode="before")
    @classmethod
    def _validate_model(cls, v: str) -> str:
        if not _MODEL_RE.match(v):
            raise ValueError(f"invalid model identifier: {v!r}")
        return v

    @field_validator("role", mode="before")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if "\n" in v or "\r" in v or len(v) > 200:
            raise ValueError(f"role must be a single line ≤ 200 chars: {v!r}")
        return v

    @field_validator("workdir", mode="before")
    @classmethod
    def _expand_workdir(cls, v: str) -> str:
        v = _interpolate(v)
        _check_path(v, "workdir")
        return v

    @field_validator("wsl_distro", mode="before")
    @classmethod
    def _validate_wsl_distro(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _DISTRO_RE.match(v):
            raise ValueError(f"wsl_distro contains invalid characters: {v!r}")
        return v

    @property
    def workdir_path(self) -> Path:
        return Path(self.workdir).expanduser()


# ── Root config ─────────────────────────────────────────────────────────────────

class MeshToml(BaseModel):
    mesh: MeshConfig = Field(default_factory=MeshConfig)
    hosts: Hosts = Field(default_factory=Hosts)
    agents: list[Agent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_validate(self) -> "MeshToml":
        self._check_unique_agent_names()
        self._check_agent_hosts()
        self._check_charter_templates()
        return self

    def _check_unique_agent_names(self) -> None:
        seen: set[str] = set()
        for agent in self.agents:
            if agent.name in seen:
                raise ValueError(f"Duplicate agent name: {agent.name!r}")
            seen.add(agent.name)

    def _check_agent_hosts(self) -> None:
        valid = self.hosts.all_names()
        for agent in self.agents:
            if agent.host not in valid:
                raise ValueError(
                    f"Agent {agent.name!r} references unknown host {agent.host!r}. "
                    f"Valid hosts: {sorted(valid)}"
                )

    def _check_charter_templates(self) -> None:
        repo_root = _repo_root()
        for agent in self.agents:
            tpl = agent.charter_template
            # Absolute path → must exist
            if tpl.startswith("/") or tpl.startswith("~"):
                p = Path(tpl).expanduser()
                if not p.exists():
                    raise ValueError(
                        f"Agent {agent.name!r}: charter_template {tpl!r} "
                        f"is an absolute path but does not exist"
                    )
            else:
                # Template name → must exist as templates/charter/<name>.md
                candidates = [
                    repo_root / "templates" / "charter" / f"{tpl}.md",
                    repo_root / "templates" / "charter" / tpl,
                ]
                if not any(c.exists() for c in candidates):
                    raise ValueError(
                        f"Agent {agent.name!r}: charter_template {tpl!r} "
                        f"not found in templates/charter/ "
                        f"(looked for {candidates[0]} and {candidates[1]})"
                    )


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _interpolate(value: str) -> str:
    """Expand $VAR references using os.environ. Unknown vars are left as-is."""
    def _replace(m: re.Match) -> str:
        var = m.group(1)
        return os.environ.get(var, m.group(0))
    return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", _replace, value)


def _repo_root() -> Path:
    """Best-effort: walk up from this file until we find a README.md or .git."""
    here = Path(__file__).resolve().parent
    for candidate in [here, here.parent, here.parent.parent]:
        if (candidate / "README.md").exists() or (candidate / ".git").exists():
            return candidate
    return here.parent  # fallback


# ── Public API ──────────────────────────────────────────────────────────────────

def load_config(path: str | Path) -> MeshToml:
    """Load and validate a mesh.toml file. Raises ValidationError on failure."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return MeshToml.model_validate(raw)
