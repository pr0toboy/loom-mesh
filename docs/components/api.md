# Mesh API reference

The mesh exposes a **FastAPI** HTTP + WebSocket service. Default port `8765`, **bound to `127.0.0.1`** — this machine only. Binding it anywhere else is a deliberate act, and what may then reach the port is yours to decide: this project ships no firewall, and bearer tokens are all that stands there. See `docs/operations.md` for the minimum rule, and the trust model in the README for what a token grants.

## Authentication

Every endpoint except `/health` requires an HTTP Bearer token.

```
Authorization: Bearer <token>
```

Tokens are stored in `${MESH_HOME}/api-tokens.json` (mode `0600`):

```json
[
  { "token": "64-hex-chars..." },
  { "token": "another-token..." }
]
```

The API caches the token set in memory with a 30-second TTL (avoids re-reading the file on every request). Bootstrap generates at least one token when the file does not yet exist.

**Missing or invalid token → HTTP 401.**

For the WebSocket endpoint, the token can be passed either way:

- Query parameter: `GET /stream?token=<token>`
- Header: `Authorization: Bearer <token>` (takes precedence)

An invalid WebSocket token closes the connection with code `1008` (policy violation).

---

## Agent and peer names

The API distinguishes two sets:

| Set | Members | Used for |
|---|---|---|
| `AGENTS` | all persistent Claude instances | ticket creation targets, inbox owners, ticket owners |
| `ALL_PEERS` | `AGENTS` + pilot handles (`user-web`, `pilot-matrix`) | message recipients (send) |

Names follow the same format as `mesh.toml` agent names: lowercase, starts with a letter, `[a-z][a-z0-9-]{0,31}`. A request referencing an unknown peer or agent returns **HTTP 422**.

---

## Routes

### `GET /health` — no auth

Service liveness check. Returns immediately without touching disk.

```json
{ "status": "ok", "version": "0.1.0" }
```

---

### `GET /status`

Full snapshot of the mesh: agent liveness, service states, host metrics.

**Response**

```json
{
  "agents": {
    "agent-1": {
      "alive": true,
      "idle": true,
      "last_activity": "2026-05-25T10:00:00+02:00",
      "last_heartbeat": null,
      "tickets_pending": 2,
      "tickets_running": 1
    }
  },
  "services": {
    "mesh-watcher": "active",
    "mesh-api": "active",
    "ticket-dispatcher": "active",
    "agent-1": "active"
  },
  "host": {
    "cpu_temp_c": 51.2,
    "disk_used_pct": 34,
    "uptime_h": 72.4,
    "load_1m": 0.8
  },
  "generated_at": "2026-05-25T10:00:05+02:00"
}
```

- `alive` / `idle`: derived from the agent's tmux pane status-bar heuristic (last line contains `← for agents` = idle).
- `last_heartbeat`: populated only for remote-host agents whose liveness arrives via SSH heartbeat; `null` for local agents.
- Service states: `"active"`, `"inactive"`, or `"unknown"`.
- Host metrics: `null` if the host does not expose `/sys/class/thermal` or `df`.

---

### `GET /inbox/{agent}`

Retrieve messages from an agent's inbox.

**Query parameters**

| Param | Type | Default | Notes |
|---|---|---|---|
| `unread` | bool | `true` | `false` returns all messages |
| `limit` | int | `50` | max `200` |

**Response**

```json
{
  "agent": "agent-1",
  "messages": [
    {
      "id": "a1b2c3d4",
      "from": "agent-2",
      "to": "agent-1",
      "priority": "normal",
      "body": "Ready for review.",
      "ts": "2026-05-25T09:30:00+02:00",
      "reply_to": null
    }
  ],
  "total": 1
}
```

**Priority values**: `low`, `normal`, `high`, `urgent`.

---

### `POST /send`

Send a message to any peer.

**Request body**

