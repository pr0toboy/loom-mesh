"""Tests for the making-of narrative generator."""
import json

from autonomy.board import Board
from autonomy.making_of import (
    filter_discussion, load_jsonl_messages, render,
)
from autonomy.run import Run


def _build_a_run(root):
    r = Run(root)
    r.start("r1", ["worker-a", "worker-b"], goal="ship a tiny CLI")
    b = Board(root)
    b.create_task("T1", "parser", scope="py", owner="worker-a")
    b.claim("T1", "worker-a")
    b.progress("T1", "worker-a", note="wrote argparse")
    b.log("skill_created", by="worker-a", name="/lint", why="enforce style in CI")
    b.submit("T1", "worker-a", branch="feat/parser", artifacts=["cli.py"])
    b.review("T1", "lead", passed=True)
    b.create_task("T2", "tests", owner="worker-b")
    b.block("T2", "worker-b", "fixtures missing")
    b.unblock("T2", by="lead")
    b.log("commit", by="worker-a", sha="abc1234", message="parser")
    r.end(reason="converged")
    return root


def test_render_contains_core_sections(tmp_path):
    md = render(_build_a_run(tmp_path))
    assert "# Making-of — ship a tiny CLI" in md
    assert "## Run" in md
    assert "## What was built" in md
    assert "## Tooling the agents created for themselves" in md
    assert "## Blockers & decisions" in md
    assert "## Timeline" in md


def test_render_reports_tasks_and_tooling(tmp_path):
    md = render(_build_a_run(tmp_path))
    assert "T1" in md and "parser" in md
    assert "feat/parser" in md
    assert "`cli.py`" in md
    assert "/lint" in md and "enforce style in CI" in md  # skill documented
    assert "Participants**: worker-a, worker-b" in md
    assert "1/2 done" in md                                 # T1 done, T2 not


def test_render_documents_blockers(tmp_path):
    md = render(_build_a_run(tmp_path))
    assert "fixtures missing" in md
    assert "unblocked by lead" in md


def test_render_empty_run_is_safe(tmp_path):
    Run(tmp_path).start("r1", [], goal="nothing yet")
    md = render(tmp_path)
    assert "No tasks were recorded" in md


# --- mesh conversation weaving ---------------------------------------------

MESH = [
    {"from": "outsider", "to": "x", "ts": "2026-06-05T09:00:00", "body": "before the run"},
    {"from": "worker-a", "to": "worker-b", "ts": "2026-06-05T10:05:00", "body": "I'll take the parser"},
    {"from": "worker-b", "to": "worker-a", "ts": "2026-06-05T10:06:00", "body": "ok, I do the tests"},
    {"from": "worker-a", "to": "stranger", "ts": "2026-06-05T11:00:00", "body": "after the window"},
]


def _run_with_window(root):
    r = Run(root)
    r.start("r1", ["worker-a", "worker-b"], goal="ship a CLI")
    # stamp a window by ending it; started is set by start()
    r.end(reason="converged")
    # force a known window for deterministic filtering
    state = r.status()
    state["started"] = "2026-06-05T10:00:00"
    state["ended"] = "2026-06-05T10:30:00"
    r._write(state)
    Board(root).create_task("T1", "parser", owner="worker-a")
    return root


def test_filter_discussion_window_and_participants():
    kept = filter_discussion(MESH, ["worker-a", "worker-b"],
                             since="2026-06-05T10:00:00", until="2026-06-05T10:30:00")
    bodies = [m["body"] for m in kept]
    assert bodies == ["I'll take the parser", "ok, I do the tests"]
    # outsider-before and after-window are both excluded


def test_filter_dedupes_same_message():
    dup = MESH[1:2] * 3
    assert len(filter_discussion(dup, ["worker-a"])) == 1


def test_filter_window_is_timezone_aware():
    # The run window is stamped in UTC; the mesh stamps in local time (+02:00).
    # The same instant must compare equal regardless of offset — a raw string
    # compare would wrongly drop these (e.g. "10:05+02:00" > "09:00+00:00").
    msgs = [
        {"from": "worker-a", "to": "worker-b",
         "ts": "2026-06-05T10:05:00+02:00", "body": "inside (= 08:05 UTC)"},
        {"from": "worker-a", "to": "worker-b",
         "ts": "2026-06-05T11:30:00+02:00", "body": "after (= 09:30 UTC)"},
        {"from": "worker-a", "to": "worker-b",
         "ts": "2026-06-05T09:30:00+02:00", "body": "before (= 07:30 UTC)"},
    ]
    kept = filter_discussion(
        msgs, ["worker-a", "worker-b"],
        since="2026-06-05T08:00:00+00:00", until="2026-06-05T09:00:00+00:00",
    )
    assert [m["body"] for m in kept] == ["inside (= 08:05 UTC)"]


def test_render_weaves_mesh_conversation(tmp_path):
    root = _run_with_window(tmp_path)
    md = render(root, discussion=MESH)
    assert "## Conversation (mesh)" in md
    assert "worker-a → worker-b" in md
    assert "I'll take the parser" in md
    assert "before the run" not in md       # outside window/participants
    assert "after the window" not in md


def test_render_without_discussion_has_no_mesh_section(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"], goal="g")
    md = render(tmp_path)
    assert "## Conversation (mesh)" not in md


def test_load_jsonl_messages(tmp_path):
    f = tmp_path / "inbox-worker-b.jsonl"
    f.write_text("\n".join(json.dumps(m) for m in MESH[1:3]) + "\ntorn{line\n")
    msgs = load_jsonl_messages([f, tmp_path / "missing.jsonl"])
    assert [m["body"] for m in msgs] == ["I'll take the parser", "ok, I do the tests"]
