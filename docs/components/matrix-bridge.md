# The Matrix bridge

A chat facade that lets the human pilot talk to agents from any Matrix client (Element, FluffyChat, …) instead of a bespoke app or a vendor "remote control" panel. It is the mesh's **primary conversational surface** — where the pilot chats with agents and gets push notifications when away — paired with the [webui](webui.md), which carries the at-a-glance dashboard. The bridge is a *peer of the mesh, not a controller*: it writes into the bus exactly the way an agent does, through a dedicated peer (`pilot-matrix`), and reads the agents' replies from that peer's inbox.

It is ~250 lines of pure standard-library Python — no SDK, no database — and runs as one always-on `systemd --user` service next to the bus.

## Why Matrix

- **You already have a client.** Element on phone and desktop, end-to-end-capable, mature, multi-account. No app to ship.
- **One room per agent.** Each agent gets its own conversation and posts under its own Matrix identity (`@agent-1`, `@agent-2`, …) with its own display name and avatar. A supervisor room (`#mesh`) carries the always-on agent.
- **Confidentiality via the network, not the protocol.** The bridge runs **no E2EE**. Instead the homeserver lives behind the same private overlay as the rest of the mesh (Tailscale/WireGuard, zero public ports — see [Tailscale-only inbound](../../README.md#tailscale-only-inbound)). Only devices on the tailnet can reach it. This keeps bot accounts simple and avoids cross-signing/key-backup fragility for non-interactive identities.

## How it works

```
Element (pilot)                Matrix homeserver              LoomMesh bus
     │                          (Conduit, tailnet-only)            │
     │  message in #agent ─────────────▶ /sync ──┐                 │
     │                                            ▼                 │
     │                                   ┌──────────────┐  send.py  │
     │  ◀── "seen" receipt (as @agent) ──│   bridge.py  │──────────▶│ inbox-<agent>
     │                                   │  (1 loop)    │           │
     │  ◀── reply (posted as @agent) ────│              │◀──────────│ inbox-pilot-matrix
     │                                   └──────────────┘  tail     │
```

Single-threaded poll loop:

1. **Matrix → mesh** (`poll_matrix`): long-poll `/sync`. For each room mapped to an agent, relay **only the pilot's** text messages to the mesh via `send.py pilot-matrix <agent>`. As soon as a message is seen, post a **read receipt under the agent's identity** so the pilot sees "seen" instantly — before the agent has composed anything.
2. **mesh → Matrix** (`poll_inbox`): tail `inbox-pilot-matrix.jsonl`. Each new line (an agent's reply) is posted into that agent's room, **under the agent's own access token**, so it shows up as `@agent` rather than a generic bot.

### Anti-loop rule

Agents post under their own Matrix identities, so a naive bridge would read those posts back on the next `/sync` and re-inject them into the mesh. The bridge relays to the mesh **only** events whose `sender` is the pilot; everything from the bot or from any agent identity is ignored. (This is the one bug that *will* bite you if you skip it.)

### Replying back: the agent's terminal is invisible

A subtle failure mode bites every agent that hasn't internalized the bridge: the pilot's eyes are on the Matrix client, **not** on the agent's terminal. An agent can receive a task from the pilot, do the work, print a perfect summary in its own pane — and look completely mute on the other side, because **only mesh messages cross the bridge, never an agent's console output**. The reply has to be sent back through the bus (to the `pilot-matrix` peer) or the pilot never sees it.

Don't rely on each agent remembering this. Make the rule structural: when the inbox reader (the `SessionStart` hook and the `read.py` CLI) renders a message whose `from` is the human client facade, it appends an explicit directive right under that message — *"the pilot reads via the client, NOT your terminal; reply with `mesh-send`"*. Two guard rails make it safe and quiet:

- **Closed whitelist of facade peers** (`{pilot-matrix, …}`), never derived from the unauthenticated `from` field — so an arbitrary sender string is never interpolated into the suggested command.
- **Only while unacked**, and **only for facade senders** — the directive disappears once the message is acked, and inter-agent traffic gets no such noise.

### Read receipts = the "seen" signal

The pilot's recurring question is "did it arrive?" before any reply comes back. The bridge answers it structurally: the read receipt is sent the moment the message is captured, decoupled from how long the agent takes to think. It is the structural answer to the pilot's "did it land?" — a native Matrix tick instead of a bespoke read-receipt UI.

## Identities

Each agent has its own Matrix account and access token:

- The **bot account** (`@mesh-bot`) owns `/sync` and inbound routing, and is the room creator/admin.
- Each **agent account** posts its own replies and read receipts.
- **Avatars**: set the member avatar via `PUT /profile/{user}/avatar_url` (under the agent token) *and* the room avatar via `PUT /rooms/{id}/state/m.room.avatar` (this needs the **admin/bot** token — agent tokens get `M_FORBIDDEN`). Modern Conduit serves **authenticated media only** (`/_matrix/client/v1/media/...`); old unauthenticated `/_matrix/media/v3/download` paths 404, so use a recent client.

## Files

```
bridges/matrix/
├── bridge.py                  # the bridge (pure stdlib)
├── config.example.json        # homeserver, tokens, peer, room map  → copy to config.json
├── agent_tokens.example.json  # per-agent access tokens             → copy to agent_tokens.json
├── matrix-bridge.service      # systemd --user unit
└── hooks/
    ├── matrix-typing-pretool.py   # PreToolUse: "<agent> is typing…"
    └── mesh-inbox-drain-stop.py   # Stop: never finish a turn with mail pending
```

`config.json` and `agent_tokens.json` hold access tokens and are **git-ignored** — only the `.example.json` templates are committed.

## Companion hooks (optional)

Two small Claude Code hooks make the agent side feel like a real chat. Both are pure stdlib, fail-open, and wired through `settings.json`. They are optional — the bridge works without them.

### Typing indicator — `bridges/matrix/hooks/matrix-typing-pretool.py` (PreToolUse)

Before each tool call, the agent refreshes a Matrix typing notification (30 s timeout) in its room, under its own identity. Element then shows "<Agent> is typing…" the whole time the agent is working — the cheap, structural answer to the pilot's "is it doing anything?". It costs **zero LLM tokens** (the hook is a plain HTTP POST, not the model) and never blocks or delays a tool: any error exits 0.

```jsonc
// settings.json
"PreToolUse": [
  { "matcher": "Edit|Write|Bash|Read|Grep|Glob|Task|WebFetch|NotebookEdit",
    "hooks": [{ "type": "command",
                "command": "python3 .../hooks/matrix-typing-pretool.py",
                "timeout": 5 }] }
]
```

### Anti-skip net — `bridges/matrix/hooks/mesh-inbox-drain-stop.py` (Stop)

When an agent is busy, the watcher *skips* the send-keys push (it cannot type into a mid-response pane), so a message waits in the inbox until the next sweep — and a careless read can miss it. This Stop hook closes the gap from the agent side: when the agent is about to go idle, it checks its inbox and, if a recent un-acked message is pending, **blocks the stop** and feeds back a prompt to drain the inbox first. The agent literally cannot end a turn with mail pending.

Loop safety is essential for a blocking Stop hook: it only counts messages `acked=false` within a 6 h window (ancient stragglers never block), and it blocks the *same* pending id-set at most twice before giving up — so a message the agent genuinely cannot ack can never wedge it. Everything is fail-open: a hook error always allows the stop.

```jsonc
// settings.json — add alongside any existing Stop hook
"Stop": [
  { "hooks": [{ "type": "command",
                "command": "python3 .../hooks/mesh-inbox-drain-stop.py",
                "timeout": 8 }] }
]
```

Both hooks resolve *which agent this session is* defensively: if they run inside a real tmux pane (`$TMUX` set), the pane's session name wins over `MESH_AGENT` — a shared tmux server can leak one agent's `MESH_AGENT` to every pane as a global, and trusting it blindly makes an agent act under the wrong identity (e.g. advance another agent's read cursor). Outside a pane (a remote SSH invocation) they fall back to `MESH_AGENT`. They also read the bridge config for the room map / tokens. **Hooks load at session start**, so the SessionStart-style ones take effect on the next restart; PreToolUse/Stop scripts are re-executed per event, so edits apply on the next tool/stop.

### Read receipts vs. milestones

These hooks pair naturally with the bridge's read receipts (the "seen" tick, sent the instant a message is captured) and with a **milestone convention**: for long work, the agent posts a one-line mesh update at each key step, and the typing indicator fills the gaps in between. That combination — instant "seen" → "typing…" → milestone lines → final reply — reproduces the felt responsiveness of a coding agent's live output, without streaming every internal step.

## Setup outline

1. Run a homeserver on the always-on host, reachable only over the tailnet (Conduit works well on small ARM boxes; expose it with `tailscale serve --bg --https=443 http://127.0.0.1:<port>` for a free, tailnet-only TLS cert).
2. Create one bot account + one account per agent (+ the pilot account). Grab an access token for each.
3. Create one room per agent and one supervisor room; invite the pilot and the matching agent; have each agent account join its room. Record the room IDs.
4. Copy `config.example.json` → `config.json` and `agent_tokens.example.json` → `agent_tokens.json`, fill in the homeserver, tokens, `pilot_user`, mesh paths and the room map.
5. Add `pilot-matrix` (or whatever you set as `mesh_peer`) to the bus peer list so `send.py` accepts it as a valid sender.
6. Install `matrix-bridge.service` under `~/.config/systemd/user/`, `enable --now`, and (with `loginctl enable-linger`) it survives reboots.

## Tradeoffs

- **No E2EE.** Acceptable here because the transport is already a private overlay; do not expose the homeserver publicly.
- **A service restart restarts the `/sync` cursor from `next_batch`**, not from history — the bridge intentionally never replays old messages, so nothing is double-relayed across restarts.
- **One pilot identity.** The relay filter keys on a single `pilot_user`. Multiple humans would need the filter widened to a set.

This component is decoupled from the rest: the bus, the webui, the API and the agents work unchanged whether or not the bridge is running. In the current deployment it is the main way the pilot reaches the agents — but nothing else depends on it.
