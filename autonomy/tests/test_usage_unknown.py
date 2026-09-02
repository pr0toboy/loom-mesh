"""F2 — fail-closed usage guard: unknown coverage, pinned ccusage, soft pause."""
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import autonomy.usage_guard as ug
from autonomy.board import Board
from autonomy.orchestrator import coordinator_tick
from autonomy.run import Run
from autonomy.usage_guard import (
    UsageLimits, UsageSnapshot, aggregate, evaluate, fleet_snapshot, parse_ts, probe_local,
)

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "work_drain_stop.py"


def _snap(source, start, limit_tokens, remaining=100.0, burn=0.0):
    return UsageSnapshot(
        source=source, window_start=start, window_end="2026-06-05T01:00:00.000Z",
        remaining_minutes=remaining, limit_tokens=limit_tokens, total_tokens=limit_tokens,
        output_tokens=0, burn_tpm=burn,
    )


def _write(d, source, limit_tokens, captured_at, window="2026-06-04T20:00:00.000Z", idle=False):
    if idle:
        (d / f"{source}.json").write_text(json.dumps(
            {"idle": True, "source": source, "captured_at": captured_at}))
        return
    (d / f"{source}.json").write_text(json.dumps(asdict(_snap(source, window, limit_tokens))
                                                 | {"captured_at": captured_at}))


# --- coverage → unknown -----------------------------------------------------

