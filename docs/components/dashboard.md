# The web dashboard

A single-file, dependency-light web UI that gives the human pilot a read-only,
at-a-glance view of the mesh from any browser on the tailnet — no app install,
no build step. It is for *monitoring* — who's up, who's working, how the
machines are doing, how traffic is flowing. Conversing with agents and
dispatching work happens elsewhere: from a chat client through the
[Matrix bridge](matrix-bridge.md), or from the command line.

It is deliberately **read-only**. It never sends as an agent, never dispatches a
ticket. It only calls `GET /status`, `GET /graph` and subscribes to the
`/stream` websocket (see [api.md](api.md)). That keeps the surface tiny and the
token it needs scoped to reads.

## Stack

- One `index.html` (~700 lines: inline CSS + vanilla JS, no framework, no
  bundler) plus `d3.v7.min.js` for the force graph.
- The bearer token is asked for on first load and kept in the browser's
  `localStorage`. It is not stored beside the page: `/ui` is served without
  authentication, so a token in a file there is readable by anyone who can
  reach the port. Note there is no read-only token — any token in
  `api-tokens.json` can write.
- Optional per-agent avatars live in `avatars/<agent-id>.png` (gitignored, so
  agent identities stay out of the repo). Agents listed in the `AVATARS` set get
  their PNG, clipped to a circle on both the cards and the graph nodes; the rest
  fall back to an emoji icon.
- Served as a static mount by `mesh-api` under `/ui/`, so it shares the API's
  origin (same bearer token, no CORS). Tailscale-only inbound, like everything
  else.

## What it renders

Three sections, refreshed from the websocket (polling fallback every few
seconds):

### Agents
One card per agent. A status dot encodes liveness:
- **green** — online and idle,
- **pulsing orange** — working (a ticket is running, or the agent isn't idle),
- **red** — down (with a red card border and red label so it can't be missed).

Each card carries the agent's emoji icon, its name, time since last activity,
and badges for running / pending ticket counts.

### Machines (hosts)
One card per host, driven by the `hosts` map in `/status`:
- state: **online** / **intentionally off** (e.g. a workstation the owner
  powered down on purpose — surfaced distinctly from a crash) / **offline**;
- for a server: CPU temperature, disk usage, uptime, and CPU **load shown as a
  percentage** (`load_1m ÷ cores × 100`) rather than a raw load average, which
  is more legible at a glance;
- for a phone: battery percentage, battery temperature, and charging state.

### Mesh graph
A D3 force-directed graph of message flow:
- nodes are agents; the human's multiple identities (phone, Matrix, terminal)
  are **merged into a single node** to avoid duplicates;
- edges are messages exchanged over the last hour — the number and the line
  thickness both encode volume;
- particles animate along each edge from sender to recipient, tinted with the
  sender's colour, at a rate proportional to traffic.

## Layout notes (force graph)

A force layout in a small fixed viewport needs a little care or nodes drift
off-screen. The settings that keep it contained:

- **seed positions on a circle** around the centre — D3's default seeds nodes in
  a phyllotaxis spiral around the origin `(0,0)`, i.e. the top-left corner, so
  without seeding the graph visibly "explodes" out of that corner on first
  paint;
- **`forceX` / `forceY` toward the centre** instead of relying on `forceCenter`
  alone — `forceCenter` only recentres the *centroid*, it does nothing to stop
  charge repulsion from pushing individual nodes past the edges. A stronger Y
  pull compensates for a short (wide-aspect) viewport;
- **bounded charge** (`distanceMax`) and a **collision radius** so nodes spread
  without flinging apart;
- a **tick clamp** that keeps every node inside the frame margins;
- a short **off-screen warm-up** (`sim.tick()` a few dozen times before the
  first paint) so the first frame is already settled — no visible jump.

## Why a separate web UI at all

The phone app is the rich client, but a browser tab is the fastest way to *look*
at the mesh from a laptop without installing anything, and a read-only view is
safe to leave open. Keeping it a single static file (no build, no server beyond
the static mount it already has) means it costs almost nothing to maintain.
