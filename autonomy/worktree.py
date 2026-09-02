#!/usr/bin/env python3
"""Per-task git worktrees — the parallelism guardrail.

When several agents work a run at once, they must not share one checkout: agent A
editing `src/x.py` for task T1 while agent B edits `src/y.py` for task T2 in the
same working tree would clobber each other and make a clean per-task branch
impossible. The fix is a **git worktree per task**: each task gets its own
directory under ``<repo>/.loom/worktrees/<task-id>`` checked out on its own
branch (default ``loom/<task-id>``), branched from a shared base. The agent works
there; the coordinator merges the branch into the integration branch once the
task passes review. Worktrees are removed after merge.

This is the same isolation the cross-host design already gets from separate
branches, extended to several agents on one host.

Stdlib + the ``git`` binary. Each helper fails loudly (raises) on a real git
error, but is idempotent: ``ensure`` on an existing worktree just returns it,
``remove`` on a missing one is a no-op.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def default_branch(task_id: str) -> str:
    return f"loom/{task_id}"


def worktrees_dir(repo_root) -> Path:
    return Path(repo_root) / ".loom" / "worktrees"


def worktree_path(repo_root, task_id: str) -> Path:
    return worktrees_dir(repo_root) / task_id


def _git(repo_root, *args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, check=check,
    )


def _branch_exists(repo_root, branch: str) -> bool:
    r = _git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return r.returncode == 0


def _worktree_registered(repo_root, path: Path) -> bool:
    r = _git(repo_root, "worktree", "list", "--porcelain", check=False)
    target = str(path.resolve())
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            if str(Path(line[len("worktree "):]).resolve()) == target:
                return True
    return False


def ensure(repo_root, task_id: str, branch: str | None = None, base: str = "HEAD") -> Path:
    """Ensure a worktree for ``task_id`` exists, checked out on ``branch``
    (default ``loom/<task_id>``), creating the branch from ``base`` if needed.
    Returns the worktree path. Idempotent."""
    branch = branch or default_branch(task_id)
    path = worktree_path(repo_root, task_id)
    if _worktree_registered(repo_root, path):
        if path.is_dir():
            return path
        # registered but the directory was deleted by hand → prune the stale entry
        # and fall through to recreate, instead of handing back a phantom path.
        _git(repo_root, "worktree", "prune", check=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo_root, branch):
        _git(repo_root, "worktree", "add", str(path), branch)
    else:
        # create the branch off base in the new worktree
        _git(repo_root, "worktree", "add", "-b", branch, str(path), base)
    return path


def remove(repo_root, task_id: str, force: bool = True) -> bool:
    """Remove a task's worktree. No-op if it isn't registered. Returns whether a
    worktree was removed. The branch itself is left intact (it may still need
    merging or inspection)."""
    path = worktree_path(repo_root, task_id)
    if not _worktree_registered(repo_root, path):
        _git(repo_root, "worktree", "prune", check=False)
        return False
    args = ["worktree", "remove", str(path)]
    if force:
        args.append("--force")
    _git(repo_root, *args)
    _git(repo_root, "worktree", "prune", check=False)
    return True


def prune(repo_root) -> None:
    _git(repo_root, "worktree", "prune", check=False)


def branch_gc(repo_root, integration_branch: str) -> dict:
    """Post-convergence cleanup: remove every loom worktree of the run, then delete
    the ``loom/*`` branches that are **fully merged** into ``integration_branch``.
    Never touches an unmerged branch (``git branch -d`` refuses those). Returns
    ``{"removed_worktrees": [...], "deleted_branches": [...]}``."""
    removed: list[str] = []
    for w in listing(repo_root):
        tid = Path(w["path"]).name
        if remove(repo_root, tid):
            removed.append(tid)

    deleted: list[str] = []
    r = _git(repo_root, "branch", "--merged", integration_branch, check=False)
    for line in r.stdout.splitlines():
        name = line.replace("*", "").strip()
        if name and name.startswith("loom/") and name != integration_branch:
            d = _git(repo_root, "branch", "-d", name, check=False)  # -d = only if merged
            if d.returncode == 0:
                deleted.append(name)
    return {"removed_worktrees": sorted(removed), "deleted_branches": sorted(deleted)}


def listing(repo_root) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into ``[{path, branch, head}]``,
    limited to the loom worktrees under ``.loom/worktrees``."""
    r = _git(repo_root, "worktree", "list", "--porcelain", check=False)
    out: list[dict] = []
    cur: dict = {}
    root = str(worktrees_dir(repo_root).resolve())
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            if cur:
                out.append(cur)
            cur = {"path": line[len("worktree "):]}
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].replace("refs/heads/", "")
        elif line == "" and cur:
            out.append(cur)
            cur = {}
    if cur:
        out.append(cur)
    return [w for w in out if str(Path(w["path"]).resolve()).startswith(root)]
