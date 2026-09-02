# Evolution

The mesh was not designed up-front — it grew organically over about ten days, with each phase solving a concrete pain point. The result is a smallish system that's still readable, but the path to it had detours. This document is the honest history.

## Reading this document

Each phase records:
- **What it added** — the new capability
- **What it cost** — the time and the rewrites
- **Tradeoffs encountered** — the decisions that turned out non-obvious
- **What the next phase had to fix** — visible in hindsight

Use this as a debugging aid (when you hit the same wall, the symptom and fix are documented) and as a planning aid (don't try to do Phase 10 before Phase 4).

## Phase 0 — Skeleton (day 1)

Three persistent agents on one host, each in a tmux session. Communication via shared markdown files in pair-wise directories (`agent-A-agent-B/`). One agent appended a section; the other read it on session start.

Tooling: nothing — `cat` and `echo >>`. No watcher. No API. No tickets.

What worked: agents reliably read messages on session start (via a `SessionStart` hook that injected pending content into context). Filesystem-only state was easy to inspect and back up.

What didn't: messages weren't pushed live. The author had to manually tell agents "go check your file." Latency from author intent to agent reaction: ~5 minutes of human attention.

Time spent: half a day.

## Phase 1 — Watcher with inotify push (day 2)

A single bash script using `inotifywait -m` on the message directory, parsing the last appended line, and using `tmux send-keys` to inject "[mesh] new message from X" into the recipient's tmux pane.

Result: end-to-end latency dropped from minutes to ~1 second.

Tradeoffs:
- The watcher had to differentiate `agent-A-agent-B/agent-A.md` (from A) vs `agent-A-agent-B/agent-B.md` (to A) — encoding direction in the filename. Worked but didn't scale to N agents.
- Detecting "is the recipient idle" via parsing the tmux status bar (`esc to interrupt` = busy, `← for agents` = idle) was hacky but robust.

Time spent: half a day.

## Phase 2 — Unified bus directory (day 3)

Three pair-wise dirs (`A-B/`, `A-C/`, `B-C/`) became unsustainable at four agents (six pairs) and would have been catastrophic at five (ten pairs). Refactor: one bus directory with one inbox file per recipient (`inbox-<agent>.jsonl`). Senders write to the recipient's inbox, recipients read their own.

Switched the format from markdown to JSONL: each line is a single message dict (id, ts, from, to, priority, body, acked). Made dedup and ack tracking deterministic.

Also introduced `peers.py`/`peers.sh` as the single source of truth for the agent list — eliminated agent name drift across scripts.

Time spent: one day. Most of it was migrating existing history into the new format (`migrate.py`).

## Phase 3 — Secondary host via SSH (day 4)

Added a workstation as a second host, with one or two agents in WSL. Used SSH from the primary's watcher to push to the remote tmux: `ssh user@host 'wsl -d Ubuntu -- tmux send-keys -t agent-X "..."'`.

Tradeoff: any text interpolated into the SSH command had to be shell-escaped. First version used double quotes — vulnerable to body content containing `"` or `$()`. Took ~2 hours to debug a "weird" remote behavior before noticing the injection vector. Fixed with `printf -v escaped '%q' "$msg"` and single-quote-safe contexts.

Also added a cron-based heartbeat on the secondary host that posts liveness to the primary's filesystem every minute. Without this, the primary couldn't tell "is the remote agent alive" without an SSH check, which was expensive.

Time spent: one day plus the bug-hunt.

## Phase 4 — FastAPI backend (day 5)

Built `mesh-api` (FastAPI + uvicorn) as a thin facade over the filesystem. Routes: `/inbox`, `/send`, `/ack`, `/conversation/<agent>` (aggregated incoming + outgoing for an agent), `/status` (all agents' liveness), `/stream` (WebSocket for live updates).

Auth: bearer token (`api-tokens.json` mode 0600), one token per device.

Tradeoffs:
- `/send` invokes the bus's `send.py` via subprocess. Synchronous. With 10 concurrent POSTs the FastAPI event loop blocks. Acceptable for one user; would not scale to a team. Future migration: rewrite `send.py` logic in pure Python and inline.
- WebSocket `/stream` initially had no heartbeat. Client watchdogs killed idle connections every 60 seconds. Fixed in Phase 9 by emitting a `{"topic":"status","event":"heartbeat"}` every 30 s.

Time spent: half a day.

## Phase 5 — Ticket state machine (day 6)

Messages weren't sufficient for "long-running tasks with completion contracts." Introduced **tickets** — JSON files moved between directories acting as states: `draft → armed → blocked → queued → running → done/failed/cancelled`.

A separate `ticket-dispatcher` daemon scans `armed/` and `blocked/`, resolves `depends_on`, moves to `queued/`, then dispatches to the assignee's tmux when idle.

Cross-agent dependencies came for free: `depends_on` IDs can point to any agent's `done/` directory. The dispatcher's resolution loop spans all agents.

Tradeoff: the dispatcher initially had a stale-ticket bug. The inbox-age guard refused to dispatch if the recipient's inbox was touched in the last 5 minutes — but the recipient's inbox is touched by *every* message, including the dispatch nudge from another ticket. Chatty agents never got their armed tickets. Fixed by adding "if ticket has been armed for ≥ idle_min_minutes, bypass inbox guard." Discovery took an audit.

Time spent: one day.

## Phase 6 — Browser webui (days 6-7)

A browser dashboard served by `mesh-api` itself at `/ui/` — a single static `index.html` (vanilla JS + d3, no build step), no framework, no separate server. Three views: Dashboard (per-agent cards + liveness + ctx % + a d3 activity graph), Conversation (per-agent timeline of messages + tickets, paginated), Compose Ticket (form for new tickets with cross-agent dependency picker).

Why a webui instead of a native app: it reuses the surface that already existed. Every view is a thin read over `/status`, `/contexts`, `/graph`, `/conversation` and a single `/stream` WebSocket — so the dashboard added almost no backend, and there was nothing to build, sign, or sideload. A change is a page reload.

Design points:
- A single WebSocket to `/stream` carries live updates while a tab is open; on each event the page does a targeted refetch and re-renders only the affected card.
- Bearer token in `localStorage` (acceptable *only* because the page is served over Tailscale-only ingress — see the trust model).
- No push of its own: when the tab is closed there is nothing to update. That role is deliberately handed to the Matrix bridge (Phase 7).

This was a fraction of the code a native app would have been (a few hundred lines of HTML/JS vs ~5000 lines of Kotlin). Most of the time went to UX iteration, not plumbing ("the messages are still unreadable," "I can't tell if you've seen my message").

Tradeoffs that emerged later:
- Rendering messages as markdown mangled free user text (lines starting with `-` became list items). Fixed by detecting "looks like markdown" before rendering.
- Long messages (briefings of 200+ lines) filled the view. Solved in Phase 11 with collapse + first-line bold preview.
- "Did the agent see my message" had no UI signal until Phase 10 (read receipts) — and is answered more naturally by the Matrix bridge's read receipts.

## Phase 7 — Matrix bridge (day 8-9)

The webui has no push of its own — close the tab and you hear nothing. Rather than build a notification stack (Firebase, device registration, a foreground service), the project bridged the bus into **Matrix**: ~250 lines of pure-stdlib Python (`bridge.py`) running as one `systemd --user` service. One room per agent, each agent posting under its own Matrix identity (`@agent`), the pilot talking to them from Element on phone and desktop. Native push, threads, and a mature client — all for free, no app to ship.

The bridge is a *peer of the mesh, not a controller*: it writes into the bus through a dedicated `pilot-matrix` peer and reads agents' replies from that peer's inbox (see [components/matrix-bridge.md](components/matrix-bridge.md)). Read receipts ("seen" the instant a message is captured) gave the pilot the arrival signal the webui couldn't.

Also fixed the WebSocket disconnect-every-60s mystery here (heartbeat task in `_watch_loop`) and several minor webui UX bugs.

Tradeoff: the one bug that *will* bite you — agents post under their own Matrix identities, so a naive bridge reads those posts back on the next `/sync` and re-injects them. Fixed with a strict "relay only the pilot's events" filter.

Time spent: half a day for the bridge, half a day for polish.

## Phase 8 — Supervisor agent + auto-compact (day 9-10)

Added a sixth agent (`supervisor` role) running on the lighter Haiku tier. Its job: monitor the others and act when they need help. Concrete capabilities:

- Cron every 10 min: read each agent's session jsonl to compute context window usage, emit alerts at thresholds (60, 75, 85, 95 %).
- At 60 %, automatically inject `/compact` into the agent's tmux via send-keys (with a `dismiss survey first` step to avoid the Claude Code quality survey eating the command).
- Restart agents whose tmux session died (`systemctl --user restart <agent>`).
- Skip auto-actions if the agent is busy (parse status bar for `esc to interrupt`).

Tradeoffs:
- Auto-compact at 60 % is aggressive. Compromise: pick what your usage tolerates. The compact preserves a summary; the agent doesn't fully forget.
- The supervisor was first not in `COMPACTABLE_AGENTS` ("it's lighter, it doesn't need it"). Then in practice it climbed to 70 %. Decision: include it. The supervisor compact-ing itself is fine (its workload is monitoring, no critical mid-task state).

Time spent: one day, with significant iteration on the "0 Enter eats a turn" bug (sending the survey dismiss key when no survey is present).

## Phase 9 — Matrix pilot as first-class peer (day 10)

The pilot's clients initially could only send into the bus; they had no inbox of their own. Fixed by giving the pilot a real bus identity — `inbox-pilot-matrix.jsonl` (and `user-web` for the webui's POSTs) — and treating the pilot as a "passive peer": agents send to it the same way they send to each other, the bridge tails that inbox into Matrix, and the API's WebSocket broadcasts to any open webui tab. No special path; the pilot is just-another-peer.

Required updates to ~6 places that had hardcoded agent lists. Started a `peers.py` deduplication pass that uncovered 11+ copies of `AGENTS` across the codebase. Consolidated most into a shared module.

Time spent: half a day for the channel, two days of cleanup for the AGENTS deduplication (revealed by a later security audit).

## Phase 10 — Security batch + read receipts (day 10-11)

A cross-cutting audit by an Opus subagent revealed three actively exploitable issues:

1. **`from` field non-validated against `ALL_PEERS`** — any token holder could spoof any agent's identity. Confirmed by reproducing the exploit (sending `from="X"` from a token that should only speak as the pilot peer, and observing X's peers ACK the impostor).
2. **Wildcard `ticket_id` glob** — `GET /tickets/<agent>/%2A` dumped the first ticket in the directory; `DELETE` destroyed it. Trivially exploitable.
3. **Off-by-one regex** — `^[a-z][a-z0-9-]{0,32}$` accepts 33 characters total (the `{0,32}` repeats 0–32 *additional* chars).

