# The webui

A browser dashboard that gives the human pilot a desktop-and-mobile window into the mesh. It is **static files served by `mesh-api` itself** at `/ui/` — no separate server, no build step, no framework. A single `index.html` (vanilla JS, plus d3 for the activity graph) talks to the same HTTP+WS API documented in [api.md](api.md): it polls `/status` and `/contexts`, opens one WebSocket to `/stream` for live updates, and POSTs to `/send` and `/tickets` as a `user-web` peer.

The webui is a *client of the mesh, not a controller*. Everything it can do is an authenticated API call any other client could make; it holds no privileged path of its own. Its value is **awareness** (what is each agent doing right now) and **lightweight ticketing** (file a brief from any browser). It is paired with the [Matrix bridge](matrix-bridge.md), which carries the conversational surface and the "notify me when I'm away" role the webui deliberately does not.

## Why a browser dashboard, not a native app

- **Nothing to ship.** A static page served by the API you already run. No app store, no signing key, no sideload pipeline, no emulator. A change is a page reload.
- **Reuses the existing surface.** The dashboard is a thin view over `/status`, `/contexts`, `/graph`, `/conversation`, and `/stream` — endpoints the API exposes anyway. Adding the webui added almost no backend code.
- **Inspectable like the rest of the system.** The files are plain HTML/JS/JSON on disk under `dashboard-web/`; `cat` and the browser devtools are the whole toolchain.
- **Works everywhere the tailnet reaches.** Desktop browser at the keyboard, phone browser when away. For real push (phone asleep, tab closed), the Matrix client takes over — see [Tradeoffs](#tradeoffs).

## Files

```
dashboard-web/                  # served by mesh-api at /ui/
├── index.html                 # the whole dashboard (markup + CSS + JS in one file)
├── registry.json              # display layer: agent id → { display name, avatar } (optional; git-ignored — you create it, the repository ships none)
├── d3.v7.min.js               # vendored, for the activity graph (no CDN at runtime)
└── office/                    # optional "office" visualization (animated agents in rooms)
    ├── index.html
    └── world.json             # room layout + per-agent home placement
```

`mesh-api` mounts this directory as static files:

```python
app.mount("/ui", StaticFiles(directory=DASHBOARD_DIR, html=True), name="ui")
```

so the dashboard lives behind the same Tailscale-only ingress and the same origin as the API — no CORS dance, no second port.

> **Display layer is local-only.** `registry.json` (real display names, avatars) and the `office/` sprites carry owner-specific identity. The public repo ships only a generic pattern; the populated versions live outside git, like the charters and tokens.

## Authentication

Same bearer token as every other API client. There is no login form and no session cookie: the page asks for the token on first load and keeps it in this browser's `localStorage`.

It is **not** stored in a file next to the page. It used to be, in a `config.js` the page loaded — and `/ui` is served as plain static files with no authentication, so anyone who could reach the port could fetch that file and read a token with full API access. A page cannot authenticate its own reader; the credential belongs in the reader's browser. Every `fetch` adds `Authorization: Bearer <token>`; the WebSocket passes it as `?token=<token>` on connect.

```js
let TOKEN = localStorage.getItem('loom.token') || askToken();   // asked for on first load
const api = (path, opts = {}) => fetch(BASE + path, {
  ...opts,
  headers: { ...opts.headers, 'Authorization': 'Bearer ' + TOKEN },
});
```

The trust model is identical to the mobile-era one: single operator, token-equals-admin, revoked by rotating the file on the server. Because the page is served over Tailscale only, the token never crosses a public network. Do not expose `/ui/` (or any of the API) to the open internet — `localStorage` is readable by any script on the origin, so the only thing keeping it private is the network boundary.

## Live updates

The dashboard opens **one** WebSocket to `/stream` and subscribes to the topics it cares about — `status`, and `inbox:<agent>` / `tickets:<agent>` for each agent in the roster:

```js
const ws = new WebSocket(`${WS_BASE}/stream?token=${TOKEN}`);
ws.onopen = () => ws.send(JSON.stringify({
  subscribe: ['status', ...agents.flatMap(a => [`inbox:${a}`, `tickets:${a}`])],
}));
ws.onmessage = (ev) => dispatch(JSON.parse(ev.data));   // re-render the affected card / feed row
```

Each event is a small envelope (`{topic, event, ts, ...}`) — it does **not** carry the full object. On an `inbox:*` or `tickets:*` event the page does a targeted follow-up fetch (`/conversation/<agent>` or `/tickets/<agent>/<id>`) to pull the fresh data, then updates only the affected card. A `status` heartbeat every 30 s keeps the socket warm and doubles as the "is the stream alive" signal.

Reconnect is exponential backoff with jitter (1 s → 2 s → 4 s → … cap 30 s). When the socket drops, a small pill in the header flips to *Reconnecting…*, and on reopen the page refetches `/status` once to fill in anything missed before resuming the live stream. There is **no** polling fallback timer beyond that one-shot resync — if the tab is closed there is nothing to update, which is the whole reason notifications are Matrix's job, not the webui's.

## The views

### Dashboard (agent cards + graph)

The landing view is a grid of **agent cards**, one per agent, each showing: display name + avatar (from `registry.json` when present, else the raw id), a status dot (idle / busy / down), context-window usage (`pct` from `/contexts`, with the model name), an unread count, and the timestamp of the last activity. Cards recompute only on events whose topic matches their agent, so an unrelated message never re-renders the whole grid.

Above or beside the grid is the **activity graph** — a d3 force-directed view of `/graph` (nodes = agents + the `user` peer, edges weighted by message/ticket volume in a selectable window of `1h / 6h / 24h / 7d`). It answers "who's been talking to whom lately" at a glance.

### Conversation (per-agent timeline)

Clicking a card opens that agent's **conversation** — a unified chronological timeline of messages *and* tickets from `/conversation/<agent>`, newest at the bottom, paginated via the `before` / `next_before` cursor. Each item renders by kind: a plain message bubble (with the server's `summary` line bolded on top and the full body collapsed past a few lines), or a ticket card (status, TL;DR, prompt preview). Outgoing items the pilot sent as `user-web` show a sent/seen tick driven by the `acked` flag. A compose box at the bottom POSTs to `/send` with `from: "user-web"`.

