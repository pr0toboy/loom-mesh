"""Tests for the loom CLI — drives the board the way agents do from the shell."""
import json
import subprocess

from autonomy.board import Board
from autonomy.cli import main


def _run(root, *argv, agent="worker-a", monkeypatch=None):
    monkeypatch.setenv("LOOM_ROOT", str(root))
    monkeypatch.setenv("MESH_AGENT", agent)
    monkeypatch.delenv("TMUX", raising=False)
    return main(list(argv))


def test_run_start_status_end(tmp_path, monkeypatch, capsys):
    _run(tmp_path, "run", "start", "r1", "--participants", "worker-a,worker-b",
         "--goal", "ship it", "--cap", "6700000", monkeypatch=monkeypatch)
    capsys.readouterr()
    _run(tmp_path, "--json", "run", "status", monkeypatch=monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert out["active"] is True
    assert out["participants"] == ["worker-a", "worker-b"]
    assert out["guardrails"]["window_token_cap"] == 6700000
    _run(tmp_path, "run", "end", "--reason", "done", monkeypatch=monkeypatch)


def test_task_flow_through_cli(tmp_path, monkeypatch, capsys):
    _run(tmp_path, "task", "new", "T1", "--title", "build X", "--scope", "py",
         monkeypatch=monkeypatch)
    _run(tmp_path, "task", "claim", "T1", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "progress", "T1", "-m", "scaffolded", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "submit", "T1", "--branch", "feat/x", monkeypatch=monkeypatch)
    capsys.readouterr()
    t = Board(tmp_path).tasks()["T1"]
    assert t["status"] == "in_review"
    assert t["owner"] == "worker-a"
    assert t["branch"] == "feat/x"
    assert t["attempts"] == 1


def test_review_pass_marks_done(tmp_path, monkeypatch, capsys):
    _run(tmp_path, "task", "new", "T1", "--owner", "worker-a", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "submit", "T1", "--branch", "b", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "review", "T1", "--pass", agent="lead", monkeypatch=monkeypatch)
    capsys.readouterr()
    assert Board(tmp_path).tasks()["T1"]["status"] == "done"


def test_mine_lists_open_tasks(tmp_path, monkeypatch, capsys):
    _run(tmp_path, "task", "new", "T1", "--owner", "worker-a", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "new", "T2", "--owner", "worker-b", monkeypatch=monkeypatch)
    capsys.readouterr()
    _run(tmp_path, "mine", agent="worker-a", monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert "T1" in out and "T2" not in out


def test_task_worktree_creates_isolated_checkout(tmp_path, monkeypatch, capsys):
    # worktree needs a real git repo at the root
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    (tmp_path / "seed").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)

    _run(tmp_path, "task", "new", "T1", "--title", "x", monkeypatch=monkeypatch)
    capsys.readouterr()
    _run(tmp_path, "task", "worktree", "T1", monkeypatch=monkeypatch)
    path = capsys.readouterr().out.strip()
    assert path.endswith(".loom/worktrees/T1")
    assert (tmp_path / ".loom" / "worktrees" / "T1" / "seed").exists()
    # board recorded the worktree/branch
    assert "worktree_created" in [e["type"] for e in Board(tmp_path).events()]


def test_as_flag_overrides_identity_over_ssh(tmp_path, monkeypatch, capsys):
    # Simulate the coordinator driving the board over SSH→WSL: no $TMUX and no
    # MESH_AGENT in the environment. Without --as this recorded 'unknown'.
    monkeypatch.setenv("LOOM_ROOT", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("MESH_AGENT", raising=False)
    monkeypatch.delenv("LOOM_AGENT", raising=False)
    main(["--as", "agent-1", "task", "new", "T1", "--title", "decompose"])
    capsys.readouterr()
    ev = Board(tmp_path).events()[-1]
    assert ev["type"] == "task_created" and ev["by"] == "agent-1"


def test_unknown_identity_without_any_signal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOOM_ROOT", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("MESH_AGENT", raising=False)
    monkeypatch.delenv("LOOM_AGENT", raising=False)
    main(["task", "new", "T1"])
    capsys.readouterr()
    assert Board(tmp_path).events()[-1]["by"] == "unknown"


def test_loom_agent_env_is_a_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOOM_ROOT", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("MESH_AGENT", raising=False)
    monkeypatch.setenv("LOOM_AGENT", "agent-4")
    main(["task", "new", "T1"])
    capsys.readouterr()
    assert Board(tmp_path).events()[-1]["by"] == "agent-4"


def test_log_free_event(tmp_path, monkeypatch, capsys):
    _run(tmp_path, "log", "skill_created", "--field", "name=/deploy",
         "--field", "by_task=T1", monkeypatch=monkeypatch)
    capsys.readouterr()
    ev = Board(tmp_path).events()[-1]
    assert ev["type"] == "skill_created" and ev["name"] == "/deploy"


def test_cli_reopen_abandon_deps(tmp_path, monkeypatch, capsys):
    # reopen a done task
    _run(tmp_path, "task", "new", "T1", "--owner", "worker-a", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "submit", "T1", "--branch", "b", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "review", "T1", "--pass", agent="carol", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "reopen", "T1", "-m", "gate", "--to", "in_progress",
         agent="coordinator", monkeypatch=monkeypatch)
    capsys.readouterr()
    assert Board(tmp_path).tasks()["T1"]["status"] == "in_progress"

    # deps replacement
    _run(tmp_path, "task", "new", "T2", "--deps", "A,B", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "deps", "T2", "--deps", "C", agent="coordinator", monkeypatch=monkeypatch)
    capsys.readouterr()
    assert Board(tmp_path).tasks()["T2"]["deps"] == ["C"]

    # abandon → terminal
    _run(tmp_path, "task", "new", "T3", "--owner", "worker-a", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "abandon", "T3", "-m", "dead", agent="coordinator", monkeypatch=monkeypatch)
    capsys.readouterr()
    assert Board(tmp_path).tasks()["T3"]["status"] == "abandoned"


def test_cli_claim_is_guarded(tmp_path, monkeypatch, capsys):
    # M1: two agents claiming the same task via the CLI — only one wins; the loser
    # gets a null claim, not a second successful task_claimed.
    _run(tmp_path, "task", "new", "T1", monkeypatch=monkeypatch)
    _run(tmp_path, "task", "claim", "T1", agent="worker-a", monkeypatch=monkeypatch)
    capsys.readouterr()
    _run(tmp_path, "--json", "task", "claim", "T1", agent="worker-b", monkeypatch=monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert out.get("claimed") is None            # loser gets a no-op
    t = Board(tmp_path).tasks()["T1"]
    assert t["owner"] == "worker-a"              # first claimer keeps it
    claims = [e for e in Board(tmp_path).events() if e.get("type") == "task_claimed"]
    assert len(claims) == 1                       # never two effective claims
