"""Tests for the run lifecycle / engine-active switch."""
from autonomy.run import Run


def test_inactive_by_default(tmp_path):
    r = Run(tmp_path)
    assert r.active() is False
    assert r.participants() == []


def test_start_marks_active_with_participants_and_guardrails(tmp_path):
    r = Run(tmp_path)
    r.start("run-1", ["worker-a", "worker-b"], goal="ship a CLI",
            guardrails={"window_token_cap": 6_700_000, "max_iterations": 200})
    assert r.active() is True
    assert r.participants() == ["worker-a", "worker-b"]
    assert r.goal() == "ship a CLI"
    assert r.guardrails()["max_iterations"] == 200


def test_end_clears_active_but_keeps_record(tmp_path):
    r = Run(tmp_path)
    r.start("run-1", ["worker-a"])
    r.end(reason="converged")
    assert r.active() is False
    assert r.status()["end_reason"] == "converged"


def test_state_survives_new_instance(tmp_path):
    Run(tmp_path).start("run-1", ["worker-a"], goal="g")
    assert Run(tmp_path).goal() == "g"


def test_write_is_atomic_no_temp_file_left_and_no_partial_write(tmp_path):
    r = Run(tmp_path)
    r.start("run-1", ["worker-a"], goal="g")
    tmp = r.path.with_suffix(".json.tmp")
    assert not tmp.exists()  # os.replace leaves no leftover temp file
    # a run.json that was never fully written (truncated) must fail-open, not crash
    r.path.write_text("{\"active\": true, \"partic", encoding="utf-8")
    assert r.status() == {"active": False}
    assert r.active() is False
