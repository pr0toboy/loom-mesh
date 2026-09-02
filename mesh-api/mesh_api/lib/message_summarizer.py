"""Deterministic message summarizer for mesh conversation items.

Returns a short string (≤ 100 chars) for each message body.
Pure regex/string — no network, no LLM.
"""
from __future__ import annotations


import os
import re

# Senders that get special treatment, supplied by the deployment as
# comma-separated lists: no agent name is hardcoded here.
#   MESH_HUMAN_SENDERS       human facades — short messages, summarised to the first sentence
#   MESH_SUPERVISOR_SENDERS  supervisors — [ctx], [mesh-alert] and health reports
#
# NOTE: the patterns below match ENGLISH wording ("delivered", "Phase N
# started"). They are behaviour, not prose — a deployment whose agents write in
# another language must replace the vocabulary in the regexes below, or the
# summariser silently falls through to "first line, truncated" for every
# message. That fallback is never wrong, only uninformative.
_HUMAN_SENDERS = tuple(a.strip() for a in os.environ.get("MESH_HUMAN_SENDERS", "").split(",") if a.strip())
_SUPERVISOR_SENDERS = tuple(a.strip() for a in os.environ.get("MESH_SUPERVISOR_SENDERS", "").split(",") if a.strip())

_MAX = 100


def _trunc(s: str, n: int = _MAX) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _strip_md_prefix(s: str) -> str:
    return re.sub(r"^[#*>]+\s*", "", s).strip()


# ── Compiled patterns ──────────────────────────────────────────────────────────

_RE_ACK = re.compile(
    r"^ACK\s+([a-f0-9]{8,})\b(?:\s*[—–\-]+\s*(.+))?",
    re.IGNORECASE,
)
_RE_DELIVERY_PREFIX = re.compile(
    r"(?:^|\b)(?:delivery|delivered|shipped|released)\s+(v\d+\.\d+\.\d+[\w.-]*)",
    re.IGNORECASE,
)
_RE_DELIVERY_INFIX = re.compile(
    r"(v\d+\.\d+\.\d+[\w.-]*)\s+(?:delivered|shipped|released)",
    re.IGNORECASE,
)
_RE_DELIVERY_AGENT_VER = re.compile(
    r"^(\w[\w-]*)\s+(v\d+\.\d+\.\d+[\w.-]*)\s+(?:delivered|shipped|released)",
    re.IGNORECASE,
)
_RE_BRIEF = re.compile(r"^#\s*(Brief|Mandate|v\d+\.\d+\.\d+)\b", re.IGNORECASE | re.MULTILINE)
_RE_PHASE = re.compile(
    r"Phase\s+(\d+)\s+(delivered|closed|started|ready|blocked|to\s+start)",
    re.IGNORECASE,
)
_RE_COMMIT = re.compile(r"\bcommit\s+([a-f0-9]{7,12})\b", re.IGNORECASE)
_RE_HEALTH = re.compile(r"P0=\d+\s+P1=\d+|health\s+check", re.IGNORECASE)
_RE_TICKET_BODY = re.compile(
    r"\[TICKET\s+(tk-[a-f0-9]{6})\]|(?:ticket\s+)?(tk-[a-f0-9]{6})\s+(?:done|failed|completed)",
    re.IGNORECASE,
)
_RE_CTX = re.compile(r"^\[ctx\]", re.IGNORECASE)
_RE_MESH_ALERT = re.compile(r"^\[mesh-alert\]", re.IGNORECASE)
_RE_BY_AGENT = re.compile(r"\bby\s+(\w[\w-]*)", re.IGNORECASE)


# ── Public API ─────────────────────────────────────────────────────────────────

def summarize(body: str, from_: str, kind_hint: str | None = None) -> str:
    try:
        return _summarize(body or "", from_ or "", kind_hint)
    except Exception:
        return _trunc(body, 80) if body else "(empty message)"


# ── Internal ───────────────────────────────────────────────────────────────────

