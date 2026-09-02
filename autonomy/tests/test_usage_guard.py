"""Tests for the usage guard — no live ccusage needed (fixtures + injected runner)."""
import json
from dataclasses import asdict
from datetime import datetime, timezone

from autonomy.usage_guard import (
    UsageLimits, UsageSnapshot, aggregate, collect, evaluate, fleet_snapshot,
    parse_active_block, probe_local, report_local,
)

# A real-shaped `ccusage blocks --active --json` payload (one active 5h window).
ACTIVE_PAYLOAD = {
    "blocks": [{
        "id": "2026-06-04T20:00:00.000Z",
        "startTime": "2026-06-04T20:00:00.000Z",
        "endTime": "2026-06-05T01:00:00.000Z",
        "isActive": True,
        "isGap": False,
        "models": ["claude-opus-4-8", "claude-sonnet-4-6"],
        "costUSD": 16.30,
        "totalTokens": 20438968,
        "tokenCounts": {
            "inputTokens": 12367,
            "outputTokens": 123626,
            "cacheCreationInputTokens": 738130,
            "cacheReadInputTokens": 19564845,
        },
        "burnRate": {"tokensPerMinute": 144590.29, "tokensPerMinuteForIndicator": 962.0479659832512},
        "projection": {"remainingMinutes": 158, "totalTokens": 43284234, "totalCost": 34.52},
    }],
}

# limit-relevant tokens = input + output + cacheCreation (cacheRead excluded)
EXPECTED_LIMIT_TOKENS = 12367 + 123626 + 738130  # = 874123


def test_parse_excludes_cache_reads():
    snap = parse_active_block(ACTIVE_PAYLOAD, "pi")
    assert snap is not None
    assert snap.limit_tokens == EXPECTED_LIMIT_TOKENS
    assert snap.total_tokens == 20438968            # raw kept for reference
    assert snap.limit_tokens < snap.total_tokens    # cache reads dominate the raw count
    assert snap.remaining_minutes == 158
    assert snap.source == "pi"


def test_projection_extrapolates_at_burn():
    snap = parse_active_block(ACTIVE_PAYLOAD, "pi")
    # 874123 + 962.05 * 158 ≈ 1_026_126
    assert 1_020_000 < snap.projected_limit_tokens < 1_032_000


def test_no_active_window_returns_none():
    assert parse_active_block({"blocks": []}, "pi") is None
    assert parse_active_block({"blocks": [{"isActive": False}]}, "pi") is None
    # a gap block must not count as active
    assert parse_active_block({"blocks": [{"isActive": True, "isGap": True}]}, "pi") is None


def test_probe_local_with_injected_runner():
    snap = probe_local("pixel", runner=lambda: json.dumps(ACTIVE_PAYLOAD))
    assert snap.source == "pixel" and snap.limit_tokens == EXPECTED_LIMIT_TOKENS


def test_probe_local_failsoft_on_garbage():
    assert probe_local("pi", runner=lambda: "not json") is None
    def boom():
        raise RuntimeError("ccusage missing")
    assert probe_local("pi", runner=boom) is None


def _snap(source, start, limit_tokens, burn=0.0, remaining=100.0):
    return UsageSnapshot(
        source=source, window_start=start, window_end="2026-06-05T01:00:00.000Z",
        remaining_minutes=remaining, limit_tokens=limit_tokens, total_tokens=limit_tokens,
        output_tokens=0, burn_tpm=burn,
    )


def test_aggregate_sums_same_window():
    pi = _snap("pi", "W1", 800_000, burn=100)
    asus = _snap("asus", "W1", 150_000, burn=50)
    pixel = _snap("pixel", "W1", 50_000, burn=10)
    agg = aggregate([pi, asus, pixel, None])  # None = idle host drops out
    assert agg.limit_tokens == 1_000_000
    assert agg.burn_tpm == 160
    assert agg.source.startswith("fleet:")
    assert "asus" in agg.source and "pi" in agg.source and "pixel" in agg.source


def test_aggregate_picks_most_populated_window():
    # one straggler still on the previous window must not be summed with the live one
    live = [_snap("pi", "W2", 500_000), _snap("asus", "W2", 300_000)]
    straggler = _snap("pixel", "W1", 999_999)
    agg = aggregate(live + [straggler])
    assert agg.window_start == "W2"
    assert agg.limit_tokens == 800_000