All three patched within a single backend release. New regression tests added.

In parallel, read receipts:
- Bus already stored `acked: bool` per message; the `/conversation` aggregator was dropping it on the way out. Patched to expose. ~5 lines.
- App displays ✓ (gray, sent) vs ✓✓ (accent, read) on user-outgoing bubbles.

Time spent: ~half a day. The audit took longer than the fix.

## Phase 11 — UX readability iteration

Long messages were still unreadable in the webui — collapse + first-line bold helped, but the first line of a markdown brief is often a heading like `# v0.30.7 — Patch ...` which is meaningful but not a summary.

Backend solution: deterministic `summarize(body, from_)` function that detects known mesh patterns (deliveries, briefs, phase updates, ACKs, health checks, commits) and extracts a structured 70-100 char summary. Exposed as `summary` field on conversation items — so every client (webui *and* the rendered Matrix/`render.py` views) benefits, not just one UI.

Webui solution: display `summary` bold at the top of each bubble when present; full body below in muted text, collapsed past a few lines.

Effect: a 200-line briefing now renders as "Brief v0.30.X for agent-Y" in bold + 3 lines of body + "See all" tap. Visual saturation eliminated.

Tradeoff: the pattern set is deterministic and finite. For arbitrary user prose, the summarizer falls back to the first non-empty line (truncated at 80 chars), which is "good enough" rather than "great." Future work: LLM-generated summary.