```json
{
  "to": "agent-1",
  "body": "Deploy the new build when ready.",
  "priority": "normal",
  "from": "user-web",
  "reply_to": null
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `to` | string | yes | must be in `ALL_PEERS` |
| `body` | string | yes | max 65 536 chars, no null bytes |
| `priority` | string | no | default `"normal"` |
| `from` | string | no | default `"user-web"` |
| `reply_to` | string | no | ID of the message being replied to |

**Response**

```json
{
  "id": "a1b2c3d4",
  "queued_at": "2026-05-25T10:01:00+02:00",
  "delivered_live": true
}
```

`delivered_live`: `true` if the watcher managed to push `send-keys` to the recipient's tmux pane at send time.

**Errors**: 422 unknown destination, 502 runtime failure.

---

### `POST /ack`

Mark a message as read.

**Request body**

```json
{ "agent": "agent-1", "id": "a1b2c3d4" }
```

**Response**

```json
{ "acked": true, "count": 1 }
```

`acked: false` / `count: 0` if the message ID was not found in the inbox.

**Errors**: 404 unknown agent, 502 runtime failure.

---

### Tickets

Tickets are async work items dispatched to agents. They follow a state machine documented in `docs/components/tickets.md`.

**Ticket states**: `draft → armed → blocked → queued → running → done | failed | cancelled`

**Ticket ID format**: `tk-` followed by 6 hex digits (e.g., `tk-a1b2c3`).

#### `POST /tickets` — create a single ticket

**Request body**

```json
{
  "to": "agent-1",
  "prompt": "Refactor the auth module to use the new token store.",
  "priority": "normal",
  "dispatch_mode": "draft",
  "depends_on": [],
  "parent_ticket_id": null,
  "from": "user-web"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `to` | string | yes | must be in `AGENTS` |
| `prompt` | string | yes | |
| `priority` | string | no | default `"normal"` |
| `dispatch_mode` | string | no | `"draft"` (default) or `"armed"` |
| `depends_on` | list[string] | no | ticket IDs that must be `done` first |
| `parent_ticket_id` | string | no | links to a parent ticket |
| `from` | string | no | default `"user-web"` |

**Response**

```json
{
  "id": "tk-a1b2c3",
  "to": "agent-1",
  "status": "draft",
  "queued_at": "2026-05-25T10:05:00+02:00",
  "position": null
}
```

`position` is set when the ticket enters the `queued` state.

**Errors**: 422 unknown target agent.

---

#### `POST /tickets/bulk` — create a chain of tickets

Creates multiple related tickets in one call. Dependency placeholders (`_step0`, `_step1`, …) let you express ordering without knowing IDs ahead of time — `_stepN` refers to the ticket created at index `N` in the same request (forward references are rejected).

**Request body**

```json
{
  "to": "agent-1",
  "tickets": [
    { "prompt": "Set up the test environment." },
    { "prompt": "Run the integration tests.", "depends_on": ["_step0"] },
    { "prompt": "Publish the report.", "depends_on": ["_step1"] }
  ],
  "priority": "normal",
  "dispatch_mode": "armed"
}
```

**Response**

```json
{
  "ids": ["tk-aaa111", "tk-bbb222", "tk-ccc333"],
  "created_count": 3,
  "first_position": null
}
```

**Errors**: 422 unknown agent or malformed placeholder (e.g., `_step2` inside index 1).

---

#### `GET /tickets/{agent}` — list tickets

**Query parameters**

| Param | Type | Default | Notes |
|---|---|---|---|
| `status` | string | `"all"` | filter by state name or `"all"` |
| `limit` | int | `100` | max `500` |

**Response**: `{ "agent": "...", "tickets": [...], "total": N }`

Each ticket object:

```json
{
  "id": "tk-a1b2c3",
  "from": "user-web",
  "to": "agent-1",
  "status": "running",
  "dispatch_mode": "armed",
  "priority": "normal",
  "prompt": "Refactor the auth module...",
  "prompt_preview": "Refactor the auth module",
  "depends_on": [],
  "parent_ticket_id": null,
  "queued_at": "2026-05-25T10:05:00+02:00",
  "started_at": "2026-05-25T10:06:00+02:00",
  "completed_at": null,
  "tldr": null,
  "position": null,
  "full_response_url": null
}
```

`full_response_url` is set (e.g., `/tickets/agent-1/tk-a1b2c3/response`) once the ticket is `done`.

**Errors**: 404 unknown agent.

---

#### `GET /tickets/{agent}/{ticket_id}` — get one ticket

Returns the same ticket object as the list. **Errors**: 404 unknown agent or ticket, 422 malformed `ticket_id`.

---

#### `GET /tickets/{agent}/{ticket_id}/response` — full ticket output

Returns the raw text file written by the agent when it closed the ticket. Only available when `status == "done"`. **Errors**: 404.

---

#### `PATCH /tickets/{agent}/{ticket_id}` — update a ticket

All fields are optional. Allowed on `draft` and `armed` tickets only.

```json
{
  "position": 2,
  "prompt": "Updated scope.",
  "dispatch_mode": "armed"
}
```

**Errors**: 404 not found, 409 ticket is running or terminal, 422 malformed ID.

---

#### `DELETE /tickets/{agent}/{ticket_id}` — delete a ticket

No body. Returns `204 No Content`. **Errors**: 404, 409 (running), 422 malformed ID.

---

### `GET /graph`

Inter-agent activity graph for the webui's visualization view.

**Query parameter**: `window` — one of `1h`, `6h`, `24h`, `7d`.

**Response**

```json
{
  "window": "1h",
  "generated_at": "2026-05-25T10:10:00+02:00",
  "nodes": [
    { "id": "agent-1", "type": "agent", "label": "agent-1", "color": "#4CAF50" },
    { "id": "user", "type": "user", "label": "user", "color": "#2196F3" }
  ],
  "edges": [
    {
      "from": "user",
      "to": "agent-1",
      "weight": 5,
      "kind": "ticket",
      "last_ts": "2026-05-25T09:50:00+02:00"
    }
  ]
}
```

`weight` is the count of messages or tickets in the time window. `kind` is `"message"` or `"ticket"`.

**Errors**: 422 invalid `window`.

---

### `GET /contexts`

Context-window usage for all agents. Useful for detecting agents close to their token limit.

**Response**

```json
{
  "contexts": [
    {
      "agent": "agent-1",
      "tokens": 42000,
      "max_tokens": 200000,
      "pct": 21.0,
      "model": "claude-sonnet-4-6",
      "status": "idle",
      "source": "live",
      "updated_at": "2026-05-25T10:00:00+02:00"
    }
  ]
}
```

**`source` values**:

| Value | Meaning |
|---|---|
| `live` | Read directly from this host's Claude session |
| `cached` | From a remote host's context file, < 30 min old |
| `stale` | From a remote host's context file, ≥ 30 min old |
| `unknown` | Agent not found in any context source |

---

### `GET /conversation/{agent}`

Unified chronological view of an agent's inbox messages and tickets, newest-first. Useful for a chat-style UI.

**Query parameters**

| Param | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | `100` | 1–500 |
| `before` | string | — | ISO 8601 timestamp; returns items before this point (pagination cursor) |

**Response**

```json
{
  "agent": "agent-1",
  "items": [
    {
      "kind": "message",
      "id": "a1b2c3d4",
      "ts": "2026-05-25T10:00:00+02:00",
      "ts_ms": 1748167200000,
      "direction": "incoming",
      "from": "agent-2",
      "to": "agent-1",
      "priority": "normal",
      "body": "Build succeeded.",
      "reply_to": null,
      "acked": true,
      "summary": "Build succeeded."
    },
    {
      "kind": "ticket",
      "id": "tk-a1b2c3",
      "ts": "2026-05-25T09:50:00+02:00",
      "ts_ms": 1748166600000,
      "from": "user-web",
      "to": "agent-1",
      "priority": "normal",
      "status": "done",
      "prompt_preview": "Refactor the auth module",
      "tldr": "Refactored AuthMiddleware; 48 tests green.",
      "queued_at": "2026-05-25T09:50:00+02:00",
      "started_at": "2026-05-25T09:51:00+02:00",
      "completed_at": "2026-05-25T10:00:00+02:00",
      "dispatch_mode": "armed",
      "depends_on": []
    }
  ],
  "next_before": "2026-05-25T09:49:00+02:00"
}
```

Pass `next_before` as the `before` parameter on the next request to paginate. `null` means no more items.

**Errors**: 404 unknown agent (must be in `ALL_PEERS`).

---

### Push notifications

Two paths, and the chat one is the one to prefer.

**Through the bridge (no keys, no endpoints).** An agent sends to the chat facade peer, the [Matrix bridge](matrix-bridge.md) posts it into that agent's room, and the operator's client delivers a native push. Nothing about it lives in the API.

**Through the API (optional, off unless configured).** `POST /devices/register`, `GET /devices`, `DELETE /devices/{token}` and `POST /push` drive Firebase Cloud Messaging for a mobile app. They are mounted always, but `firebase-admin` is not in the base requirements and the credentials file is normally absent, in which case push initialisation logs a warning and every call degrades to a no-op. Install `mesh-api/requirements-notifications.txt` and drop the credentials in `$MESH_HOME` to turn it on. All four routes require a write token — they are writes, and one of them sends a notification to someone's phone.

The dashboard registers no device: it is live only while a tab is open (see [webui.md](webui.md)).

---

### `WS /stream` — real-time event stream

WebSocket endpoint for live updates. The webui subscribes here to receive live events while a tab is open.

**Connect and authenticate**

```
GET ws://<host>:8765/stream?token=<token>
```

Or pass `Authorization: Bearer <token>` as a header (takes precedence over query param).

**Subscription message** — must be sent within 10 seconds of connect, or the server closes the connection.

```json
{ "subscribe": ["status", "inbox:agent-1", "tickets:agent-1"] }
```

Valid topic patterns:

| Topic | Description |
|---|---|
| `status` | Heartbeat events only |
| `inbox:<agent>` | Fires when a new message arrives in that agent's inbox |
| `tickets:<agent>` | Fires on any ticket state change for that agent |

**Server → client events**

Heartbeat (every 30 seconds):
```json
{ "topic": "status", "event": "heartbeat", "ts": "2026-05-25T10:00:00+02:00" }
```

New inbox message:
```json
{ "topic": "inbox:agent-1", "event": "new_message", "ts": "2026-05-25T10:00:01+02:00" }
```

Ticket state change:
```json
{
  "topic": "tickets:agent-1",
  "event": "done",
  "file": "tk-a1b2c3.json",
  "ts": "2026-05-25T10:00:02+02:00"
}
```

Ticket event names match ticket state names: `started`, `done`, `failed`, `cancelled`, `blocked`, `armed`, `queued`, `updated`.

The event payload does not include the full object — the client should follow up with `GET /tickets/{agent}/{ticket_id}` or `GET /inbox/{agent}` to fetch the updated data.

**Implementation note**: the server uses `watchfiles` (inotify-based) when available; falls back to 30-second polling. A parallel heartbeat task ensures the connection stays alive even when no file events occur.

---

## Error codes summary

| Code | Condition |
|---|---|
| 401 | Missing or invalid Bearer token |
| 404 | Unknown agent/peer, ticket not found |
| 409 | Operation not allowed in current ticket state (e.g., patching a running ticket) |
| 422 | Validation failure: unknown peer, malformed ticket ID, invalid placeholder, out-of-range parameter |
| 502 | Runtime error during message send or ack |
