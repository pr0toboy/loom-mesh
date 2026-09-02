#!/usr/bin/env python3
"""Night window — the hours during which the engine is allowed to keep agents working.

Why this exists: agents may run full-auto, but *only at
night*, so that his own quota is free when he starts working in the morning.

The arithmetic that fixes the closing hour — it is not a round number by taste:
the subscription's rate limit is a **5-hour window that opens on the first
request** and then runs 5 h. An agent still sending at 03:00 opens a window that
lasts until 08:00, i.e. exactly onto his morning. Closing at **02:30** means the
latest window the engine can open ends at 07:30, and 08:00 is clean whatever
happens during the night.

Note what this does *not* buy: the 7-day quota has no separate night budget.
Night work is invisible to the 5 h window by morning, never to the weekly one.

Fail direction, deliberately asymmetric:
  - **no config file** → unrestricted (returns ``None``). Keeps the engine a
    general-purpose tool: a dev run or a test is not silently time-gated.
  - **config present but unreadable/invalid** → **closed**. A corrupt config must
    not be a licence to burn the morning quota; the operator has stated an intent
    and we honour its safe side.

The config is *fleet-level*, not per-run, on purpose: a run started by anyone who
forgot the rule is still gated. Placing it in the guardrails would have made the
protection depend on the person starting the run remembering it.

Config (``~/mesh/autonomy-night.json``, override with ``LOOM_NIGHT_CONFIG``)::

    {"start": "23:00", "stop": "02:30"}

``start > stop`` is the normal case and means the window wraps past midnight.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time
from pathlib import Path
from typing import NamedTuple

CONFIG_ENV = "LOOM_NIGHT_CONFIG"
DEFAULT_CONFIG = Path.home() / "mesh" / "autonomy-night.json"


class Decision(NamedTuple):
    """``open`` is None when no window is configured (= no restriction)."""
    open: bool | None
    reason: str

    @property
    def closed(self) -> bool:
        """True only for an explicit refusal — ``None`` (unconfigured) is not a refusal."""
        return self.open is False


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV, "").strip()
    return Path(override) if override else DEFAULT_CONFIG


def _parse_hhmm(raw: object) -> time:
    """Accept only ``HH:MM``. Anything else raises — the caller turns that into a
    closed window rather than guessing an hour."""
    hh, mm = str(raw).strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"hour out of range: {raw!r}")
    return time(h, m)


def decide(now: datetime | None = None, path: Path | None = None) -> Decision:
    p = path or config_path()
    if not p.exists():
        return Decision(None, "no night window configured")

    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        start = _parse_hhmm(cfg["start"])
        stop = _parse_hhmm(cfg["stop"])
    except Exception as exc:
        # Fail closed: a stated intent we cannot read is honoured on its safe side.
        return Decision(False, f"night window config unreadable ({exc.__class__.__name__}: {exc}) → closed")

    t = (now or datetime.now()).time()
    if start == stop:
        # Degenerate: an empty window is a stop, not a 24 h licence.
        return Decision(False, f"night window start == stop ({start:%H:%M}) → closed")

    inside = (start <= t < stop) if start < stop else (t >= start or t < stop)
    window = f"{start:%H:%M}-{stop:%H:%M}"
    if inside:
        return Decision(True, f"inside night window {window} (now {t:%H:%M})")
    return Decision(False, f"outside night window {window} (now {t:%H:%M})")


def is_open(now: datetime | None = None, path: Path | None = None) -> bool:
    """Convenience for callers that treat 'unconfigured' as allowed."""
    return decide(now, path).open is not False