def test_aggregate_all_idle_returns_none():
    assert aggregate([None, None]) is None


def test_evaluate_levels():
    limits = UsageLimits(window_token_cap=1_000_000, warn_fraction=0.60, kill_fraction=0.80,
                         kill_on_projection=False)
    assert evaluate(_snap("f", "W", 400_000), limits).level == "ok"
    assert evaluate(_snap("f", "W", 650_000), limits).level == "warn"
    assert evaluate(_snap("f", "W", 850_000), limits).level == "kill"


def test_evaluate_kills_on_projection():
    limits = UsageLimits(window_token_cap=1_000_000, warn_fraction=0.60, kill_fraction=0.80,
                         kill_on_projection=True)
    # used only 50% now, but burning 3000 tpm for 120 min → +360k → 86% projected
    snap = _snap("f", "W", 500_000, burn=3000, remaining=120)
    v = evaluate(snap, limits)
    assert v.level == "kill"
    assert "projected" in v.reason


def test_evaluate_no_data_is_unknown():
    # F2: absence of data is fail-closed unknown, NEVER ok.
    assert evaluate(None, UsageLimits()).level == "unknown"
    assert evaluate(None, UsageLimits(), coverage={"fresh": [], "idle": [],
                                                   "stale": [], "missing": []}).level == "unknown"


def test_evaluate_fully_idle_fleet_is_ok():
    # a fleet that is genuinely idle (fresh idle markers, no gaps) is ok.
    cov = {"fresh": [], "idle": ["pi", "asus"], "stale": [], "missing": []}
    assert evaluate(None, UsageLimits(), coverage=cov).level == "ok"


# --- fleet collection -------------------------------------------------------

def _write_snapshot(d, source, limit_tokens, captured_at, window="W1"):
    snap = UsageSnapshot(
        source=source, window_start=window, window_end="2026-06-05T01:00:00.000Z",
        remaining_minutes=100.0, limit_tokens=limit_tokens, total_tokens=limit_tokens,
        output_tokens=0, burn_tpm=0.0, captured_at=captured_at,
    )
    (d / f"{source}.json").write_text(json.dumps(asdict(snap)), encoding="utf-8")


def test_report_local_writes_then_aggregates(tmp_path):
    snap = report_local("pixel", tmp_path, runner=lambda: json.dumps(ACTIVE_PAYLOAD))
    assert snap is not None
    written = json.loads((tmp_path / "pixel.json").read_text())
    assert written["limit_tokens"] == EXPECTED_LIMIT_TOKENS
    assert written["captured_at"]  # stamped


def test_report_local_idle_writes_marker(tmp_path):
    # F2: idle → write an idle marker (a fresh, deliberate 0), NOT a deletion.
    out = report_local("pi", tmp_path, runner=lambda: json.dumps({"blocks": []}))
    assert out is None
    written = json.loads((tmp_path / "pi.json").read_text())
    assert written["idle"] is True and written["captured_at"]


def test_report_local_error_writes_nothing(tmp_path):
    # A probe failure writes no file → surfaces as staleness/missing, not a 0.
    def boom():
        raise RuntimeError("ccusage missing")
    out = report_local("pi", tmp_path, runner=boom)
    assert out is None
    assert not (tmp_path / "pi.json").exists()


def test_collect_classifies_and_aggregates_fresh(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    old = "2026-06-01T00:00:00+00:00"
    _write_snapshot(tmp_path, "pi", 800_000, now)
    _write_snapshot(tmp_path, "asus", 150_000, now)
    _write_snapshot(tmp_path, "dead-host", 999_999, old)  # offline → stale, not dropped silently
    fresh, coverage = collect(tmp_path, max_age_seconds=600)
    assert {s.source for s in fresh} == {"pi", "asus"}
    assert coverage["fresh"] == ["asus", "pi"]
    assert coverage["stale"] == ["dead-host"]  # classified, not silently discarded
    agg, cov2 = fleet_snapshot(tmp_path, max_age_seconds=600)
    assert agg.limit_tokens == 950_000  # dead host excluded from the aggregate


def test_collect_empty_dir_is_none(tmp_path):
    snaps, coverage = collect(tmp_path)
    assert snaps == []
    assert coverage == {"fresh": [], "idle": [], "stale": [], "missing": []}
    agg, _ = fleet_snapshot(tmp_path)
    assert agg is None