## Phase 12 — Per-ticket working directories

Tickets started carrying their context entirely in the prompt field — fine for short tasks, but long briefings (~200 lines) made the agent's working memory the only place the brief lived. If the session compacted mid-task, the brief was at risk.

Introduced a per-ticket **working directory** scaffolded automatically:

```
~/mesh/tickets/<agent>/working/<tk-id>/
├── brief.md      # the prompt extracted at dispatch
├── todo.md       # template with To do / In progress / Done
├── notes.md      # intermediate artifacts
└── output.md     # final TL;DR + details
```

Archived alongside the JSON at terminal state. See [working-dirs.md](components/working-dirs.md).

Agents now reference these files (`cd ~/mesh/tickets/me/working/<tk-id>/`) for durable state.

Time spent: a few hours backend + charter update for agents.

## Phase 13 — Identity / display layer

The bus uses canonical ids everywhere (`agent-1`, `agent-2`, …) for routing — but the human pilot wants names and faces, not slugs. Added a thin **display layer** read only at the rendering surfaces: a `registry.json` (`id → {display name, avatar}`) that the webui reads for its cards, plus per-agent Matrix profile display names + avatars pushed to the homeserver, plus an optional `office/` view that renders each agent as an animated sprite in a room.

The rule that kept this clean: **never rename the base layer.** The display layer is a lookup over the canonical ids, applied at the edge (webui card, Matrix room) — so re-theming the whole fleet is a `registry.json` edit + an avatar push, with zero impact on inboxes, tmux sessions, or tokens.

