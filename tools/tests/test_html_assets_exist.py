"""Every local file an HTML page loads must be in the repository.

Sibling of `test_docs_reference_real_files.py`, and it exists because that guard
only reads markdown. `dashboard-web/index.html` loaded `d3.v7.min.js` — a file
that lived in the operator's deployment and was in no commit. The page still
served with HTTP 200 and the agent cards still rendered, so nothing looked
broken; the graph, the one thing the dashboard exists to show, was a black
rectangle in every fresh clone. A missing script is silent in a way a missing
markdown link is not: the browser reports it to a console no one is reading.

Only same-repository references are checked. Schemes (`http:`, `data:`),
fragments, absolute URL paths (`/ui/...` is a route, not a file) and template
expressions (`${...}`, `{{...}}`) are claims about runtime, not about the tree.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HTML = sorted(p for p in REPO.rglob("*.html")
              if not any(part.startswith(".") for part in p.parts)
              and "node_modules" not in p.parts)
#: `src="a"`, `src='a'` and bare `src=a` — the 2026-09-09 gate pointed out that
#: only the first form was seen, so switching a quote style would have hidden the
#: very defect this file exists to catch.
REF = re.compile(r"""(?:src|href)\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s"'<>`]+))""")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def _local_refs(html: Path) -> list[str]:
    out = []
    for groups in REF.findall(html.read_text(encoding="utf-8")):
        ref = next((g for g in groups if g), "").strip()
        if not ref or ref.startswith(("#", "/")) or SCHEME.match(ref):
            continue
        if "${" in ref or "{{" in ref:      # built at runtime, not a file claim
            continue
        out.append(ref.split("?")[0].split("#")[0])
    return out


CASES = [(h, r) for h in HTML for r in _local_refs(h)]


def test_at_least_one_html_is_scanned():
    """A regex that silently matches nothing would make this file always pass.

    Both halves matter, and the second was missing: with zero CASES the
    parametrised test below is *skipped*, and a skipped test reports green. So
    assert on the extracted references too, not only on the files.
    """
    assert HTML, "no HTML found in the repository — the guard is scanning nothing"
    assert CASES, (
        "no local asset reference was extracted from any HTML: either the pages "
        "genuinely load none, or the regex stopped matching — and the second case "
        "would leave this guard green forever"
    )


@pytest.mark.parametrize("html,ref", CASES,
                         ids=[f"{h.relative_to(REPO)}:{r}" for h, r in CASES])
def test_html_asset_is_committed(html: Path, ref: str):
    target = (html.parent / ref).resolve()
    assert target.is_file(), (
        f"{html.relative_to(REPO)} loads {ref!r}, which is not in the repository. "
        "The page will serve with HTTP 200 and the asset will 404 in silence."
    )
