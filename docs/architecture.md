# Architecture

The full picture, top to bottom, with the actual data flows.

## Mental model

Think of the system as a small distributed OS where:

- Each **agent** is a long-running process (a Claude Code CLI in a tmux pane).
- Each **message** is a line appended to a JSON-lines file on disk.
- Each **ticket** is a JSON file moved between directories that act as state slots.
- The **watcher** is a system-level event loop that wakes recipients when their inbox changes.
- The **API** is just a thin facade over the filesystem.
- The **webui** is a browser client that hits the API; the **Matrix bridge** is a peer that relays the bus to/from a chat client.

There is no central scheduler. There is no message queue (RabbitMQ, NATS, Kafka). There is no shared database. There is no service mesh (Istio, Linkerd). The "mesh" is the agents and the bus — that's it.

## Hosts

Two hosts in a typical deployment:

- **Primary**: an always-on box (Raspberry Pi, NUC, or any small Linux machine). Hosts the bus, the API, the dispatcher, the watcher, the supervisor, and 3–4 agents in `tmux` sessions.
- **Secondary**: a workstation (any OS that runs WSL2 or a Linux VM). Hosts 1–2 agents in `tmux` sessions inside WSL.

You can run with only the primary host. Adding the secondary is purely additive: more agents, more parallelism, no architectural change.

The **human pilot** participates as a **passive peer** rather than a host: it sends messages and tickets but runs no agent. It reaches the mesh two ways — a browser pointed at the webui (`/ui/`, sending as `user-web`) and a Matrix client bridged in as `pilot-matrix`. The latter has a dedicated inbox file on the primary host (`inbox-pilot-matrix.jsonl`) so the bus treats it symmetrically with real agents.

## Filesystem layout

On the primary host (`~/mesh/` is the canonical mesh directory):

```
~/mesh/
├── inbox-<agent-1>.jsonl           # one file per recipient, append-only
├── inbox-<agent-2>.jsonl
├── inbox-<agent-N>.jsonl
├── inbox-pilot-matrix.jsonl       # the human pilot is a peer, gets its own inbox
│
├── state-<agent-1>.json            # last-read cursor per peer, ack flags
├── state-<agent-2>.json
├── ...
│
├── heartbeats/                     # liveness signals
│   ├── <agent-1>.json
│   └── <agent-N>.json
│
├── tickets/
│   ├── <agent-1>/
│   │   ├── draft/                  # created, not dispatchable
│   │   ├── armed/                  # dispatchable, waiting for deps
│   │   ├── blocked/                # deps unmet
│   │   ├── queued/                 # ready, waiting for agent idle
│   │   ├── running/                # in progress
│   │   ├── done/
│   │   ├── failed/
│   │   └── cancelled/
│   └── <agent-2>/...
│
├── logs/                           # operational logs
│   ├── watcher.log
│   └── classifier.log              # scope enforcement audit trail
│
├── peers.py                        # canonical agent list (Python)
├── peers.sh                        # canonical agent list (Bash)
│
├── send.py                         # CLI: append a message to a recipient's inbox
├── read.py                         # CLI: read+ack messages in your own inbox
├── render.py                       # generate human-readable mesh.md from JSONL
├── watcher.sh                      # inotify loop (started by systemd)
├── ticket-complete.py              # CLI: mark a ticket done with TL;DR
│
├── api-tokens.json                 # bearer tokens for the HTTP API + webui (mode 0600)
└── (Matrix bridge config lives under bridges/matrix/, see components/matrix-bridge.md)
```

The same layout is replicated **except** for `inbox-*` and `tickets/` on the secondary host. The secondary host's agents read from / write to **the primary host's** filesystem via SSH (or via the API). The secondary host is essentially "agents only" — no bus state lives there.

## Data shapes

### Message (one JSONL line)

```json
{
  "id": "a1b2c3d4",
  "ts": "2026-05-24T14:30:00+02:00",
  "from": "<agent-1>",
  "to": "<agent-2>",
  "priority": "low" | "normal" | "urgent",
  "body": "...",
  "reply_to": "<other-message-id>" | null,
  "acked": false
}
```

