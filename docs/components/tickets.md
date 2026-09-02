# Tickets — async task primitive

A **ticket** is a distinct primitive from a **message** (see [bus.md](bus.md)). Messages are conversational chatter; tickets are structured units of work with a documented lifecycle, dependencies, and a TL;DR-on-completion contract.

## Why a separate primitive

Messages and tickets *could* be one primitive. They're not, for three reasons:

1. **Lifecycle**: a message is a one-shot append; a ticket goes through a state machine.
2. **Discoverability**: a ticket sitting in `running/` answers "what is this agent doing right now?" with a single `ls`. Messages don't.
3. **Contract on completion**: a ticket promises a TL;DR (and ideally an `output.md` working file). Messages promise nothing.

The two share the same bus mechanics (push via watcher, ACK protocol) but are stored differently and managed by a separate dispatcher service.

## Ticket shape

A ticket is a single JSON file on disk:

```json
{
  "id": "tk-abc123",
  "ts_created": "2026-05-25T14:30:00+01:00",
  "from": "<sender>",
  "to": "<assignee-agent>",
  "prompt": "...",
  "dispatch_mode": "draft" | "armed",
  "depends_on": ["tk-other1", "tk-other2"],
  "parent_ticket_id": "tk-parent" | null,
  "started_at": null,
  "completed_at": null,
  "tldr": null,
  "pilot_notified": false
}
```

Fields:
- `id` — `tk-` + 6 hex chars (e.g., `tk-abc123`). Strict regex enforced by API (`^tk-[0-9a-f]{6}$`) to prevent wildcard glob attacks.
- `from` — who created it (agent, user-app, etc.).
- `to` — the assignee. Must be in `ALL_PEERS`.
- `prompt` — the actual work description, often long markdown. Capped at 64 KiB.
- `dispatch_mode` — `draft` (created, not yet armed) or `armed` (eligible for dispatch). Default `draft`.
- `depends_on` — list of other ticket IDs (cross-agent OK) that must reach `done/` before this ticket can leave `blocked/`.
- `parent_ticket_id` — optional, for sub-ticket trees.
- `started_at`/`completed_at` — set by dispatcher and `ticket-complete` respectively.
- `tldr` — short summary written at completion (the contract).
- `pilot_notified` — true once the completion TL;DR has been sent to the pilot peer (the Matrix bridge then surfaces it with native client push).

## State machine

```
draft ──▶ armed ──▶ blocked ──▶ queued ──▶ running ──▶ done | failed | cancelled
              ╰─────────────▶ queued     (if depends_on is empty)
```

**State is encoded by directory**, not by a field. Each ticket lives in exactly one of:

```
~/mesh/tickets/<agent>/
├── draft/<tk-id>.json
├── armed/<tk-id>.json
├── blocked/<tk-id>.json
├── queued/<tk-id>.json
├── running/<tk-id>.json
├── done/<tk-id>.json
├── failed/<tk-id>.json
└── cancelled/<tk-id>.json
```

Transitions are filesystem moves: `mv tickets/agent/queued/tk-abc.json tickets/agent/running/tk-abc.json`. Atomic on a single filesystem.

The `status` field in the JSON is **derived** from the directory at read time (not authoritative). If JSON and directory disagree, directory wins.

### Why directory-encoded state

- `ls tickets/<agent>/running/` immediately answers operational questions.
- Backups are file-level (`cp -a`).
- No DB schema migrations when adding states.
- Tests can `mv` files between directories without going through an API.

Tradeoff: the dispatcher must hold a lock-equivalent (file-level atomic rename) to avoid two transitions racing. Single-process dispatcher avoids the issue.

## Working directory per ticket

When a ticket is created, the dispatcher scaffolds a companion directory:

```
~/mesh/tickets/<agent>/working/<tk-id>/
├── brief.md      # the prompt extracted from the ticket JSON
├── todo.md       # template with To do / In progress / Done sections
├── notes.md      # empty; agent drops intermediate artifacts here
└── output.md     # empty; filled at completion with the TL;DR + details
```

See [working-dirs.md](working-dirs.md) for the full pattern.

The working directory is moved alongside the JSON only on terminal states (done/failed/cancelled) — at that point it sits next to the final JSON as an audit trail.

## Dispatcher service

A single Python process (`ticket-dispatcher.service` under `systemd --user`) continuously:

1. Scans `tickets/*/armed/` and `tickets/*/blocked/`.
2. For each ticket:
   - If all `depends_on` exist in `done/` of their respective assignee → move armed/blocked → `queued/`.
   - Else → ensure ticket is in `blocked/`.
