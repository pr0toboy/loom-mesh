#!/usr/bin/env python3
"""Usage guard — fleet-wide rate-limit budgeting for autonomous LoomMesh runs.

When agents drive themselves autonomously (no human in the loop), the single
real danger is burning the shared subscription's rolling-window rate limit while
nobody is watching. This module turns local usage data into a fleet-wide gauge
and a kill verdict, so the autonomy loop can throttle or stop a run *before* it
walls the whole fleet.

Design
------
- **Source of truth = local logs, not scraping.** Each Claude Code session writes
  per-message token usage to local JSONL. We read it through `ccusage`
  (https://github.com/ryoppippi/ccusage), which already models the provider's
  5-hour rolling windows ("blocks") and emits clean JSON in one shot.
- **The metric that maps to the plan limit excludes cache reads.** Raw
  `totalTokens` is dominated by `cacheReadInputTokens`, which are cheap and do
  not weigh on the limit the way fresh tokens do — ccusage's own
  `tokensPerMinuteForIndicator` is ~150x smaller than the raw rate for exactly
  this reason. So we budget on `limit_tokens = input + output + cacheCreation`.
- **Fleet aggregation.** One host's ccusage only sees the agents that share its
  user account. Agents on other machines consume the *same* subscription window
  but log locally. So each host emits a `UsageSnapshot`; they are summed by
  aligned window (the 5h block boundaries are UTC-aligned, so they line up).
- **Policy vs mechanism.** This module *measures* and *judges* (OK / WARN / KILL).
  Acting on a KILL verdict (flipping the run's stop flag, notifying) is the
  caller's job — kept separate on purpose.

The exact ceiling of a flat-rate subscription is not published, so the cap is an
operator-set estimate with headroom; the guard fires on the calibrated fraction,
and a real 429 wall is the hard backstop the caller should also watch for.

Pure stdlib. No network. `ccusage` is invoked as a subprocess (or inject a
`runner` for tests).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# Pinned ccusage version — never `@latest`, so a silent upstream schema change
# can't reshape the numbers under us. Override for a bump with LOOM_CCUSAGE_VERSION;
# the contract test (test_usage_guard) locks the parse against a recorded payload.
CCUSAGE_VERSION = "20.0.14"


# --- normalized snapshot ----------------------------------------------------

@dataclass
class UsageSnapshot:
    """One host's view of the *active* rolling window."""
    source: str                  # host/agent-group label, e.g. "primary", "secondary", "mobile"
    window_start: str            # ISO-8601 UTC — the 5h block start (alignment key)
    window_end: str              # ISO-8601 UTC
    remaining_minutes: float     # until the window resets
    limit_tokens: int            # input + output + cacheCreation (maps to the plan limit)
    total_tokens: int            # raw, incl. cache reads (for reference only)
    output_tokens: int
    burn_tpm: float              # limit-relevant tokens/min (ccusage indicator rate)
    models: list[str] = field(default_factory=list)
    cost_usd_estimate: float = 0.0   # API-equivalent estimate; meaningless on flat plans
    captured_at: str = ""            # wall-clock ISO of the probe (for staleness)

    @property
    def projected_limit_tokens(self) -> int:
        """limit_tokens extrapolated to the end of the window at current burn."""
        return int(self.limit_tokens + max(0.0, self.burn_tpm) * max(0.0, self.remaining_minutes))


# --- ccusage probe ----------------------------------------------------------

def _limit_tokens(tc: dict) -> int:
    # Exclude cacheRead — it does not weigh on the window limit like fresh tokens.
    return int(tc.get("inputTokens", 0)) + int(tc.get("outputTokens", 0)) + int(tc.get("cacheCreationInputTokens", 0))


def parse_active_block(payload: dict, source: str) -> UsageSnapshot | None:
    """Turn a `ccusage blocks --active --json` payload into a snapshot.

    Returns None when there is no active window (idle host = contributes nothing).
    """
    blocks = (payload or {}).get("blocks", [])
    active = next((b for b in blocks if b.get("isActive") and not b.get("isGap")), None)
    if active is None:
        return None
    tc = active.get("tokenCounts", {}) or {}
    burn = active.get("burnRate") or {}
    proj = active.get("projection") or {}
    return UsageSnapshot(
        source=source,
        window_start=active.get("startTime") or active.get("id", ""),
        window_end=active.get("endTime", ""),
        remaining_minutes=float(proj.get("remainingMinutes", 0) or 0),
        limit_tokens=_limit_tokens(tc),
        total_tokens=int(active.get("totalTokens", 0) or 0),
        output_tokens=int(tc.get("outputTokens", 0) or 0),
        # Prefer the indicator rate (cache-read-excluded); fall back to raw.
        burn_tpm=float(burn.get("tokensPerMinuteForIndicator",
                                burn.get("tokensPerMinute", 0)) or 0),
        models=list(active.get("models", []) or []),
        cost_usd_estimate=float(active.get("costUSD", 0) or 0),
        captured_at=now_iso(),
    )


