"""Tests for the night reconciler.

``_kick`` and ``_interrupt_pane`` are stubbed everywhere: they are the two real
side effects (a mesh message to a live agent, Escape into a live pane), and a
test suite that exercised them for real would nudge the actual fleet.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from autonomy import night_reconcile as nr
from autonomy.board import Board
from autonomy.run import Run


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    """Stub the three real side effects: a mesh message, Escape into a live pane,
    and the ccusage probe (a subprocess that would hit the network per test)."""
    sent, interrupted = [], []
    monkeypatch.setattr(nr, "_kick", lambda a, g, log: (sent.append(a) or True))
    monkeypatch.setattr(nr, "_interrupt_pane", lambda a, log: interrupted.append(a))
    monkeypatch.setattr(nr, "report_local", lambda source, d: None)
    return sent, interrupted


def _hhmm(dt):
    return dt.strftime("%H:%M")


def _window(monkeypatch, tmp_path, *, open_now: bool):
    now = datetime.now()
    if open_now:
        cfg = {"start": _hhmm(now - timedelta(minutes=60)),
               "stop": _hhmm(now + timedelta(minutes=60))}
    else:
        start = now + timedelta(hours=2)
        cfg = {"start": _hhmm(start), "stop": _hhmm(start + timedelta(minutes=20))}
    p = tmp_path / "night.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("LOOM_NIGHT_CONFIG", str(p))
    return p


def _make_run(runs_dir: Path, name: str, participants=("worker-a",)) -> Path:
    root = runs_dir / name
    root.mkdir(parents=True)
    Run(root).start(f"r-{name}", list(participants), goal="ship it")
    Board(root).create_task("T1", "x", owner=participants[0])
    return root


def test_pauses_an_active_run_outside_the_window(tmp_path, monkeypatch, no_side_effects):
    _window(monkeypatch, tmp_path, open_now=False)
    runs = tmp_path / "runs"
    root = _make_run(runs, "alpha")

    out = nr.reconcile(runs)
    assert out["paused"] == ["alpha"]
    st = Run(root).status()
    assert st["active"] is False
    assert st["end_reason"] == nr.NIGHT_PAUSE
    assert "night_pause" in [e["type"] for e in Board(root).events()]
    assert no_side_effects[1] == ["worker-a"]        # turn in flight interrupted


def test_resumes_only_what_the_night_paused(tmp_path, monkeypatch, no_side_effects):
    _window(monkeypatch, tmp_path, open_now=True)
    runs = tmp_path / "runs"
    night = _make_run(runs, "paused-by-night")
    done = _make_run(runs, "converged")
    Run(night).end(reason=nr.NIGHT_PAUSE)
    Run(done).end(reason="goal reached")

    out = nr.reconcile(runs)
    assert out["resumed"] == ["paused-by-night"]
    assert Run(night).status()["active"] is True
    assert Run(done).status()["active"] is False     # convergence is not undone
    assert no_side_effects[0] == ["worker-a"]


def test_resume_preserves_the_iteration_budget(tmp_path, monkeypatch, no_side_effects):
    """The runaway guard must not get a fresh budget every night."""
    _window(monkeypatch, tmp_path, open_now=True)
    runs = tmp_path / "runs"
    root = _make_run(runs, "alpha")
    r = Run(root)
    for _ in range(7):
        r.bump_iteration()
    r.end(reason=nr.NIGHT_PAUSE)

    nr.reconcile(runs)
    st = r.status()
    assert st["active"] is True
    assert st["iterations"] == 7
    assert "ended" not in st and "end_reason" not in st


def test_dry_run_changes_nothing_at_all(tmp_path, monkeypatch, no_side_effects):
    _window(monkeypatch, tmp_path, open_now=False)
    runs = tmp_path / "runs"
    root = _make_run(runs, "alpha")
    before = (Run(root).path.read_text(), len(Board(root).events()))

    out = nr.reconcile(runs, dry_run=True)
    assert out["paused"] == ["alpha"]                       # reported...
    assert Run(root).path.read_text() == before[0]          # ...but nothing written
    assert len(Board(root).events()) == before[1]
    assert no_side_effects[1] == []                         # no Escape either


def test_is_idempotent(tmp_path, monkeypatch, no_side_effects):
    _window(monkeypatch, tmp_path, open_now=False)
    runs = tmp_path / "runs"
    _make_run(runs, "alpha")
    assert nr.reconcile(runs)["paused"] == ["alpha"]
    assert nr.reconcile(runs)["paused"] == []               # already paused
    assert nr.reconcile(runs)["paused"] == []


def test_wake_cap_defers_the_surplus(tmp_path, monkeypatch, no_side_effects):
    _window(monkeypatch, tmp_path, open_now=True)
    monkeypatch.setattr(nr, "MAX_KICKS", 2)
    runs = tmp_path / "runs"
    root = _make_run(runs, "alpha", participants=("w1", "w2", "w3", "w4"))
    Run(root).end(reason=nr.NIGHT_PAUSE)

    out = nr.reconcile(runs)
    assert no_side_effects[0] == ["w1", "w2"]
    assert out["deferred"] == ["w3", "w4"]                  # reported, not silent


def test_feeds_the_kill_switch_of_active_runs_only(tmp_path, monkeypatch, no_side_effects):
    """An unfed guard is an unguarded night: the hook reads a snapshot no older
    than 600 s, so every active run must get one every pass — and only active ones."""
    _window(monkeypatch, tmp_path, open_now=True)
    runs = tmp_path / "runs"
    live = _make_run(runs, "live")
    idle = _make_run(runs, "idle")
    Run(idle).end(reason="goal reached")

    calls = []
    monkeypatch.setattr(nr, "report_local", lambda source, d: calls.append(Path(d)) or None)
    out = nr.reconcile(runs)
    assert out["usage_fed"] == ["live"]
    assert calls == [live / ".loom" / "usage"]


def test_usage_refresh_failure_does_not_stop_the_pass(tmp_path, monkeypatch, no_side_effects):
    # Window OPEN and the run active, otherwise the refresh is never attempted and
    # the test would pass without exercising the failure path at all.
    _window(monkeypatch, tmp_path, open_now=True)
    runs = tmp_path / "runs"
    _make_run(runs, "alpha")
    _make_run(runs, "beta")

    calls = []

    def boom(source, d):
        calls.append(d)
        raise RuntimeError("ccusage exploded")
    monkeypatch.setattr(nr, "report_local", boom)

    out = nr.reconcile(runs)
    assert len(calls) == 2                      # both roots were attempted...
    assert out["usage_fed"] == []               # ...none reported as fed
    assert out["window"] is True                # and the pass completed


def test_a_stale_pause_is_not_resurrected(tmp_path, monkeypatch, no_side_effects):
    """The zombie case: the June 2026 addup run, still active with todo tasks."""
    _window(monkeypatch, tmp_path, open_now=True)
    runs = tmp_path / "runs"
    root = _make_run(runs, "zombie")
    r = Run(root)
    r.end(reason=nr.NIGHT_PAUSE)
    st = r.status()
    st["ended"] = (datetime.now() - timedelta(days=nr.MAX_PAUSE_AGE_DAYS + 1)).isoformat()
    (root / ".loom" / "run.json").write_text(json.dumps(st), encoding="utf-8")

    out = nr.reconcile(runs)
    assert out["resumed"] == []
    assert Run(root).status()["active"] is False
    assert no_side_effects[0] == []                         # nobody woken


@pytest.mark.parametrize("ended", [None, "", "not-a-date", 12345])
def test_unparseable_pause_timestamp_is_treated_as_stale(tmp_path, monkeypatch,
                                                        no_side_effects, ended):
    _window(monkeypatch, tmp_path, open_now=True)
    runs = tmp_path / "runs"
    root = _make_run(runs, "weird")
    r = Run(root)
    r.end(reason=nr.NIGHT_PAUSE)
    st = r.status()
    if ended is None:
        st.pop("ended", None)
    else:
        st["ended"] = ended
    (root / ".loom" / "run.json").write_text(json.dumps(st), encoding="utf-8")

    assert nr.reconcile(runs)["resumed"] == []
    assert no_side_effects[0] == []


def test_a_fresh_pause_is_resumed(tmp_path, monkeypatch, no_side_effects):
    """Control for the staleness guard: same path, recent timestamp."""
    _window(monkeypatch, tmp_path, open_now=True)
    runs = tmp_path / "runs"
    root = _make_run(runs, "lastnight")
    Run(root).end(reason=nr.NIGHT_PAUSE)          # ended = now
    assert nr.reconcile(runs)["resumed"] == ["lastnight"]


def test_noop_without_a_window(tmp_path, monkeypatch, no_side_effects):
    monkeypatch.setenv("LOOM_NIGHT_CONFIG", str(tmp_path / "absent.json"))
    runs = tmp_path / "runs"
    root = _make_run(runs, "alpha")
    out = nr.reconcile(runs)
    assert out == {"window": None, "paused": [], "resumed": [], "deferred": [],
                   "usage_fed": []}
    assert Run(root).status()["active"] is True             # untouched