3. For each `queued/<tk>.json`:
   - Check if assignee is idle (via tmux capture-pane heuristic).
   - Check inbox-age guard (don't dispatch if recipient inbox was touched recently, unless ticket is older than `idle_min_minutes`).
   - If green: move queued → running, set `started_at`, send the standard "[mesh] ticket id=X..." nudge via the bus.
4. Periodically scans `running/` for timeouts (default 30 min) → mark as `failed/` with `tldr="timeout"`.

Inbox-age guard rationale: avoid interrupting an agent that just finished a different task. The "stale ticket" exception (`_armed_ticket_age >= idle_min_minutes`) ensures armed tickets do eventually dispatch even on a chatty agent.

## Cross-agent dependencies

A ticket's `depends_on` can list ticket IDs assigned to **different agents**:

```
tk-001 → assigned to agent-A (writes config)
tk-002 → assigned to agent-B, depends_on=["tk-001"] (deploys using config)
tk-003 → assigned to agent-A, depends_on=["tk-002"] (smoke-tests deployment)
```

The dispatcher scans across all `<agent>/done/` directories when resolving deps. No orchestrator needed — agents discover the chain at dispatch time.

Use this for short workflows where coordination between specialized agents matters. For deep DAGs (10+ deps), build a real workflow engine; this pattern targets small, ad-hoc chains.

## Completion contract

When an agent finishes a ticket:

```bash
python3 ~/mesh/ticket-complete.py <tk-id> --tldr "1-3 sentence summary"
```

Or on failure:

```bash
python3 ~/mesh/ticket-complete.py <tk-id> --tldr "what happened" --failed "reason"
```

The CLI:
1. Locates the ticket (must be in `running/`).
2. Writes `tldr` and `completed_at` to the JSON.
3. Writes the TL;DR (plus optional detail) to `working/<tk-id>/output.md`.
4. Moves `running/<tk-id>.json` to `done/<tk-id>.json` (or `failed/`, or `cancelled/`).
5. Moves the working directory alongside.
6. Optionally sends the TL;DR to the pilot peer (set `pilot_notified=true`); the Matrix bridge then delivers it with native client push.

## Stop-hook fallback

If an agent's session ends abruptly (Claude Code `Stop` hook fires while a ticket is in `running/`), a fallback script automatically:
1. Tries to extract the last assistant message from the transcript as TL;DR.
2. Calls `ticket-complete.py` with `--failed "session ended without explicit completion"` if extraction fails.

This prevents tickets from being stuck in `running/` forever when an agent crashes or is restarted mid-task.

## Sub-tickets

A ticket can spawn child tickets via the API (`POST /tickets` with `parent_ticket_id=<tk-id>`). The webui renders parent → children as a tree, so a user can monitor decomposition.

Pattern use: an agent receives "build feature X" → realizes it's 5 components → creates 5 sub-tickets, some targeting other specialized agents. The user sees the breakdown progress per sub-item rather than as one opaque task.

## API surface

Tickets are exposed through these endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /tickets` | Create a new ticket (draft or armed) |
| `POST /tickets/bulk` | Create multiple tickets in one call (supports `_stepN` placeholders for chains) |
| `GET /tickets/<agent>` | List tickets for an agent, filterable by state |
| `GET /tickets/<agent>/<tk-id>` | Read one ticket (strict regex validation on `tk-id`) |
| `PATCH /tickets/<agent>/<tk-id>` | Update prompt, dispatch_mode (only in mutable states) |
| `DELETE /tickets/<agent>/<tk-id>` | Cancel a ticket; running tickets are archived as `cancelled/` |

All routes validate `tk-id` against the strict regex `^tk-[0-9a-f]{6}$` to prevent wildcard attacks.

## Tests

A reference implementation should include pytest coverage for:

- State machine transitions (draft → armed → queued → running → done)
- Cross-agent dependency resolution
- Bulk creation with `_stepN` placeholders
- Wildcard ticket ID rejection (regex enforcement)
- Stop-hook fallback (running → failed with synthesized TL;DR)
- Idempotent working-dir scaffolding
- DELETE removes both the JSON and the working directory

Typical coverage: ~80 tests for the ticket subsystem, including adversarial cases.

## Observability

Useful one-liners on a running mesh:

```bash
# What is each agent doing right now?
ls -la ~/mesh/tickets/*/running/

# How many tickets in each state, all agents?
for s in draft armed blocked queued running done failed cancelled; do
  n=$(ls ~/mesh/tickets/*/$s/*.json 2>/dev/null | wc -l)
  echo "$s: $n"
done

# Show open dependencies for an agent
ls ~/mesh/tickets/<agent>/blocked/*.json | xargs -I{} jq -r '"\(.id): waits for \(.depends_on)"' {}
```
