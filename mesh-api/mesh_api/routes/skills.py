"""Route /api/skills — the Claude Code skills installed on this machine.

Scans ~/.claude/skills/*/SKILL.md and parses the frontmatter: name and
description, plus the provenance fields (auto_generated, source_session,
generated_at) that self-documentation writes when a skill is created by an
agent rather than by hand. Feeds /ui/skills.html.
"""
import os
from pathlib import Path

from fastapi import APIRouter, Depends

from ..auth import require_auth

router = APIRouter()

SKILLS_DIR = Path(os.environ.get("CLAUDE_SKILLS_DIR", os.path.expanduser("~/.claude/skills")))


def _parse_skill(skill_md: Path) -> dict:
    name = skill_md.parent.name
    meta = {
        "name": name, "description": "", "auto_generated": False,
        "source_session": None, "generated_at": None, "body_preview": "", "mtime": None,
    }
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except Exception:
        return meta
    body = raw
    fm = {}
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]
            for line in parts[1].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
    meta["description"] = fm.get("description", "")
    meta["auto_generated"] = str(fm.get("auto_generated", "")).lower() == "true"
    meta["source_session"] = fm.get("source_session")
    meta["generated_at"] = fm.get("generated_at")
    meta["body_preview"] = body.strip()[:400]
    try:
        meta["mtime"] = skill_md.stat().st_mtime
    except Exception:
        pass
    return meta


@router.get("/api/skills")
async def list_skills(_: str = Depends(require_auth)):
    skills = []
    if SKILLS_DIR.exists():
        for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            skills.append(_parse_skill(skill_md))
    skills.sort(key=lambda s: s["name"])
    return {
        "skills": skills,
        "count": len(skills),
        "auto_count": sum(1 for s in skills if s["auto_generated"]),
    }