def _ccusage_version() -> str:
    return os.environ.get("LOOM_CCUSAGE_VERSION", "").strip() or CCUSAGE_VERSION


def _default_runner():
    out = subprocess.run(
        ["npx", "-y", f"ccusage@{_ccusage_version()}", "blocks", "--active", "--json"],
        capture_output=True, text=True, timeout=120,
    )
    return out.stdout


def _probe(source: str, runner=None) -> tuple[str, UsageSnapshot | None]:
    """Probe ccusage and classify the outcome:
      - ``("active", snap)`` — a live window was parsed;
      - ``("idle", None)``   — ccusage ran fine but there is no active window;
      - ``("error", None)``  — the probe raised or returned unparsable JSON.
    The idle/error distinction is what lets ``report_local`` write an *idle marker*
    (a real "0, on purpose") vs write nothing (a failure, visible via staleness)."""
    if runner is None:
        runner = _default_runner
    try:
        raw = runner()
        payload = json.loads(raw)
    except Exception:
        return ("error", None)
    snap = parse_active_block(payload, source)
    return ("idle", None) if snap is None else ("active", snap)


def probe_local(source: str, runner=None) -> UsageSnapshot | None:
    """Run ccusage locally and parse the active window. `runner` is injectable
    for tests: a callable() -> json string. Fail-soft: error or idle → None."""
    return _probe(source, runner)[1]


# --- fleet aggregation ------------------------------------------------------

def aggregate(snapshots) -> UsageSnapshot | None:
    """Sum per-host snapshots that belong to the same rolling window.

    Snapshots are grouped by `window_start` (UTC-aligned 5h boundary). The most
    populated window is taken as "the current fleet window" (hosts a few seconds
    out of sync still align on the same boundary id). Idle hosts (None) drop out.
    """
    snaps = [s for s in snapshots if s is not None]
    if not snaps:
        return None
    groups: dict = {}
    for s in snaps:
        # Group by the window's start rounded to the hour (5h blocks are UTC-aligned),
        # so two hosts whose window_start differ only in format (`...Z` vs `+00:00`,
        # with/without millis) still land in ONE group. Unparsable timestamps fall
        # back to raw-string equality (legacy behaviour, e.g. the "W1" test ids).
        dt = parse_ts(s.window_start)
        gkey = int(dt.timestamp()) // 3600 if dt is not None else s.window_start
        groups.setdefault(gkey, []).append(s)
    # the window with the most contributing hosts (ties → most tokens)
    gkey = max(groups, key=lambda k: (len(groups[k]), sum(x.limit_tokens for x in groups[k])))
    group = groups[gkey]
    return UsageSnapshot(
        source="fleet:" + "+".join(sorted(s.source for s in group)),
        window_start=min(s.window_start for s in group),
        window_end=group[0].window_end,
        remaining_minutes=min(s.remaining_minutes for s in group),
        limit_tokens=sum(s.limit_tokens for s in group),
        total_tokens=sum(s.total_tokens for s in group),
        output_tokens=sum(s.output_tokens for s in group),
        burn_tpm=sum(s.burn_tpm for s in group),
        models=sorted({m for s in group for m in s.models}),
        cost_usd_estimate=sum(s.cost_usd_estimate for s in group),
    )


# --- fleet collection (cross-host) ------------------------------------------
#
# One host's ccusage only sees the agents sharing its user account. To budget
# the *shared* subscription, every host writes its own snapshot to a common
# directory; the collector reads them all and hands them to `aggregate`. The
# directory is synced by whatever transport the run already uses (git, or the
# mesh pushing each host's file to the coordinator). Stale files (a host that
# went offline) are dropped so a dead host can't pin the gauge at an old value.

from pathlib import Path  # noqa: E402  (kept local to this section for clarity)


def report_local(source: str, usage_dir, runner=None) -> UsageSnapshot | None:
    """Probe locally and write ``<usage_dir>/<source>.json``. Returns the snapshot,
    or ``None`` when idle or on a probe failure.

    Fail-closed semantics:
      - **active** → write the snapshot;
      - **idle**   → write an *idle marker* (``{"idle": true, ...}``) — a fresh,
        deliberate "0", NOT a deletion (a deleted file used to be read as "all
        good", which is exactly the failure mode this closes);
      - **error**  → write **nothing**; the probe failure surfaces as staleness.
    A file is never deleted here — deletion is no longer a signal."""
    d = Path(usage_dir)
    d.mkdir(parents=True, exist_ok=True)
    status, snap = _probe(source, runner=runner)
    target = d / f"{source}.json"
    if status == "error":
        return None  # write nothing → becomes stale/missing → unknown
    if status == "idle":
        target.write_text(json.dumps(
            {"idle": True, "source": source, "captured_at": now_iso()},
            ensure_ascii=False), encoding="utf-8")
        return None
    target.write_text(json.dumps(asdict(snap), ensure_ascii=False), encoding="utf-8")
    return snap


