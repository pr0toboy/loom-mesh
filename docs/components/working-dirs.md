# Working directories per ticket

Every non-trivial ticket gets a **scoped working directory** with structured files. The agent works "inside" that directory rather than from raw working memory.

## Why

A ticket's prompt can be 200+ lines (briefs, multi-step plans, code samples). The agent's working memory can hold it for a while, but:

- The conversation may be **compacted** mid-task, losing the brief.
- The session may **crash** and need to restart from scratch.
- The progress is invisible to anyone but the agent (no audit trail).
- The intermediate artifacts (logs, diffs, screenshots, decisions) end up scattered across the conversation transcript.

A working directory solves all four by giving each ticket a small, durable filesystem footprint that survives sessions, crashes, and compacts.

## Layout

When a ticket is created (`POST /tickets`), the dispatcher scaffolds:

```
~/mesh/tickets/<agent>/working/<tk-id>/
├── brief.md      # the prompt extracted from the ticket JSON, readable
├── todo.md       # template with To do / In progress / Done sections
├── notes.md      # empty; agent drops intermediate artifacts here
└── output.md     # empty; agent fills the final TL;DR + details here
```

The directory is **independent** of the state directories (draft/armed/queued/running/done). State transitions happen through filesystem moves of the JSON file; the working directory is moved alongside the JSON only on terminal states (done/failed/cancelled), where it's archived next to the final JSON.

## Lifecycle

```
1. POST /tickets → create JSON + scaffold working/<tk-id>/ with empty files
2. user arms ticket → no change to working dir
3. armed → queued → extract `prompt` from JSON, write into brief.md
4. queued → running → no change to working dir (agent will fill it)
5. agent works:
     - reads brief.md to know what to do
     - maintains todo.md with checkboxes (3 sections: "To do", "In progress", "Done")
     - drops intermediate artifacts (logs, diffs, refs) into notes.md
6. agent completes → runs ticket-complete.py:
     - writes the short TL;DR into output.md (alongside details)
     - moves working/<tk-id>/ to done/<tk-id>/
     - moves running/<tk-id>.json to done/<tk-id>.json
```

If the agent's session ends abruptly (the `Stop` hook fires while a ticket is in `running/`), the fallback handler moves both the JSON and the working directory to `failed/<tk-id>/`. No data loss.

## Template: `todo.md`

```markdown
# Todo — tk-<id>

## To do
- [ ] (filled by agent at start)

## In progress
- [ ] (active items)

## Done
- [x] (completed items, kept as audit trail)
```

The 3-section model is simpler than Kanban-style boards and adequate for an agent's working memory. Items move between sections by editing the markdown. The Done section keeps a record visible to anyone reading the directory later.

## Template: `brief.md`

Filled automatically by the dispatcher at the `armed → queued` transition:

```markdown
# Brief — tk-<id>

**From**: <sender>
**To**: <agent>
**Created**: <iso8601>
**Priority**: <low|normal|urgent>

---

<the prompt content, as-is>
```

The agent reads this file as the canonical source of "what was asked of me", instead of relying on its working memory or scrolling back through the inbox.

## Template: `output.md`

Filled by the agent at completion. Convention:

```markdown
# Output — tk-<id>

**Status**: done | failed | cancelled
**Completed at**: <iso8601>

## TL;DR

(1-3 sentences, matches the `--tldr` argument passed to ticket-complete.py)

## Detailed report

(longer, if the agent wants to document its work)

## Artifacts produced

- path/to/file-1
- path/to/file-2
- ...

## Open questions / follow-ups

- (anything left for the next agent or for the user)
```

The webui's ticket detail view renders `output.md` (rather than just the short TL;DR), giving the user the full picture without needing to SSH into the host.

## Agent-side practice

This is what each agent's charter must say:

> When I receive a ticket whose estimate exceeds 30 minutes or whose brief exceeds 50 lines:
> 1. `cd ~/mesh/tickets/<self>/working/<tk-id>/` (the dispatcher scaffolds it on creation)
> 2. Read `brief.md`
> 3. Fill `todo.md` with my breakdown into checkbox items, three sections (To do / In progress / Done)
> 4. Check / move items as I progress
> 5. Drop intermediate artifacts (logs, diffs, screenshots) into `notes.md`
> 6. At the end, write the detailed `output.md`
> 7. Close the ticket with `ticket-complete.py <tk-id> --tldr "..."` (the short TL;DR; the detail lives in `output.md`)

Charters of trivial tickets (e.g., "ACK", "ping", quick reply) **don't need** this — the working directory remains empty, gets archived as-is. The pattern auto-scales: heavy tickets get full directories, light tickets get scaffolded but unused directories.

## Sub-tickets

A ticket's working directory is also where the agent decides to spawn sub-tickets when the work is too large for one ticket. The sub-tickets carry `parent_ticket_id=<my-current-ticket>` so the webui can render the parent→children tree.

Example: an agent receives a ticket "build feature X" (5 components). It creates 5 sub-tickets, one per component, each with `parent_ticket_id=tk-X`. Some sub-tickets may target other agents (delegation). The user sees the breakdown in the webui and can monitor progress per sub-item rather than as a single opaque ticket.

## Why not just a notes file in the agent's home?

The working directory lives **next to the ticket state**. This means:

- A `ls ~/mesh/tickets/<agent>/done/` shows finished tickets with their work directories.
- A `cat ~/mesh/tickets/<agent>/done/<tk-id>/output.md` retrieves any past result.
- Backup of `~/mesh/` includes the work history.
- The API can serve the contents of these files via a `/tickets/<agent>/<tk-id>/files` endpoint.

If working dirs lived in the agent's home, none of this would be automatic.

## Implementation notes

The scaffolding is centralized in the dispatcher:

```python
# mesh_api/lib/tickets_io.py (sketch)
WORKING_DIR = TICKETS_DIR / "{agent}" / "working" / "{tk_id}"

def scaffold_working_dir(tk_id: str, agent: str, prompt: str | None = None):
    d = TICKETS_DIR / agent / "working" / tk_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "brief.md").write_text(prompt or "(brief pending — ticket still in draft state)\n")
    (d / "todo.md").write_text(TODO_TEMPLATE.format(tk_id=tk_id))
    (d / "notes.md").write_text("")
    (d / "output.md").write_text("")
    # 0755 dir, 0644 files (no secrets expected inside)
    d.chmod(0o755)
    for f in d.iterdir():
        f.chmod(0o644)
```

`ticket-complete.py` moves the directory atomically:

```python
import shutil
src = WORKING_DIR / agent / "working" / tk_id
dst = WORKING_DIR / agent / "done" / tk_id  # alongside the .json
if src.exists() and not dst.exists():
    shutil.move(str(src), str(dst))
```

`shutil.move` falls back to copy+delete across filesystems, but everything stays on the same mount so it's a rename — atomic.

## Tradeoffs

- **More disk I/O per ticket**: scaffolding 4 files at creation. Negligible (< 5 ms).
- **More clutter in `tickets/<agent>/`**: each ticket has both a JSON and a directory. Manageable with `ls -F` (directories vs files visible).
- **The agent must remember to `cd` into the dir**: enforced by the charter, not the code. Loose convention.

These are acceptable. The pattern's value comes from the durability of work artifacts and the readability for humans + other agents.