- `id` is `sha1(from|ts-minute|body[:200])[:8]` + entropy nonce (seconds + a short random hex) to prevent intra-minute collisions
- `ts` is ISO 8601 with timezone (use `ZoneInfo("Europe/<your-city>")`, not a hardcoded offset)
- `body` is plain text; can contain markdown. Cap at 64 KiB to stay below subprocess `ARG_MAX`. Reject null bytes
- `reply_to` lets clients reconstruct threads (webui timeline, Matrix threading)
- `acked` is `false` on write, flipped to `true` by the recipient's CLI tool

### Ticket (one JSON file)

```json
{
  "id": "tk-abc123",
  "ts_created": "2026-05-24T14:30:00+02:00",
  "from": "<sender>",
  "to": "<agent>",
  "prompt": "...",
  "dispatch_mode": "draft" | "armed",
  "depends_on": ["tk-other1", "tk-other2"],
  "parent_ticket_id": "tk-parent" | null,
  "started_at": null,
  "completed_at": null,
  "tldr": null
}
```

The ticket's **state** is encoded in **which directory it sits in**, not in a field. Transitions are filesystem moves. This is what makes the system inspectable with `ls`.

### Heartbeat (one JSON file)

```json
{
  "ts": "2026-05-24T14:30:00+02:00",
  "idle": true | false,
  "last_activity": "2026-05-24T14:28:00+02:00" | null,
  "ctx": {
    "tokens": 125000,
    "max_tokens": 200000,
    "model": "...",
    "pct": 62.5
  }
}
```

Each **remote** agent's host writes its own heartbeat once per minute (cron on the host). The primary host's local agents are observed differently — via `tmux capture-pane` for liveness and via `~/.claude/sessions/*.json` for ctx %. The supervisor merges both sources to know "is this agent alive, what % of its context window is used, is it idle".

## Critical data flows

### 1. Message from agent-1 (primary) to agent-2 (primary)

```
agent-1 in tmux pane
    │
    │  runs:  send.py agent-1 agent-2 normal "body..."
    ▼
send.py
    │
    │  1. validate from/to/priority against peers.py whitelist
    │  2. compute id = sha1(from|ts|body[:200])[:8] + entropy
    │  3. assemble JSON line
    │  4. flock(inbox-agent-2.jsonl, LOCK_EX)
    │  5. append the line
    │  6. release lock
    ▼
inbox-agent-2.jsonl  ←  file modified  ─────▶  inotify event
                                                    │
                                                    ▼
                                            watcher.sh main loop
                                                    │
                                                    │  1. parse event path → recipient = agent-2
                                                    │  2. parse last line of inbox-agent-2.jsonl → id, priority, body
                                                    │  3. tmux capture-pane -t agent-2 → check status bar
                                                    │     ├── "esc to interrupt" → busy → skip push
                                                    │     └── "← for agents" → idle → push
                                                    │  4. push:
                                                    ▼
                                            tmux send-keys -t agent-2 \
                                              "[mesh] ticket id=$id ... priority=$pri. Lis: python3 ~/mesh/read.py agent-2"
                                                    │
                                                    ▼
                                            agent-2's CLI receives the prompt as user input
                                                    │
                                                    │  1. Claude Code processes it
                                                    │  2. typically runs read.py to fetch the body
                                                    │  3. acts (or replies, or starts work)
                                                    ▼
                                            read.py agent-2  → prints unread bodies,
                                                              flips acked=true if invoked with --ack
```

End-to-end latency on the local case: ~500 ms (inotify is fast).

### 2. Message from agent-1 (primary) to agent-4 (secondary, WSL)

Same as above up to the watcher. Then:

```
watcher.sh
    │
    │  case "to" of:
    │    secondary-host agent → push_remote()
    ▼
push_remote(agent-4, msg):
    │
    │  builds an SSH command, escapes the msg with printf %q to
    │  prevent shell injection from a malicious body
    │
    │  ssh primary-user@secondary-host \
    │    "wsl -d Ubuntu -- tmux send-keys -t agent-4 -l $escaped_msg"
    │
    ▼
SSH connects → WSL boots tmux subprocess → send-keys delivered

The agent-4 CLI sees the prompt and proceeds.
```

Latency on LAN: ~1–2 seconds (SSH handshake dominates). The push is fire-and-forget; if SSH fails (host asleep, WSL not running), the message stays in `inbox-agent-4.jsonl` and the agent will pick it up at its next `SessionStart` hook.

