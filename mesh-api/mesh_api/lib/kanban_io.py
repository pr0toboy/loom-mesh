"""Read and write the Kanban board (a markdown file in the operator's notes).

The board is the SOURCE OF TRUTH for the operator's tasks: the web page edits it
through this layer, and agents edit it directly, as they would any other markdown
file. That is the whole point of keeping it a file — a task stays something an
agent can both read and write, which it would not be inside a closed app.

Three deliberate choices, each for a reason that was measured:

1. **We edit the LIST OF LINES, never a re-render of the file.** The board carries
   frontmatter, double blank lines between columns, and a `%% kanban:settings`
   block that the editor plugin reads back. A parse-then-render round trip would
   quietly normalise all of it away. Moving or ticking a card moves or rewrites
   ONE line; the rest is untouched down to the character.

2. **The store lock is taken with an explicit holder and with mirroring off.**
   - Without an agent name, the lock resolves its holder to `unknown` (there is no
     terminal session behind an API call), which makes the ownership log useless.
   - Mirroring off is the script's own documented escape hatch. This board is
     private, so it never reaches the shared mirror: republishing the whole set of
     notes on every card that moves would be pure waste, and slow enough to make
     the page unusable.
   The lock is held for milliseconds, so its staleness break-glass (five minutes,
   based on whether the holder's session still exists) is never reached.

3. **A backup before every write.** The real risk on this file is not an attacker,
   it is the slip of the hand that loses a task.
"""
import hashlib
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

VAULT = Path(os.environ.get("MESH_VAULT", ""))
BOARD = VAULT / "kanban-taches.md"
LOCK = VAULT / ".tools" / "vault-lock"
BACKUP_DIR = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh"))) / "backups" / "kanban"
KEEP_BACKUPS = 50

CARD_RE = re.compile(r"^- \[([ xX])\]\s+(.*)$")
HEAD_RE = re.compile(r"^##\s+(.+?)\s*$")
TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z0-9_\-]+)")
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


class BoardError(Exception):
    """A domain error — the route turns it into a 4xx, never a 500."""


# --------------------------------------------------------------------------- lock
class vault_lock:
    """The cooperative store lock, as a context manager.

    Deliberately strict: when `acquire` fails — it times out after 30 s because an
    agent is writing — this RAISES instead of writing anyway. Writing without the
    lock is exactly what produces the lost updates the lock exists to prevent.
    """

    _env = {**os.environ, "MESH_AGENT": "mesh-api", "VAULT_LOCK_NO_MIRROR": "1"}

    def __enter__(self):
        r = subprocess.run([str(LOCK), "acquire"], capture_output=True,
                           text=True, timeout=45, env=self._env)
        if r.returncode != 0:
            raise BoardError(
                "an agent is holding the store, try again in a few seconds "
                f"({(r.stdout + r.stderr).strip()[:200]})")
        return self

    def __exit__(self, *exc):
        subprocess.run([str(LOCK), "release"], capture_output=True,
                       text=True, timeout=45, env=self._env)
        return False


# -------------------------------------------------------------------------- parse
def _card_id(raw: str, seen: dict) -> str:
    """A stable id derived from the card's TEXT, not from its position.

    Intended consequence: moving or ticking a card does not change its id, so the
    client has nothing to reload — while rewriting its text does mint a new one.
    Two cards with identical text are told apart by the order they appear in.
    """
    base = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else f"{base}-{n + 1}"


def _clean(raw: str) -> str:
    t = WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1).split("/")[-1], raw)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    return TAG_RE.sub("", t).strip()


def read_board() -> dict:
    """{columns: [{title, cards: [{id, text, raw, done, tags}]}]} — lecture seule."""
    lines = BOARD.read_text(encoding="utf-8").splitlines()
    columns, cur, seen, in_settings = [], None, {}, False
    for line in lines:
        if line.strip().startswith("%%"):
            in_settings = not in_settings
            continue
        if in_settings:
            continue
        h = HEAD_RE.match(line)
        if h:
            cur = {"title": h.group(1), "cards": []}
            columns.append(cur)
            continue
        c = CARD_RE.match(line)
        if c and cur is not None:
            raw = c.group(2)
            cur["cards"].append({
                "id": _card_id(raw, seen),
                "text": _clean(raw),
                "raw": raw,
                "done": c.group(1).lower() == "x",
                "tags": [t.lower() for t in TAG_RE.findall(raw)],
            })
    return {"columns": columns}