def parse_ts(s: str | None) -> datetime | None:
    """Tolerant ISO-8601 parse: accepts a trailing ``Z``, an explicit offset,
    fractional seconds, or no tz at all (assumed UTC). ``None`` on anything else."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_seconds(captured_at: str, now: datetime) -> float:
    dt = parse_ts(captured_at)
    if dt is None:
        return float("inf")  # no/unparsable timestamp → treat as infinitely stale
    return (now - dt).total_seconds()


def collect(usage_dir, max_age_seconds: float = 600.0, expected=None):
    """Read every host file in ``usage_dir`` and classify coverage.

    Returns ``(snapshots, coverage)`` where ``snapshots`` are the fresh *active*
    windows to aggregate and ``coverage`` is
    ``{"fresh": [...], "idle": [...], "stale": [...], "missing": [...]}``:
      - **fresh**   — a fresh active snapshot (contributes to the aggregate);
      - **idle**    — a fresh idle marker (a real 0, no coverage gap);
      - **stale**   — a file too old, unparsable, or malformed (NO LONGER dropped
        silently — a stale host is a coverage gap, not a zero);
      - **missing** — an ``expected`` host label with no file at all.
    ``expected`` (a list of host labels) enables the missing-host detection; when
    ``None`` the guard runs in degraded mode (staleness still detected)."""
    coverage = {"fresh": [], "idle": [], "stale": [], "missing": []}
    snapshots: list[UsageSnapshot] = []
    seen: set[str] = set()
    d = Path(usage_dir)
    if d.is_dir():
        now = datetime.now(timezone.utc)
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                coverage["stale"].append(f.stem)  # unreadable → coverage gap
                seen.add(f.stem)
                continue
            source = data.get("source") or f.stem
            seen.add(source)
            if _age_seconds(data.get("captured_at", ""), now) > max_age_seconds:
                coverage["stale"].append(source)
                continue
            if data.get("idle"):
                coverage["idle"].append(source)
                continue
            data.pop("projected_limit_tokens", None)  # property, not a field
            try:
                snapshots.append(UsageSnapshot(**data))
            except Exception:
                coverage["stale"].append(source)  # malformed → coverage gap
                continue
            coverage["fresh"].append(source)
    if expected:
        for label in expected:
            if label not in seen:
                coverage["missing"].append(label)
    for k in coverage:
        coverage[k] = sorted(set(coverage[k]))
    return snapshots, coverage


def fleet_snapshot(usage_dir, max_age_seconds: float = 600.0, expected=None):
    """Convenience: collect every fresh host file and aggregate into one window.
    Returns ``(aggregate|None, coverage)``."""
    snaps, coverage = collect(usage_dir, max_age_seconds=max_age_seconds, expected=expected)
    return aggregate(snaps), coverage


# --- policy: judge the window ----------------------------------------------

@dataclass
class UsageLimits:
    """Operator-calibrated budget for one rolling window.

    `window_token_cap` is an *estimate* of how many limit-relevant tokens the
    subscription tolerates per window before the provider walls it (429). Set it
    below the real wall to keep headroom; a real 429 is the hard backstop.
    """
    # Calibrated 2026-06-05 against the native /usage gauge: it read 14% of the
    # 5h window when a same-moment ccusage probe showed ~935k limit-relevant
    # tokens → real cap ≈ 935k / 0.14 ≈ 6.7M tokens/window. Single anchor; refine
    # as more (gauge %, ccusage tokens) pairs are collected. A real 429 is the
    # hard backstop. NOTE: the anchor was taken while only Pi agents were active
    # (Asus down) — when the whole fleet runs, the fleet aggregate is what counts.
    window_token_cap: int = 6_700_000
    warn_fraction: float = 0.60          # gauge turns amber
    kill_fraction: float = 0.80          # stop launching new work
    kill_on_projection: bool = True      # also kill if *projected* end-of-window ≥ kill


@dataclass
class UsageVerdict:
    level: str            # "ok" | "warn" | "kill" | "unknown"
    reason: str
    usage_fraction: float
    projected_fraction: float
    snapshot: dict        # the aggregate snapshot, as a dict, for logging


def evaluate(aggregate_snapshot: UsageSnapshot | None, limits: UsageLimits,
             coverage: dict | None = None) -> UsageVerdict:
    """Judge the window, fail-closed on incomplete coverage.

    Order, worst → best:
      (a) the *known* aggregate already at/over kill ⇒ ``kill`` — partial data that
          already breaches suffices; ``unknown`` must never mask a real breach;
      (b) else any ``stale``/``missing`` coverage ⇒ ``unknown`` (soft pause), the
          gap named in the reason — no more silent under-counting;
      (c) no aggregate at all: a fully-idle fleet (fresh idle markers, no gap) is
          ``ok``; otherwise ``unknown`` ("no usage data") — **never** ``ok``;
      (d) else ``ok``/``warn`` on the usage fraction as before."""
    cap = max(1, limits.window_token_cap)
    used = projected = 0.0
    snapdict: dict = {}
    if aggregate_snapshot is not None:
        used = aggregate_snapshot.limit_tokens / cap
        projected = aggregate_snapshot.projected_limit_tokens / cap
        snapdict = asdict(aggregate_snapshot)
        # (a) a breach on the hosts we DO see wins over any coverage gap.
        if used >= limits.kill_fraction:
            return UsageVerdict("kill", (
                f"window usage {used:.0%} ≥ kill {limits.kill_fraction:.0%} "
                f"({aggregate_snapshot.limit_tokens:,}/{cap:,} tok)"), used, projected, snapdict)
        if limits.kill_on_projection and projected >= limits.kill_fraction:
            return UsageVerdict("kill", (
                f"projected end-of-window {projected:.0%} ≥ kill {limits.kill_fraction:.0%} "
                f"(reset in {aggregate_snapshot.remaining_minutes:.0f} min at current burn)"),
                used, projected, snapdict)

    stale = list(coverage.get("stale", [])) if coverage else []
    missing = list(coverage.get("missing", [])) if coverage else []
    # (b) coverage gap → fail-closed unknown (the absence of data is not "ok").
    if stale or missing:
        srcs = ", ".join(sorted(set(stale + missing)))
        return UsageVerdict("unknown", f"usage coverage incomplete (stale/missing: {srcs})",
                            used, projected, snapdict)

    # (c) nothing to aggregate.
    if aggregate_snapshot is None:
        if coverage and coverage.get("idle"):
            return UsageVerdict("ok", "fleet idle (fresh markers)", 0.0, 0.0, {})
        return UsageVerdict("unknown", "no usage data", 0.0, 0.0, {})

    # (d) fresh, complete, below kill.
    if used >= limits.warn_fraction:
        return UsageVerdict("warn", f"window usage {used:.0%} ≥ warn {limits.warn_fraction:.0%}",
                            used, projected, snapdict)
    return UsageVerdict("ok", f"window usage {used:.0%}", used, projected, snapdict)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- CLI --------------------------------------------------------------------

def _main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="LoomMesh usage guard")
    p.add_argument("--source", default="local", help="host/agent-group label")
    p.add_argument("--probe", action="store_true", help="run ccusage and print the local snapshot as JSON")
    p.add_argument("--report", metavar="DIR", help="probe and write this host's snapshot to DIR/<source>.json, then exit")
    p.add_argument("--dir", metavar="DIR", help="judge the aggregated FLEET from snapshot files in DIR instead of probing locally")
    p.add_argument("--expect", metavar="LABELS", help="comma-separated expected host labels (enables missing-host → unknown detection)")
    p.add_argument("--max-age", type=float, default=600.0, help="classify fleet snapshots older than this many seconds as stale")
    p.add_argument("--cap", type=int, default=UsageLimits.window_token_cap, help="window token cap (limit-relevant tokens)")
    p.add_argument("--warn", type=float, default=UsageLimits.warn_fraction)
    p.add_argument("--kill", type=float, default=UsageLimits.kill_fraction)
    args = p.parse_args(argv)

    if args.report:
        snap = report_local(args.source, args.report)
        print(json.dumps(asdict(snap) if snap else None, indent=2))
        return 0

    coverage = None
    if args.dir:
        expected = [s.strip() for s in (args.expect or "").split(",") if s.strip()] or None
        snap, coverage = fleet_snapshot(args.dir, max_age_seconds=args.max_age, expected=expected)
    else:
        snap = probe_local(args.source)
    if args.probe:
        print(json.dumps(asdict(snap) if snap else None, indent=2))
        return 0
    verdict = evaluate(snap, UsageLimits(window_token_cap=args.cap,
                                         warn_fraction=args.warn, kill_fraction=args.kill),
                       coverage)
    print(json.dumps({"at": now_iso(), "level": verdict.level, "reason": verdict.reason,
                      "usage_fraction": round(verdict.usage_fraction, 4),
                      "projected_fraction": round(verdict.projected_fraction, 4)}, indent=2))
    # exit code doubles as a kill signal for shell callers
    return {"ok": 0, "warn": 0, "kill": 3, "unknown": 4}[verdict.level]


if __name__ == "__main__":
    raise SystemExit(_main())