### Compose ticket

A small form to file a ticket: pick the assignee, write the prompt, optionally set `depends_on` (a multi-select fed by every agent's open tickets, so you can chain "agent-2 sets up → agent-3 deploys → agent-4 smoke-tests" inline) and a dispatch mode (`draft` or `armed`). Submit calls `POST /tickets`; the new ticket card appears optimistically and is confirmed by the next `tickets:*` stream event.

### Office (optional)

A playful spatial view (`/ui/office/`) that renders each agent as an animated sprite walking around rooms of a building, its position/pose reflecting online/idle/busy/down state from `/status`. Layout and per-agent "home" room come from `world.json`. It is purely a visualization — no control — and is entirely optional; the dashboard works without it.

## What the webui is not

- **Not a remote terminal.** The pilot can read what agents are doing and write into the bus (messages and tickets), but cannot run arbitrary shell on agent hosts.
- **Not multi-tenant.** One operator, one token. No account UI, no per-user scoping.
- **Not a notifier.** It only updates while a tab is open. Push-when-away is delegated to the Matrix client — that division is intentional, not a gap.
- **Not offline-first.** Reading requires the API; a dropped connection shows a stale-data banner, not a usable cache.

## Tradeoffs

- **No push of its own.** Closing the tab means no notifications — by design. The [Matrix bridge](matrix-bridge.md) carries the away-from-keyboard role with native client push, so the webui stays a dumb, stateless view. Two surfaces, but each tiny and decoupled.
- **`localStorage` token.** Convenient, and acceptable *only* because the origin is Tailscale-only. The moment you'd expose the API publicly, this becomes a real XSS-to-token-theft risk — so don't.
- **Roster is client-side.** The page learns the agent list from `/status` (or a small bootstrap), so a freshly added agent shows up on the next load with no rebuild — but a stale tab won't know about it until refreshed.
- **Display layer drift.** `registry.json` is read only by the page; renaming an agent's display name there has no effect on routing (ids stay canonical), which is the point — but it means the dashboard and the bus can show different labels for the same agent if `registry.json` falls out of sync.