def column_titles() -> list[str]:
    return [c["title"] for c in read_board()["columns"]]


# ------------------------------------------------------------------------ helpers
def _locate(lines: list[str], card_id: str) -> int:
    """The card's line index, replaying EXACTLY the numbering read_board used."""
    seen, in_settings = {}, False
    for i, line in enumerate(lines):
        if line.strip().startswith("%%"):
            in_settings = not in_settings
            continue
        if in_settings:
            continue
        c = CARD_RE.match(line)
        if c and _card_id(c.group(2), seen) == card_id:
            return i
    raise BoardError(f"no such card: {card_id} (the board changed, reload the page)")


def _column_bounds(lines: list[str], title: str) -> tuple[int, int]:
    """The column as (index of its heading, index just AFTER its last card)."""
    start = None
    for i, line in enumerate(lines):
        h = HEAD_RE.match(line)
        if h and h.group(1) == title:
            start = i
            break
    if start is None:
        raise BoardError(f"colonne inconnue : {title!r} (colonnes : {', '.join(column_titles())})")
    end = start + 1
    last_card = start
    for i in range(start + 1, len(lines)):
        if HEAD_RE.match(lines[i]) or lines[i].strip().startswith("%%"):
            break
        end = i + 1
        if CARD_RE.match(lines[i]):
            last_card = i
    return start, (last_card + 1 if last_card > start else start + 2)


def _backup(text: str) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    (BACKUP_DIR / f"kanban-taches.{stamp}.md").write_text(text, encoding="utf-8")
    olds = sorted(BACKUP_DIR.glob("kanban-taches.*.md"))
    for p in olds[:-KEEP_BACKUPS]:
        p.unlink(missing_ok=True)


def _save(lines: list[str], original: str) -> None:
    """An atomic write: a reader sees the old file or the new one, never half of either."""
    _backup(original)
    out = []
    for line in lines:
        if line.startswith("updated:"):
            line = f"updated: {date.today().isoformat()}"
        out.append(line)
    text = "\n".join(out) + "\n"
    tmp = BOARD.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, BOARD)


# ------------------------------------------------------------------------ mutations
def _mutate(fn):
    with vault_lock():
        original = BOARD.read_text(encoding="utf-8")
        lines = original.splitlines()
        fn(lines)
        _save(lines, original)
    return read_board()


def add_card(column: str, text: str, tag: str | None = None) -> dict:
    text = " ".join(text.split())
    if not text:
        raise BoardError("a card cannot be empty")
    if len(text) > 500:
        raise BoardError("card text is too long (500 characters max)")
    if "\n" in text:
        raise BoardError("a card must fit on one line")
    raw = f"{text} #{tag}" if tag and re.fullmatch(r"[a-zA-Z0-9_\-]{1,24}", tag) else text

    def op(lines):
        _, end = _column_bounds(lines, column)
        lines.insert(end, f"- [ ] {raw}")

    return _mutate(op)


def move_card(card_id: str, column: str) -> dict:
    def op(lines):
        i = _locate(lines, card_id)
        line = lines.pop(i)
        _, end = _column_bounds(lines, column)
        lines.insert(end, line)

    return _mutate(op)


def set_done(card_id: str, done: bool) -> dict:
    def op(lines):
        i = _locate(lines, card_id)
        lines[i] = CARD_RE.sub(lambda m: f"- [{'x' if done else ' '}] {m.group(2)}", lines[i])

    return _mutate(op)


def delete_card(card_id: str) -> dict:
    def op(lines):
        lines.pop(_locate(lines, card_id))

    return _mutate(op)