def _summarize(body: str, from_: str, kind_hint: str | None) -> str:
    body = body.strip()
    if not body:
        return "(empty message)"

    # ── human facades: short messages, keep the first sentence ────────────────
    if from_ in _HUMAN_SENDERS:
        if len(body) <= 120:
            return _trunc(body)
        return _trunc(_first_sentence(body))

    # ── supervisors: [ctx], [mesh-alert], health checks ───────────────────────
    if from_ in _SUPERVISOR_SENDERS:
        if _RE_CTX.match(body):
            return _trunc(_strip_md_prefix(body.splitlines()[0]))
        if _RE_MESH_ALERT.match(body):
            line = re.sub(r"^\[mesh-alert\]\s*", "", body.splitlines()[0], flags=re.IGNORECASE)
            return _trunc(line)
        if _RE_HEALTH.search(body):
            return _trunc(_health_summary(body))

    # ── ACK <id> ───────────────────────────────────────────────────────────────
    m = _RE_ACK.match(body)
    if m:
        ack_id = m.group(1)[:8]
        ctx = (m.group(2) or "").strip()
        if ctx:
            first_ctx = ctx.splitlines()[0].strip()
            return _trunc(f"ACK {ack_id} — {first_ctx}")
        return f"ACK {ack_id}"

    first_line = body.splitlines()[0].strip()

    # ── Brief / Mandate — checked before delivery (briefs may quote past deliveries) ──
    if _RE_BRIEF.search(body):
        for line in body.splitlines():
            if re.match(r"^#\s*(Brief|Mandate|v\d+\.\d+\.\d+)\b", line, re.IGNORECASE):
                return _trunc(_strip_md_prefix(line))

    # ── Delivery: "<agent> vX.Y.Z delivered" on the first line ────────────────
    m = _RE_DELIVERY_AGENT_VER.match(first_line)
    if m:
        agent, version = m.group(1), m.group(2)
        return _trunc(f"{version} delivered by {agent}")

    # ── Delivery: "Delivery vX.Y.Z" anywhere ──────────────────────────────────
    m = _RE_DELIVERY_PREFIX.search(body)
    if m:
        version = m.group(1)
        by = _RE_BY_AGENT.search(body)
        by_str = f" by {by.group(1)}" if by else (f" by {from_}" if from_ else "")
        return _trunc(f"{version} delivered{by_str}")

    # ── Delivery: bare "vX.Y.Z delivered" ─────────────────────────────────────
    m = _RE_DELIVERY_INFIX.search(body)
    if m:
        version = m.group(1)
        by = _RE_BY_AGENT.search(body)
        by_str = f" by {by.group(1)}" if by else (f" by {from_}" if from_ else "")
        return _trunc(f"{version} delivered{by_str}")

    # ── Phase N ────────────────────────────────────────────────────────────────
    m = _RE_PHASE.search(body)
    if m:
        state = re.sub(r"\s+", " ", m.group(2))
        return _trunc(f"Phase {m.group(1)} {state}")

    # ── Health check ───────────────────────────────────────────────────────────
    if _RE_HEALTH.search(body):
        return _trunc(_health_summary(body))

    # ── Commit <sha> ───────────────────────────────────────────────────────────
    m = _RE_COMMIT.search(body)
    if m:
        sha = m.group(1)[:7]
        heading = re.search(r"(?:^|\n)#+\s*(.+)", body)
        if heading:
            return _trunc(f"Commit {sha} — {heading.group(1).strip()}")
        return f"Commit {sha}"

    # ── Ticket lifecycle ───────────────────────────────────────────────────────
    m = _RE_TICKET_BODY.search(body)
    if m:
        tk_id = m.group(1) or m.group(2)
        tldr_m = re.search(r"tldr[:\s]+(.+)", body, re.IGNORECASE)
        if tldr_m:
            return _trunc(f"Ticket {tk_id} — {tldr_m.group(1).strip()}")
        return _trunc(f"Ticket {tk_id} done")

    # ── Fallback ───────────────────────────────────────────────────────────────
    if len(body) <= _MAX:
        cleaned = _strip_md_prefix(first_line)
        return cleaned if cleaned else body

    return _trunc(_first_sentence(body))


def _health_summary(body: str) -> str:
    p0 = re.search(r"P0=(\d+)", body)
    p1 = re.search(r"P1=(\d+)", body)
    ok = re.search(r"ok=(true|false)", body, re.IGNORECASE)
    parts = []
    if p0:
        parts.append(f"P0={p0.group(1)}")
    if p1:
        parts.append(f"P1={p1.group(1)}")
    if ok:
        parts.append(f"ok={ok.group(1)}")
    return ("Health check " + " ".join(parts)) if parts else "Health check"


def _first_sentence(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = _strip_md_prefix(line)
        if not cleaned:
            continue
        m = re.match(r"(.+?[.!?])\s", cleaned)
        if m:
            return m.group(1)
        return cleaned[:80]
    return text[:80]