Gotcha learned here: a Matrix *room* carries its own `m.room.name`/`m.room.avatar` that **overrides** the member profile in the client's room list — so pushing only the profile leaves the old name showing in the sidebar. You have to set both.

Time spent: ~half a day, mostly chasing the room-vs-profile override.

## Phase 14 (in progress) — Config-driven deployment

The system was hardcoded to the original deployer's machine: paths in `/home/<user>/`, agent names baked in, ports in source. The current refactor extracts:

- All paths into environment variables (`MESH_HOME`, `MESH_AGENT_BASE`, etc.) — done.
- Agent list into a `mesh.toml` config schema (parsed with Pydantic) — in progress.
- An installer script (`bootstrap.sh`) that takes a `mesh.toml` and provisions everything — in progress.

Goal: someone clones the public repo, writes their `mesh.toml`, runs `bootstrap.sh`, and gets a working mesh on their hosts.

## Phases not done

The following are documented but deliberately not implemented (yet, or in this repo):

- **Native mobile app** — the browser webui + Matrix client cover the use case (the webui is responsive enough on a phone browser, and Element carries native push). A bespoke Android/iOS app would be ~5000 lines of duplicated effort for a single operator. Deliberately not built.
- **Multi-user / multi-tenant** — the trust boundary is "anyone with a token." A team version would need per-token scopes, audit logs, rate limiting. Not in scope for a personal mesh.
- **Horizontal scaling** — single-process watcher and dispatcher. Past 5–10 agents on commodity hardware, you'd need to shard.
- **Production-grade observability** — logs are structured but no Prometheus, Grafana, or OpenTelemetry integration. The hooks are there; the wiring isn't.

## Lessons that generalize

Three things from this evolution that would inform a v2 or a copy:

1. **Build the bus first, the UI last.** The webui is the most visible artifact but it shipped in Phase 6 — *after* messaging and tickets were working from the CLI. UIs amplify the underlying primitive; if the primitive is rough, the UI papers over it instead of fixing it. Corollary learned the expensive way first: don't reach for a native app when a static page over your existing API does the job — the UI should be the *thinnest* thing that surfaces the primitive, not a second codebase.

2. **Charters > code for behavior shaping.** When you want an agent to behave differently, change its `CLAUDE.md` first. Code is the fallback. Most "feature requests" in this project were charter edits.

3. **Audit early, audit often.** The Opus-tier cross-cutting audit found 3 actively exploitable security issues that had survived earlier reviews. Spending a few hours every couple of weeks reading the system from an attacker's mindset catches things that incremental development misses.
