"""Tests for per-task git worktree isolation (uses a real temp git repo)."""
import subprocess

import pytest

from autonomy import worktree


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    # explicit initial branch so the fixture is deterministic regardless of the
    # host's init.defaultBranch (a `main` default would collide with the branch_gc
    # test's integration branch).
    _git(tmp_path, "init", "-q", "-b", "master")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("seed\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def test_ensure_creates_worktree_and_branch(repo):
    path = worktree.ensure(repo, "T1")
    assert path.is_dir()
    assert (path / "README.md").exists()              # checked out from base
    # the default branch loom/T1 now exists and the worktree is on it
    wts = {w["path"]: w.get("branch") for w in worktree.listing(repo)}
    assert str(path) in [p for p in wts] or any(str(path) in p for p in wts)
    assert any(w.get("branch") == "loom/T1" for w in worktree.listing(repo))


def test_ensure_is_idempotent(repo):
    p1 = worktree.ensure(repo, "T1")
    p2 = worktree.ensure(repo, "T1")
    assert p1 == p2
    assert len(worktree.listing(repo)) == 1


def test_two_tasks_get_isolated_worktrees(repo):
    a = worktree.ensure(repo, "T1")
    b = worktree.ensure(repo, "T2")
    assert a != b
    # edits in one don't appear in the other
    (a / "a.txt").write_text("from T1\n")
    assert not (b / "a.txt").exists()
    branches = {w.get("branch") for w in worktree.listing(repo)}
    assert {"loom/T1", "loom/T2"} <= branches


def test_ensure_reuses_existing_branch(repo):
    # pre-create a branch, then ensure a worktree should check it out, not fail
    _git(repo, "branch", "loom/T9")
    path = worktree.ensure(repo, "T9")
    assert path.is_dir()
    assert any(w.get("branch") == "loom/T9" for w in worktree.listing(repo))


def test_custom_branch_name(repo):
    worktree.ensure(repo, "T1", branch="feature/custom")
    assert any(w.get("branch") == "feature/custom" for w in worktree.listing(repo))


def test_remove_is_noop_when_absent(repo):
    assert worktree.remove(repo, "nope") is False


def test_remove_cleans_up_but_keeps_branch(repo):
    worktree.ensure(repo, "T1")
    assert worktree.remove(repo, "T1") is True
    assert worktree.listing(repo) == []
    # branch survives for later merge/inspection
    r = subprocess.run(["git", "-C", str(repo), "branch", "--list", "loom/T1"],
                       capture_output=True, text=True)
    assert "loom/T1" in r.stdout


def test_work_on_branch_is_committable(repo):
    path = worktree.ensure(repo, "T1")
    (path / "x.py").write_text("print('hi')\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "work")
    # the commit landed on loom/T1, not on the base branch
    base_log = _git(repo, "log", "--oneline").stdout
    assert "work" not in base_log
    branch_log = _git(repo, "log", "--oneline", "loom/T1").stdout
    assert "work" in branch_log


def test_ensure_recreates_phantom_worktree(repo):
    # registered worktree whose directory was deleted by hand → recreate, not phantom
    import shutil
    path = worktree.ensure(repo, "T1")
    assert path.is_dir()
    shutil.rmtree(path)                     # remove the checkout behind git's back
    path2 = worktree.ensure(repo, "T1")     # must prune + recreate a working tree
    assert path2 == path and path2.is_dir()
    assert (path2 / "README.md").exists()


def test_branch_gc_deletes_merged_keeps_unmerged(repo):
    # merged branch: create a worktree, commit, merge into main, then gc
    m = worktree.ensure(repo, "MERGED")
    (m / "m.txt").write_text("merged work\n")
    _git(m, "add", "-A")
    _git(m, "commit", "-q", "-m", "merged work")
    _git(repo, "checkout", "-q", "-b", "main")   # integration branch
    _git(repo, "merge", "-q", "loom/MERGED")

    # unmerged branch: has a commit never merged into main
    u = worktree.ensure(repo, "UNMERGED")
    (u / "u.txt").write_text("unmerged\n")
    _git(u, "add", "-A")
    _git(u, "commit", "-q", "-m", "unmerged work")

    out = worktree.branch_gc(repo, "main")
    assert set(out["removed_worktrees"]) == {"MERGED", "UNMERGED"}   # all worktrees removed
    assert "loom/MERGED" in out["deleted_branches"]                 # merged → deleted
    assert "loom/UNMERGED" not in out["deleted_branches"]           # unmerged → kept

    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list"],
                              capture_output=True, text=True).stdout
    assert "loom/MERGED" not in branches
    assert "loom/UNMERGED" in branches
