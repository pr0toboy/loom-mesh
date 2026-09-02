"""Route /api/hooks — the Claude Code hooks this machine has configured.

Reads the `hooks` section of ~/.claude/settings.json (SessionStart, Stop,
PreToolUse, …) and, for each hook, reports the script it runs plus a one-line
description taken from the script's docstring or first comment. Feeds
/ui/hooks.html.
"""
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends

from ..auth import require_auth

router = APIRouter()

SETTINGS = Path(os.environ.get("CLAUDE_SETTINGS", os.path.expanduser("~/.claude/settings.json")))

_EVENT_ORDER = {
    "SessionStart": 0, "UserPromptSubmit": 1, "PreToolUse": 2, "PostToolUse": 3,
    "Stop": 4, "SubagentStop": 5, "PreCompact": 6, "Notification": 7,
}


def _script_and_desc(cmd: str):
    script = ""
    for tok in cmd.split():
        if tok.endswith(".py") or tok.endswith(".sh"):
            script = tok
            break
    desc = ""
    if script and Path(script).exists():
        try:
            raw = Path(script).read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw = ""
        m = re.search(r'"""(.+?)"""', raw, re.S)
        if m:
            desc = next((l.strip() for l in m.group(1).strip().splitlines() if l.strip()), "")
        else:
            for line in raw.splitlines():
                s = line.strip()
                if not s or s.startswith("#!"):
                    continue
                if s.startswith("#"):
                    desc = s.lstrip("#").strip()
                    if desc:
                        break
                else:
                    break
    return script, desc


@router.get("/api/hooks")
async def list_hooks(_: str = Depends(require_auth)):
    try:
        d = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    hooks = []
    for event, groups in (d.get("hooks") or {}).items():
        for grp in groups or []:
            matcher = grp.get("matcher")
            for hk in grp.get("hooks", []) or []:
                cmd = hk.get("command", "")
                script, desc = _script_and_desc(cmd)
                hooks.append({
                    "event": event,
                    "matcher": matcher,
                    "command": cmd,
                    "timeout": hk.get("timeout"),
                    "script": Path(script).name if script else "",
                    "description": desc,
                })
    hooks.sort(key=lambda h: (_EVENT_ORDER.get(h["event"], 99), h["script"]))
    return {
        "hooks": hooks,
        "count": len(hooks),
        "events": sorted({h["event"] for h in hooks}, key=lambda e: _EVENT_ORDER.get(e, 99)),
    }