def test_missing_expected_host_is_unknown(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    _write(tmp_path, "pi", 100_000, now)
    agg, coverage = fleet_snapshot(tmp_path, expected=["pi", "asus"])
    assert coverage["missing"] == ["asus"]
    v = evaluate(agg, UsageLimits(window_token_cap=1_000_000), coverage)
    assert v.level == "unknown" and "asus" in v.reason


def test_stale_host_is_unknown_named(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
    _write(tmp_path, "pi", 100_000, now)
    _write(tmp_path, "asus", 100_000, old)   # >600s → stale
    agg, coverage = fleet_snapshot(tmp_path, max_age_seconds=600)
    assert coverage["stale"] == ["asus"]
    v = evaluate(agg, UsageLimits(window_token_cap=1_000_000), coverage)
    assert v.level == "unknown" and "asus" in v.reason


def test_partial_aggregate_over_kill_is_kill_not_unknown(tmp_path):
    # one host missing, but the hosts we DO see already breach kill → kill wins.
    now = datetime.now(timezone.utc).isoformat()
    _write(tmp_path, "pi", 900_000, now)     # 90% of a 1M cap
    agg, coverage = fleet_snapshot(tmp_path, expected=["pi", "asus"])
    assert coverage["missing"] == ["asus"]
    v = evaluate(agg, UsageLimits(window_token_cap=1_000_000, kill_fraction=0.8), coverage)
    assert v.level == "kill"   # unknown must not mask a real breach


def test_idle_host_does_not_degrade_others(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    _write(tmp_path, "pi", 400_000, now)
    _write(tmp_path, "asus", 0, now, idle=True)   # genuinely idle marker
    agg, coverage = fleet_snapshot(tmp_path, expected=["pi", "asus"])
    assert coverage["idle"] == ["asus"] and not coverage["missing"] and not coverage["stale"]
    v = evaluate(agg, UsageLimits(window_token_cap=1_000_000), coverage)
    assert v.level == "ok"   # idle is a real ok, not a gap


# --- window grouping across timestamp formats -------------------------------

def test_parse_ts_tolerant():
    assert parse_ts("2026-06-04T20:00:00Z") == parse_ts("2026-06-04T20:00:00+00:00")
    assert parse_ts("2026-06-04T20:00:00.000Z").tzinfo is not None
    assert parse_ts("2026-06-04T20:00:00").tzinfo == timezone.utc  # naive → UTC
    assert parse_ts("garbage") is None


def test_aggregate_groups_across_ts_formats():
    # same 5h window, three formats → ONE group, summed together
    a = _snap("pi", "2026-06-04T20:00:00.000Z", 500_000)
    b = _snap("asus", "2026-06-04T20:00:00+00:00", 300_000)
    c = _snap("pixel", "2026-06-04T20:15:00Z", 100_000)   # same UTC hour
    agg = aggregate([a, b, c])
    assert agg.limit_tokens == 900_000  # all three in one window


# --- pinned ccusage version -------------------------------------------------

def test_probe_uses_pinned_version_no_latest(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        class R:
            stdout = json.dumps({"blocks": []})
        return R()

    monkeypatch.delenv("LOOM_CCUSAGE_VERSION", raising=False)
    monkeypatch.setattr(ug.subprocess, "run", fake_run)
    probe_local("pi")  # runner=None → builds the real argv
    argv = captured["argv"]
    assert not any("@latest" in a for a in argv)
    assert any(f"ccusage@{ug.CCUSAGE_VERSION}" == a for a in argv)


def test_probe_honours_version_override(monkeypatch):
    captured = {}
    monkeypatch.setenv("LOOM_CCUSAGE_VERSION", "9.9.9")
    monkeypatch.setattr(ug.subprocess, "run",
                        lambda argv, **k: captured.setdefault("argv", argv) or type("R", (), {"stdout": "{}"})())
    probe_local("pi")
    assert "ccusage@9.9.9" in captured["argv"]


# --- orchestrator soft pause ------------------------------------------------

def test_tick_unknown_pauses_then_halts_then_resumes(tmp_path):
    Run(tmp_path).start("r1", ["w"], guardrails={
        "window_token_cap": 1_000_000, "usage_sources": ["pi", "asus"],
        "unknown_grace_ticks": 3,
    })
    b = Board(tmp_path)
    b.create_task("T1", "x")             # ready, unowned
    b.create_task("T2", "y", owner="w")  # owned open work → would be kicked but for the pause
    b.claim("T2", "w")                    # in_progress, so open_by_agent = {w: [T2]}
    usage = tmp_path / ".loom" / "usage"
    usage.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    _write(usage, "pi", 100_000, now)   # asus missing → unknown

    # ticks 1-2: soft pause, no assignment, NO kick (no forced relaunch), run active.
    # kick would be ["w"] (w owns the in_progress T2) if the pause didn't suppress it.
    for i in range(2):
        rep = coordinator_tick(tmp_path, {"w": []}, usage_dir=usage)
        assert rep.usage_level == "unknown"
        assert rep.assigned == []
        assert rep.kick == []                   # M2: agents are not relaunched during a pause
        assert rep.halt is False
    assert b.tasks()["T1"]["owner"] is None    # never assigned under unknown

    # tick 3: grace exhausted → halt + usage_pause logged, run STILL active
    rep = coordinator_tick(tmp_path, {"w": []}, usage_dir=usage)
    assert rep.halt is True and rep.usage_level == "unknown"
    assert "usage_pause" in [e["type"] for e in b.events()]
    assert Run(tmp_path).active() is True

    # tick 4: still unknown, still halts — but usage_pause is NOT re-logged (R1: log
    # only at the crossing, no board spam while the pause persists)
    coordinator_tick(tmp_path, {"w": []}, usage_dir=usage)
    assert sum(1 for e in b.events() if e["type"] == "usage_pause") == 1

    # data returns fresh for both → next tick resumes and assigns (unknown_ticks reset)
    _write(usage, "asus", 100_000, datetime.now(timezone.utc).isoformat())
    rep = coordinator_tick(tmp_path, {"w": []}, usage_dir=usage)
    assert rep.halt is False
    assert any(a["task"] == "T1" for a in rep.assigned)


def test_unknown_ticks_reset_lets_next_pause_start_fresh(tmp_path):
    # R6: after data returns, the unknown counter resets — a later single unknown
    # tick is a soft pause (not an immediate halt from a carried-over count).
    Run(tmp_path).start("r1", ["w"], guardrails={
        "window_token_cap": 1_000_000, "usage_sources": ["pi", "asus"],
        "unknown_grace_ticks": 3})
    b = Board(tmp_path)
    b.create_task("T1", "x")
    usage = tmp_path / ".loom" / "usage"
    usage.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    _write(usage, "pi", 100_000, now)   # asus missing → unknown
    for _ in range(3):                  # exhaust grace → halt
        coordinator_tick(tmp_path, {"w": []}, usage_dir=usage)
    _write(usage, "pi", 100_000, datetime.now(timezone.utc).isoformat())
    _write(usage, "asus", 100_000, datetime.now(timezone.utc).isoformat())
    coordinator_tick(tmp_path, {"w": []}, usage_dir=usage)   # fresh → reset
    # asus goes missing again → the very next tick is a soft pause, NOT a halt
    (usage / "asus.json").unlink()
    rep = coordinator_tick(tmp_path, {"w": []}, usage_dir=usage)
    assert rep.usage_level == "unknown" and rep.halt is False


def test_stale_idle_marker_is_classified_stale(tmp_path):
    # R6: an idle marker that has gone stale is a coverage gap (stale), not a fresh
    # idle → it must drive unknown, not ok.
    old = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
    _write(tmp_path, "pi", 0, old, idle=True)   # stale idle marker
    _, coverage = fleet_snapshot(tmp_path, max_age_seconds=600)
    assert coverage["stale"] == ["pi"] and coverage["idle"] == []
    v = evaluate(None, UsageLimits(), coverage)
    assert v.level == "unknown"


# --- hook soft pause --------------------------------------------------------

def _run_hook(root, agent):
    env = {"PATH": "/usr/bin:/bin", "LOOM_ROOT": str(root), "MESH_AGENT": agent}
    return subprocess.run([sys.executable, str(HOOK)], input="{}",
                          capture_output=True, text=True, env=env, timeout=30)


def test_hook_unknown_allows_stop_without_block(tmp_path):
    Run(tmp_path).start("r1", ["w"], guardrails={
        "window_token_cap": 1_000_000, "usage_sources": ["pi", "asus"],
    })
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    usage = tmp_path / ".loom" / "usage"
    usage.mkdir(parents=True)
    _write(usage, "pi", 100_000, datetime.now(timezone.utc).isoformat())  # asus missing → unknown

    p = _run_hook(tmp_path, "w")
    assert '"decision": "block"' not in p.stdout   # soft pause → allow stop
    assert b.tasks()["T1"]["status"] != "blocked"  # tasks stay open, NOT blocked
    assert "task_blocked" not in [e["type"] for e in b.events()]
