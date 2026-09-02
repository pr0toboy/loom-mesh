# LoomMesh web dashboard

A single-file, read-only web UI for watching the mesh: who's online, who's
working, host health (CPU / disk / load / battery), and a live force-directed
graph of message flow between agents. Tailnet-only, no build step.

## What it shows

- **Agents** — one card per agent with a status dot: green = online/idle,
  pulsing orange = working, **red = down**. Each agent has an emoji icon and
  its running/pending ticket counts.
- **Machines (hosts)** — primary / secondary / mobile, with online / *intentionally
  off* / offline state, CPU temp, disk, uptime, CPU load (as a %), and phone
  battery + temperature.
- **Mesh graph** — D3 force layout. Nodes = agents (the human's identities are
  merged into a single node), edges = messages exchanged in the last hour
  (number + thickness = volume), with particles animating from sender to
  recipient.

It polls `GET /status` and `GET /graph` and upgrades to the `/stream` websocket
when available (falls back to polling otherwise).

## Setup

**The token is not stored here.** The page asks for it on first load and keeps
it in your browser's `localStorage`. It used to be read from a `config.js` file
next to `index.html` — but `/ui` is served as plain static files with no
authentication, so anyone who could reach the port could fetch that file and
read a token with full API access. A page cannot authenticate its own reader;
the credential belongs in the reader's browser, not in the page.

Note there is no read-only token: any token in `api-tokens.json` can write. The
dashboard only reads, but the token you paste into it could do more if it
leaked. See the trust model in the top-level README.

**`d3.v7.min.js` is vendored here**, next to `index.html` — D3 v7.9.0,
ISC licensed, © 2010-2023 Mike Bostock. It is committed rather than fetched
from a CDN so the dashboard works on a machine with no internet, and so a
fresh clone renders the graph without a setup step.

This file used to be a manual `curl` the reader was told to run, described as
gitignored — it was not in `.gitignore`, it was simply absent, and
`docs/components/webui.md` already called it vendored. The visible cost was
worse than the disagreement: the page served 200, the agent cards rendered,
and the graph — the reason the dashboard exists — was an empty rectangle,
reported only to a browser console. `tools/tests/test_html_assets_exist.py`
now fails if any HTML here loads a file the repository does not carry.

Optionally, drop per-agent avatars in `dashboard-web/avatars/<agent-id>.png`
(the same images you use on Matrix, if you bridge) and list those ids in the
`AVATARS` set near the top of the script. Agents without an avatar fall back to
an emoji icon, so this is entirely optional. The `avatars/` folder is
gitignored — your agent identities stay out of the repo.

Then serve the folder. The simplest path is to let `mesh-api` serve it as a
static mount so the dashboard shares its origin (no CORS, same bearer token):

```python
# in your FastAPI app, AFTER all API routes so they aren't shadowed:
from fastapi.staticfiles import StaticFiles
app.mount("/ui", StaticFiles(directory="dashboard-web", html=True), name="ui")
```

Browse to `https://<your-tailnet-host>:8443/ui/`.

See `docs/components/dashboard.md` for the design notes.
