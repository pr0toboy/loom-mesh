# Getting started — your first mesh in 5 minutes

By the end of this guide you will have two AI agents running in tmux sessions, exchanging messages, and accepting tickets from the command line.

> **What you need before starting.** An authenticated Claude Code CLI — the mesh runs agents, it does not replace them, and every agent consumes your own Claude quota. That is the real prerequisite; the installation itself takes one command.
>
> Everything the bootstrap calls is in this repository: the bus (`bus/send.py`, `bus/read.py`, `bus/watcher.sh`, `bus/ticket-complete.py`), the FastAPI backend (`mesh-api/`) and the dashboard it serves at `/ui/` (`dashboard-web/`), the Claude Code hooks (`hooks/`), and the Matrix bridge (`bridges/matrix/`). `bootstrap.sh` installs the bus into your mesh home and writes the systemd units that run it.

---

## 1. Clone the repo

```bash
git clone <repo-url> loom-mesh
cd loom-mesh
```

## 2. Install prerequisites

```bash
# System tools
sudo apt install tmux inotify-tools jq

# Python library
pip install --user pydantic     # or: pip install -r requirements.txt
```

Verify the Claude Code CLI is installed and authenticated:

```bash
claude --version       # prints a version number
```

If `claude` is not found, install it from [claude.ai/claude-code](https://claude.ai/claude-code) and run `claude` once to authenticate.

## 3. Create `mesh.toml`

Create a minimal config with two agents on your local machine:

```toml
[mesh]
home     = "~/mesh"
api_port = 8765

[hosts.primary]
user = "$USER"
host = "localhost"

[[agents]]
name             = "builder"
role             = "Writes and tests code"
model            = "claude-sonnet-4-6"
workdir          = "~/builder"
charter_template = "worker"
host             = "primary"

[[agents]]
name             = "ops"
role             = "Monitors health and handles housekeeping"
model            = "claude-haiku-4-5-20251001"
workdir          = "~/ops"
charter_template = "supervisor"
host             = "primary"
```

Save it as `mesh.toml` in the repo root.

Validate before deploying:

```bash
python3 config_schema/validate.py mesh.toml
# ✓ mesh.toml is valid  (2 agents, 1 hosts)
```

## 4. Deploy

```bash
bash bootstrap.sh --no-systemd --skip-health mesh.toml
```

`--no-systemd` skips service installation for now — you'll start the agents manually in the next step.

Expected output:

```
[OK]    Config valid: 2 agents, mesh home=~/mesh, api=127.0.0.1:8765
[OK]    peers.py and peers.sh written to ~/mesh
[OK]    api-tokens.json generated
[OK]      builder: CLAUDE.md rendered from worker
[OK]      ops: CLAUDE.md rendered from supervisor
[OK]    Workspace dirs and charters done
```

What was created:

```
~/mesh/
  peers.py            ← canonical agent list
  peers.sh
  api-tokens.json     ← bearer token for the HTTP API

~/builder/
  CLAUDE.md           ← charter rendered from templates/charter/worker.md

~/ops/
  CLAUDE.md           ← charter rendered from templates/charter/supervisor.md
```

## 5. Start the agents

Each agent lives in its own tmux session. Open two terminals (or tmux panes) and run:

```bash
# Terminal 1 — builder agent
tmux new-session -s builder -c ~/builder
# inside: claude --permission-mode bypassPermissions --model claude-sonnet-4-6
```

```bash
# Terminal 2 — ops agent
tmux new-session -s ops -c ~/ops
# inside: claude --permission-mode bypassPermissions --model claude-haiku-4-5-20251001
```

Or start both detached and attach to watch:

```bash
tmux new-session -d -s builder -c ~/builder \
  "while true; do claude --permission-mode bypassPermissions --model claude-sonnet-4-6; sleep 2; done"

tmux new-session -d -s ops -c ~/ops \
  "while true; do claude --permission-mode bypassPermissions --model claude-haiku-4-5-20251001; sleep 2; done"

tmux attach -t builder    # watch builder; Ctrl-b d to detach
```

## 6. Send your first message

From any terminal on the same machine:

```bash
python3 ~/mesh/send.py ops builder normal "Hello from ops — are you online?"
```

The message lands in `~/mesh/inbox-builder.jsonl`. Without the watcher running, builder won't be notified automatically — but it will see the message at its next session start (the SessionStart hook reads unread inbox automatically).

To read it manually from builder's perspective:

```bash
python3 ~/mesh/read.py builder
```

Expected output:

```
=== unread in inbox-builder (1 message(s)) ===

[2026-05-25T10:00:00+02:00] ops → builder [normal] id=a1b2c3d4
Hello from ops — are you online?
```

Acknowledge it:

```bash
python3 ~/mesh/read.py builder --ack a1b2c3d4
```

## 7. Send a ticket

Tickets are async work items. Dispatch one to builder:

```bash
python3 - <<'EOF'
import sys, json
sys.path.insert(0, '.')
# Quick ticket via the mesh send path (dispatcher not needed for this demo)
import subprocess, secrets
tid = "tk-" + secrets.token_hex(3)
import pathlib, json, datetime
ticket = {
    "id": tid,
    "from": "ops",
    "to": "builder",
    "priority": "normal",
    "dispatch_mode": "armed",
    "status": "queued",
    "prompt": "Write a hello_world.py that prints the current time.",
    "depends_on": [],
    "queued_at": datetime.datetime.now().isoformat(),
}
d = pathlib.Path.home() / "mesh" / "tickets" / "builder" / "queued"
d.mkdir(parents=True, exist_ok=True)
(d / f"{tid}.json").write_text(json.dumps(ticket, indent=2))
print(f"Ticket {tid} written to {d}/{tid}.json")
EOF
```

Or, once the mesh-api is running, use the HTTP API (see `docs/components/api.md`):

```bash
TOKEN=$(python3 -c "import json; print(json.load(open('$HOME/mesh/api-tokens.json'))[0]['token'])")
curl -s -X POST http://localhost:8765/tickets \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"to":"builder","prompt":"Write hello_world.py that prints the current time.","dispatch_mode":"armed"}' \
  | python3 -m json.tool
```

## 8. Start the live push watcher (optional)

Without the watcher, agents only see new messages at session start. To enable sub-second push:

```bash
bash ~/mesh/watcher.sh &
```

Now when you send a message to `builder`, the watcher injects it as a keystroke into the `builder` tmux pane within ~1 second.

For persistent operation, enable the systemd service:

```bash
# Re-run bootstrap without --no-systemd
bash bootstrap.sh --skip-health mesh.toml

systemctl --user start mesh-watcher.service
systemctl --user enable mesh-watcher.service
```

## Done

Your mesh is running. You have:
- Two agents with distinct charters in tmux sessions
- A shared message bus at `~/mesh/`
- A bearer-token protected HTTP API (once mesh-api is started)
- Idempotent deploy — re-running `bootstrap.sh` won't overwrite your customized `CLAUDE.md` files or regenerate your tokens

**Next steps**:
- Read `docs/components/bus.md` for the full message bus mechanics
- Read `docs/components/tickets.md` for the ticket state machine
- Read `docs/components/api.md` for the full HTTP+WS API reference
- Add more agents to `mesh.toml` and re-run `bootstrap.sh`
- Set up `mesh-api.service` (it serves the webui at `/ui/`) and the Matrix bridge for remote control (see `docs/operations.md` and `docs/components/matrix-bridge.md`)
