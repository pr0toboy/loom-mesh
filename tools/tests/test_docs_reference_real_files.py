"""Every repository path the documentation names must exist.

This is the guard behind one rule: do not document what the repository does not
contain. It is not a style check — the README claimed a dashboard and a backend
that were in no branch, and `docs/getting-started.md` sent readers to scripts
that were never published. Someone following those instructions reached a
service that could not start, and concluded the project was abandoned.

Only paths that look like they belong to *this* repository are checked: a
backticked token containing a slash, whose first segment is a directory that
exists here. Deployment paths (`~/mesh/...`, `/etc/...`), globs and placeholders
are left alone — they are examples, not claims about the contents.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# Every markdown in the repository, not only the top-level README and docs/: a
# subdirectory README is the first page someone lands on when browsing the tree,
# and one of them pointed at a screenshot that was never committed — a broken
# image on the front page of a folder.
# Hidden directories are skipped: `.pytest_cache` ships a README of its own, so
# a local cache silently added two parametrised cases that a fresh clone does not
# have — a suite whose size depends on whether you ran it before.
DOCS = sorted(p for p in REPO.rglob("*.md")
              if not any(part.startswith(".") for part in p.parts)
              and "node_modules" not in p.parts)
BACKTICKED = re.compile(r"`([^`\n]+)`")
#: Markdown links and images: `![alt](path)` / `[text](path)`.
LINKED = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
TOP_LEVEL = {p.name for p in REPO.iterdir() if p.is_dir() and not p.name.startswith(".")}
# A path the repository deliberately does not track (a token file, an operator's
# own config) is something the reader creates, not something we claim to ship.
IGNORED = {line.strip().rstrip("/") for line in
           (REPO / ".gitignore").read_text().splitlines()
           if line.strip() and not line.startswith("#")}


def is_deployment_file(path: str) -> bool:
    return path in IGNORED or Path(path).name in IGNORED


def repo_paths(text: str) -> set[str]:
    found = set()
    for token in BACKTICKED.findall(text):
        token = token.strip().rstrip(".,;:)")
        if "/" not in token or token.startswith(("~", "/", "http")):
            continue
        if any(c in token for c in "*{}<>$ "):
            continue
        if token.split("/", 1)[0] not in TOP_LEVEL:
            continue
        found.add(token)
    return found


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_documented_paths_exist(doc: Path):
    missing = sorted(p for p in repo_paths(doc.read_text(encoding="utf-8"))
                     if not (REPO / p).exists() and not is_deployment_file(p))
    assert not missing, (
        f"{doc.relative_to(REPO)} refers to files this repository does not contain: "
        f"{missing}. Ship them, or stop promising them."
    )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_linked_files_and_images_exist(doc: Path):
    """Relative links and images, resolved from the document that carries them.

    A backticked path is a claim about the contents; a link is a promise the
    reader will click. Anchors, external URLs and mail links are left alone.
    """
    broken = []
    for target in LINKED.findall(doc.read_text(encoding="utf-8")):
        target = target.split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:", "/")):
            continue
        resolved = (doc.parent / target).resolve()
        if not resolved.exists() and not is_deployment_file(target):
            broken.append(target)
    assert not broken, (
        f"{doc.relative_to(REPO)} links to files that do not exist: {sorted(broken)}")
