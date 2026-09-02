#!/usr/bin/env python3
"""CLI validator for mesh.toml config files.

Usage:
    python3 validate.py mesh.toml
    python3 validate.py mesh.toml --json        # machine-readable output
    python3 validate.py mesh.example.toml --summary

Exit codes:
    0  valid
    1  validation error
    2  file not found or parse error
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a script from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_schema.schema import load_config
from pydantic import ValidationError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a mesh.toml configuration file."
    )
    parser.add_argument("config", help="Path to mesh.toml")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    parser.add_argument("--summary", action="store_true", help="Print agent/host summary")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        _err(args.json, f"File not found: {config_path}")
        return 2

    try:
        cfg = load_config(config_path)
    except ValidationError as exc:
        if args.json:
            print(json.dumps({"valid": False, "errors": exc.errors()}, indent=2))
        else:
            print(f"✗ Validation failed: {config_path}", file=sys.stderr)
            for err in exc.errors():
                loc = " → ".join(str(x) for x in err["loc"]) if err["loc"] else "(root)"
                print(f"  [{loc}] {err['msg']}", file=sys.stderr)
        return 1
    except Exception as exc:
        _err(args.json, f"Parse error: {exc}")
        return 2

    if args.json:
        summary = {
            "valid": True,
            "mesh": {
                "home": cfg.mesh.home,
                "api_port": cfg.mesh.api_port,
                "api_bind": cfg.mesh.api_bind,
            },
            "hosts": {
                "primary": cfg.hosts.primary.host,
                "remote": [h.name for h in cfg.hosts.remote],
            },
            "agents": [
                {
                    "name": a.name,
                    "model": a.model,
                    "host": a.host,
                    "workdir": a.workdir,
                }
                for a in cfg.agents
            ],
        }
        print(json.dumps(summary, indent=2))
    elif args.summary:
        print(f"✓ {config_path} is valid")
        print(f"\n  mesh home : {cfg.mesh.home}")
        print(f"  api       : {cfg.mesh.api_bind}:{cfg.mesh.api_port}")
        print(f"\n  hosts:")
        print(f"    primary  : {cfg.hosts.primary.user}@{cfg.hosts.primary.host}")
        for h in cfg.hosts.remote:
            wsl = f" (WSL: {h.wsl_distro})" if h.wsl_distro else ""
            print(f"    {h.name:<10}: {h.user}@{h.host}{wsl}")
        print(f"\n  agents ({len(cfg.agents)}):")
        for a in cfg.agents:
            print(f"    {a.name:<15} model={a.model:<30} host={a.host}")
    else:
        print(f"✓ {config_path} is valid  "
              f"({len(cfg.agents)} agents, "
              f"{1 + len(cfg.hosts.remote)} hosts)")

    return 0


def _err(as_json: bool, msg: str) -> None:
    if as_json:
        print(json.dumps({"valid": False, "errors": [{"msg": msg}]}))
    else:
        print(f"✗ {msg}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