### 3. Ticket lifecycle

```
1. The webui POSTs to /tickets (as user-web)
        │
        ▼
2. API creates JSON file at ~/mesh/tickets/<agent>/draft/<tk-id>.json
   (dispatch_mode default = "draft" — user must arm it explicitly)
        │
        ▼
3. User taps "arm" → API moves draft/ → armed/
        │
        ▼
4. Dispatcher loop (systemd service, polls every 5s):
        │
        │  for ticket in armed/*:
        │      if all depends_on are done/ → move to queued/
        │      else → move to blocked/
        │
        ▼
5. queued/<tk>.json — waiting for agent idle
        │
        ▼
6. When watcher sees the agent's heartbeat says idle=true:
        │
        │  dispatcher:
        │    1. move queued/<tk>.json → running/<tk>.json + set started_at
        │    2. send the ticket prompt via the standard message bus path
        │      ("[ticket tk-abc] from <sender> priority=normal. Lis: ...")
        │
        ▼
7. Agent works, then runs ticket-complete.py with --tldr (success) or --failed (failure)
        │
        ▼
8. ticket-complete.py:
        │    move running/<tk>.json → done/<tk>.json
        │    set completed_at and tldr fields
        │    send the TL;DR to the pilot peer → the Matrix bridge surfaces
        │      it as a message in the agent's room (native client push)
```

Fail-safe: if the agent's Claude Code session exits (`Stop` hook fires) while a ticket is `running/`, a fallback handler marks the ticket as `failed/` with a synthesized TL;DR ("session ended without explicit completion"). This prevents tickets from getting stuck.

### 4. Cross-agent dependency

```
The webui sends a chain:
  tk-001 (agent-2): "set up directory structure"
  tk-002 (agent-3): "run tests", depends_on: ["tk-001"]
  tk-003 (agent-2): "publish results", depends_on: ["tk-002"]

Initial state:
  tickets/agent-2/queued/tk-001.json
  tickets/agent-3/blocked/tk-002.json  (waits for tk-001)
  tickets/agent-2/blocked/tk-003.json  (waits for tk-002)

After tk-001 done:
  Dispatcher's blocked-scan:
    tk-002.deps = [tk-001] → tk-001 is done → move blocked/ → queued/
    tk-003.deps = [tk-002] → tk-002 still queued → stay blocked

After tk-002 done:
  tk-003.deps = [tk-002] → done → move blocked/ → queued/
```

The dispatcher's blocked-scan runs on every transition. Cross-agent is just cross-directory `find`.

### 5. Webui → API → live update

```
Browser tab (webui at /ui/, open):
    │
    │  compose box sends POST /send {to: agent-2, body: "hi", from: "user-web"}
    ▼
API:
    │   1. auth check (bearer token from localStorage, over Tailscale-only origin)
    │   2. validate from (must be a pilot identity), to (must be a peer)
    │   3. call mesh_io.send_message (which shells out to send.py)
    │   4. respond 200 with the new message id
    │
    │   In parallel:
    │   5. server-side WS endpoint /stream has subscribers (the open tab's WS)
    │   6. an inotify task on the API side sees inbox-agent-2.jsonl change
    │   7. broadcasts {topic: "inbox:agent-2", event: "new_message"}
    ▼
Browser tab (WS receives the event):
    │   refetch /conversation/agent-2 → render the new bubble in the active view
```

When **no tab is open**, there is nothing to update — and that's fine, because the pilot's away-from-keyboard channel is Matrix, not the webui. When an agent replies, it sends to the `pilot-matrix` peer; the bridge posts it into the agent's Matrix room, and the Matrix client (Element) delivers a native push to the pilot's phone. No FCM, no foreground service, no app to keep alive.

### 6. Auto-compact (supervisor)

```
Cron on primary host every 10 min:
    │
    ▼
context_monitor.py:
    │   1. read each agent's recent session jsonl (Claude Code stores transcripts here)
    │      OR read heartbeats/<agent>.json.ctx for remote agents
    │   2. compute current ctx % for each
    │   3. for each agent ≥ 60% AND idle (check tmux status bar) AND not in cooldown:
    │        dismiss any survey popup ("0 + Enter + sleep 2")
    │        tmux send-keys -t <agent> "/compact" Enter   (or via SSH for remote)
    │        update state file with last_compact_ts
    │   4. emit threshold-crossing alerts to the supervisor agent's inbox
    ▼
The agent runs /compact (its CLI feature), context shrinks to ~30%, supervised conversation continues.
```

This is what lets agents run for hours. Without it, every agent hits its context wall and either silently degrades (autocompact at 95%, partial information loss) or fails. The 60% threshold trades "some context shrinkage" for "always-fresh agent". The cooldown (10 min) and idle check prevent interrupting mid-task.

## Scope enforcement

Each agent has a `CLAUDE.md` in its working directory (the project-level instruction file Claude Code reads on session start). This file declares the agent's charter in natural language: what it owns, what it must not touch, who reports to whom.

This is **soft** scope. To make it **hard** (or at least auditable), a `PreToolUse` hook is wired into `~/.claude/settings.json` for the user. The hook:

1. Receives every tool call before it runs.
2. Reads the calling agent's working directory (via `cwd` from the hook context).
3. Loads a per-agent scope profile (a list of allowed/denied path prefixes).
4. Decides ALLOW / DENY / LOG.
5. In current deployments: runs in **LOG mode** (writes `WOULD_DENY` lines to a classifier log but never blocks). The supervisor reviews this log periodically.

LOG mode is intentional: it avoids breaking agents while the scope profiles stabilize. Hard ENFORCE can be enabled per-agent once the profile is validated.

## Persistence and recovery

What survives a reboot of the primary host:
- All inboxes (JSONL files)
- All tickets in any state (JSON files)
- All heartbeats (until they're overwritten by the next cron)
- The API tokens and the Matrix bridge tokens
- The Tailscale tunnel (it reconnects)
- `systemd --user` re-starts every service: API, dispatcher, watcher, agents

What does **not** survive:
- The in-memory `last_alerted_threshold` of the cron context monitor (V2 persists this to a state file to fix it)
- An agent's in-CLI conversation (Claude Code itself persists transcripts in `~/.claude/projects/<...>/<session>.jsonl`, but a hard kill loses the live conversation — recovery uses a `SessionStart` hook that injects the agent's pending inbox into the new session's context)

What survives a reboot of the secondary host:
- The agents come back via `systemd --user` on next boot
- They re-read their inbox on `SessionStart` (the hook scans the primary's filesystem via SSH)
- Heartbeat resumes via cron once WSL is up

The whole thing is essentially **a stateless API over a versioned filesystem**. The state IS the filesystem.

## Observability

Built in:
- `~/mesh/logs/watcher.log` — every inotify event + push decision (success/skip/busy)
- `~/mesh/logs/classifier.log` — every PreToolUse decision (LOG mode = audit trail)
- `~/<supervisor-agent>/logs/context-monitor.log` — every cron context snapshot
- `~/<supervisor-agent>/logs/compact-actions.log` — every auto-compact trigger
- `~/mesh/mesh.md` and `~/mesh/mesh-<agent>.md` — auto-rendered human-readable views of recent messages (regenerated on every event by `render.py`)

The webui has an activity graph (d3 over `/graph`) and a live feed that subscribe to the WS stream and show every mesh event with per-agent color coding.

For deeper observability (latency p99, error rates, etc.) you'd plug Prometheus + Grafana — not done in current deployments, but the data is already structured logs so the integration is straightforward.

## What this architecture is *not*

- **Not a production multi-tenant system.** It's a personal mesh. Trust boundary = "anyone with a token can do anything". Adding scopes per token, audit logs, rate limits, etc. is left as an exercise.
- **Not a distributed-consensus system.** Single source of truth = primary host filesystem. If you lose the primary, you lose state. Backup the bus directory.
- **Not Kubernetes.** No declarative state, no rolling updates, no replica sets. `systemctl --user restart <agent>` is the deploy command.
- **Not horizontally scalable** past 5–10 agents on commodity hardware. The inotify watcher is single-threaded; tmux pane count starts to weigh on the host. For more, you'd shard the bus.

These limits are choices, not accidents. Adding any of them would change the character of the system from "a personal lab" to "a tool", which is a different project.
