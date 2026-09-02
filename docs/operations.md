# Operations guide

How to set up, run, and maintain the mesh.

---

## First-time setup

### 1. Clone and install prerequisites

```bash
git clone <repo-url> loom-mesh
cd loom-mesh
pip install -r requirements.txt
```

Also install system packages (Debian/Ubuntu):

```bash
sudo apt install tmux inotify-tools jq
```

Verify the Claude Code CLI is installed and authenticated:

```bash
claude --version        # prints version
claude --help           # confirms CLI is usable
```

### 2. Write `mesh.toml`

Copy the example and edit it:

```bash
cp config_schema/mesh.example.toml mesh.toml
$EDITOR mesh.toml
```

Validate before deploying:

```bash
python3 config_schema/validate.py mesh.toml
```

Dry-run to preview what bootstrap would do without writing anything:

```bash
bash bootstrap.sh --dry-run mesh.toml
```

### 3. Deploy

```bash
bash bootstrap.sh mesh.toml
```

This creates:
- Agent working directories + `CLAUDE.md` (charter) in each
- `${MESH_HOME}/peers.py` and `peers.sh` — canonical agent list consumed by all services
- `${MESH_HOME}/api-tokens.json` — bearer tokens (generated once, never overwritten)
- Systemd user units for each agent + infrastructure services (unless `--no-systemd`)

### 4. Verify

```bash
systemctl --user status mesh-api.service mesh-watcher.service ticket-dispatcher.service
curl -s http://localhost:8765/health          # {"status":"ok","version":"..."}
```

---

## Day-to-day operations

### Start / stop a service

```bash
systemctl --user start   mesh-api.service
systemctl --user stop    mesh-api.service
systemctl --user restart mesh-api.service
```

### Check service health

```bash
systemctl --user status mesh-api.service
journalctl --user -u mesh-api.service -n 50 --no-pager
```

### Watch the mesh watcher log

```bash
tail -f ${MESH_HOME:-~/mesh}/logs/watcher.log
```

### See what each agent is doing right now

```bash
# List all tmux sessions
tmux ls

# Attach to a session (read-only)
tmux attach-session -t agent-1 -r
# Detach: Ctrl-b d
```

### Send a message to an agent

```bash
python3 ~/mesh/send.py <from> <to> normal "message body"
# e.g.:
python3 ~/mesh/send.py ops agent-1 normal "Please compact and restart."
```

### Read an agent's inbox

```bash
python3 ~/mesh/read.py agent-1
python3 ~/mesh/read.py agent-1 --ack <message-id>
```

### Close a ticket from the shell

```bash
python3 ~/mesh/ticket-complete.py tk-a1b2c3 --tldr "Done — summary here."
```

---

## Restricting who can reach the API

**This project ships no firewall, and the API cannot protect itself.** It binds
to `127.0.0.1` by default, which means nothing off this machine reaches it. The
moment you change that — to open the dashboard on a phone — every host that can
route to that address can talk to the API, and a bearer token is all that stands
between them and every agent's inbox. Writing to an inbox is close to running
code as that agent, so this is worth ten minutes.

The rule you want is narrow: allow the port on loopback and on the one interface
you actually use (a VPN interface, typically), drop everything else. With
`nftables`:

```bash
# Adjust: 8765 = your api_port, wg0/tailscale0 = the interface you trust
sudo nft add table inet loom
sudo nft add chain inet loom input '{ type filter hook input priority 0; }'
sudo nft add rule inet loom input iif lo accept
sudo nft add rule inet loom input iifname "tailscale0" tcp dport 8765 accept
sudo nft add rule inet loom input tcp dport 8765 drop
```

The `iptables` equivalent, if that is what your distribution uses:

```bash
sudo iptables -A INPUT -i lo -p tcp --dport 8765 -j ACCEPT
sudo iptables -A INPUT -i tailscale0 -p tcp --dport 8765 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8765 -j DROP
# Persist them (iptables-persistent, or your distribution's mechanism)
```

**Verify from another machine, never from this one.** A test from the host
against its own address travels over loopback and answers `200` whatever the
firewall says — it is the single most common way to conclude that a port is
protected when it is wide open:

```bash
# From a DIFFERENT machine on the same network:
curl -m 5 -o /dev/null -w '%{http_code}\n' http://<this-host-lan-ip>:8765/health
# 000 = filtered (what you want).  200 = reachable.
```

Two related settings, covered in the trust model: `MESH_API_NO_AUTH=1` opens
*reads* without a token, and the service refuses to start if it is set while
listening on a non-loopback address (override: `MESH_API_ALLOW_OPEN_BIND=1`,
which is you saying the firewall above exists and you meant it).

## Monitoring

### Log files

| File | What it contains |
|---|---|
| `${MESH_HOME}/logs/watcher.log` | inotify events + push decisions |
| `${MESH_HOME}/logs/watcher.stdout.log` | stdout of the watcher process |
| `${MESH_HOME}/logs/watcher.stderr.log` | stderr of the watcher process |
| `${MESH_HOME}/logs/scope-classifier.log` | per-tool-call scope decisions (ALLOW / WOULD_DENY) |
| `${MESH_HOME}/logs/compact-actions.log` | auto-compact events from the supervisor |
| `journalctl --user -u mesh-api` | API access + error log |
| `journalctl --user -u ticket-dispatcher` | dispatcher state transitions |

### Key metrics to watch

```bash
# Disk usage
df -h /

# CPU temperature (Raspberry Pi)
cat /sys/class/thermal/thermal_zone0/temp   # divide by 1000 for °C

# Load average
uptime

# Unread message backlog (stale if > 10)
python3 ~/mesh/read.py agent-1 | grep -c '^\['

# Running tickets
ls ~/mesh/tickets/agent-1/running/

# API liveness
curl -sf http://localhost:8765/health && echo OK
```

### Detecting a stuck agent

Signs an agent has frozen:
- tmux pane shows `ESC to interrupt` for > 30 min
- No new lines in the agent's inbox `state-<agent>.json`
- Supervisor escalates with priority `urgent`

Action: attach to the pane, press `Esc`, wait for the prompt, then `/compact` if context is high.

---

## Troubleshooting

### Watcher is down (agents not receiving push)

```bash
systemctl --user status mesh-watcher.service
journalctl --user -u mesh-watcher.service -n 30 --no-pager
```

If it's in a crash loop, check `${MESH_HOME}/logs/watcher.stderr.log`. Common causes:
- `inotifywait` not installed → `sudo apt install inotify-tools`
- `MESH_HOME` path does not exist → create it or check `mesh.env`
- SSH to remote host failing → `ssh -i <key> <user>@<host> echo ok`

Restart:

```bash
systemctl --user restart mesh-watcher.service
```

Agents still receive messages at their next session start (SessionStart hook reads inbox); the watcher only handles real-time push.

### Dispatcher is stuck (tickets not advancing past `queued`)

```bash
systemctl --user status ticket-dispatcher.service
journalctl --user -u ticket-dispatcher.service -n 30 --no-pager
```

Check for tickets in `running/` that have no active agent (orphaned running state):

```bash
ls ~/mesh/tickets/*/running/
```

If a ticket is stuck in `running/` because the agent crashed, move it manually:

```bash
mv ~/mesh/tickets/agent-1/running/tk-abc123.json \
   ~/mesh/tickets/agent-1/failed/
```

Then restart the dispatcher:

```bash
systemctl --user restart ticket-dispatcher.service
```

### Agent freeze (Claude Code session unresponsive)

1. Attach to the tmux session: `tmux attach -t agent-1 -r`
2. Press `Esc` to interrupt the current tool call
3. If the session is completely locked, kill it: `tmux kill-session -t agent-1`
4. The systemd service will restart it automatically (the `while true` loop in `agent@.service`)

### Agents missing / sessions dying (shared tmux server)

Symptom: one or more agents have no tmux session and never appear in `tmux ls` (or in the Remote Control hub), even though `systemctl --user status <agent>.service` reports `active (exited)` with `status=0/SUCCESS`. Happens after a host reboot **or** after a `systemctl --user restart` of a single agent.

Two known causes — don't stop at the first:

1. **Missing `--remote-control <name>` flag** in the unit's `ExecStart`: the agent runs but never registers with the Remote Control hub. Add the flag and restart.

2. **Shared tmux server killed via cgroup** (the real culprit). All agents share one tmux server. With the default `KillMode=control-group`, the first agent to call `tmux new-session` adopts the server into *its own* cgroup. From then on, a `systemctl --user restart` (or stop) of *that* agent kills its entire cgroup — taking the shared server, and therefore every other agent's session, down with it. An earlier hypothesis blamed a boot-time `new-session` race and tried to fix it with `After=` serialization; that addressed the wrong root cause and didn't hold (a restart still kills everything). Verify ownership:

   ```bash
   cat /proc/$(tmux display-message -p '#{pid}')/cgroup
   ```

   If that points at a single agent's `*.service`, the trap is armed.

Diagnose by comparing *declared* services against *live* sessions — never trust `active (exited)` alone (it only means `tmux new-session` returned 0):

```bash
tmux ls                                    # which sessions actually exist
ps aux | grep '[c]laude'                   # which agent processes are running
systemctl --user is-active <agent>.service
```

Immediate fix — restart the missing agents (a tmux server already exists now):

```bash
systemctl --user restart <agent>.service
```

Permanent fix (applied): give the tmux server its own unit so no single agent owns it.

- `tmux-server.service` owns the server in its own cgroup, with a `mesh-keepalive` session and `exit-empty off` so it never self-exits. Its `KillMode` is left at the default (control-group) — stopping *that* unit *should* bring the whole server down.
- Every `agent@.service` declares `After=`/`Wants=tmux-server.service` (server is up before any agent → also removes the boot race) and `KillMode=process` (restarting an agent only kills its own `claude`/loop, never the shared server).

`KillMode=process` protects the currently running server immediately after a `daemon-reload`; the clean handover (server living in `tmux-server.service`'s cgroup) takes effect on the next reboot, when `tmux-server.service` starts first. `After=` only orders startup; it does not couple restarts, so restarting one agent never cascades to the others.

### API returning 401 on all requests

Token file may have wrong permissions or be empty:

```bash
ls -l ~/mesh/api-tokens.json          # must be 0600
python3 -c "import json; print(json.load(open('$(echo ~/mesh/api-tokens.json)')))"
```

If the file is missing, re-run bootstrap to regenerate:

```bash
bash bootstrap.sh mesh.toml
```

### mesh-api fails to start (port already in use)

```bash
ss -tlnp | grep 8765
```

Kill the stale process and restart:

```bash
systemctl --user restart mesh-api.service
```

### Remote agent not receiving messages

Check:
1. SSH connectivity: `ssh <user>@<remote-host> echo ok`
2. tmux session exists on remote: `ssh <user>@<remote-host> tmux ls`
3. Watcher log for SKIP lines: `grep SKIP ~/mesh/logs/watcher.log | tail -20`

### WSL agent: `cmd.exe` / `powershell.exe` fail with "Invalid argument" or "command not found"

Symptom: a WSL2-hosted agent reports it cannot invoke Windows binaries, even though `WSL_INTEROP` is set, the socket exists, and `binfmt_misc` is `enabled`.

Cause: when a process is spawned by `systemd --user`, its PATH is the minimal systemd default and **does not include any `/mnt/c/*` directory**. Bash cannot resolve relative `.exe` names, so `binfmt_misc` never gets a chance to dispatch them through `/init`.

Verify:

```bash
tr '\0' '\n' < /proc/<agent-pid>/environ | grep ^PATH=
```

If you see no `/mnt/c/...` entry, this is the problem. The shipped `agent@.service` template includes the Windows paths by default (since 2026-05-28); if you are running an older deployment, re-run bootstrap or patch the unit:

```ini
Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin:/mnt/c/WINDOWS/system32:/mnt/c/WINDOWS:/mnt/c/WINDOWS/System32/Wbem:/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/:/mnt/c/Program Files/PowerShell/7/"
```

Then `systemctl --user daemon-reload && systemctl --user restart <agent>.service`. The new agent process inherits the extended PATH; `cmd.exe`, `powershell.exe`, and `pwsh.exe` resolve in relative form.

Workaround without restart: have the agent call binaries by absolute path (`/mnt/c/Windows/System32/cmd.exe ...`).

---

## Backup

The entire mesh state lives in `${MESH_HOME}` (default `~/mesh`). A snapshot:

```bash
cp -a ~/mesh ~/mesh-backup-$(date +%Y%m%d-%H%M%S)
```

What is worth backing up:

| Path | Why |
|---|---|
| `~/mesh/api-tokens.json` | Tokens are not regenerated if the file exists; losing it invalidates all clients |
| `~/mesh/inbox-*.jsonl` | Message history |
| `~/mesh/tickets/` | All ticket state |
| `bridges/matrix/config.json` + `agent_tokens.json` | Matrix bridge homeserver + per-agent tokens (if the bridge is deployed) |
| `mesh.toml` | Your deployment config |

Agent working directories (chat history, code, CLAUDE.md) are separate and should be backed up independently if needed.

---

## Re-running bootstrap (update / add agents)

Bootstrap is idempotent:
- `api-tokens.json` is **not** regenerated if it already exists.
- `CLAUDE.md` in each workdir is **not** overwritten if it already exists (manual customization is preserved).
- Systemd units are re-rendered and `daemon-reload`ed; services are restarted.

To add a new agent, add its `[[agents]]` block to `mesh.toml` and run:

```bash
bash bootstrap.sh mesh.toml
```

---

## Rolling restart

Restart services one at a time to minimize downtime:

```bash
for svc in mesh-watcher ticket-dispatcher mesh-api; do
    systemctl --user restart "$svc"
    sleep 2
    systemctl --user is-active "$svc" || echo "FAILED: $svc"
done
```

For agent sessions, restart them one at a time via their systemd units (the Claude Code CLI restarts automatically inside the `while true` loop, so in practice agent sessions rarely need manual intervention):

```bash
systemctl --user restart agent-1.service
```

---

## CLI tools reference

| Tool | Usage | Notes |
|---|---|---|
| `bootstrap.sh` | `bash bootstrap.sh [--dry-run] [--no-systemd] [--no-ssh] [--skip-health] mesh.toml` | Full deploy / update |
| `config_schema/validate.py` | `python3 validate.py mesh.toml [--json] [--summary]` | Validates config; exit 0 valid, 1 validation error, 2 parse error |
| `send.py` | `python3 ~/mesh/send.py <from> <to> <priority> "<body>"` | Send a mesh message |
| `read.py` | `python3 ~/mesh/read.py <agent> [--ack <id>]` | Read inbox (count unread: pipe to `grep -c '^\['`) |
| `ticket-complete.py` | `python3 ~/mesh/ticket-complete.py <ticket-id> --tldr "..."` | Close a running ticket |

---

## Environment variables

All paths have defaults that match a standard single-user install. Override by exporting before running any command, or set in `~/.config/systemd/user/mesh.env` for services.

| Variable | Default | Purpose |
|---|---|---|
**Paths**

| Variable | Default | Purpose |
|---|---|---|
| `MESH_HOME` | `~/mesh` | Bus directory (inboxes, roster, scripts, tokens) |
| `MESH_TICKETS_DIR` | `${MESH_HOME}/tickets` | Ticket state machine root |
| `MESH_TOKENS_PATH` | `${MESH_HOME}/api-tokens.json` | API bearer tokens |
| `MESH_API_BASE` | the checkout's `mesh-api/` | Where the API code lives |
| `MESH_WEBUI_DIR` | the repository's `dashboard-web`, else a `webui` next to the API | Directory served at `/ui` |
| `MESH_AGENT_BASE` | `~` | Parent of agent working directories |
| `MESH_VAULT` | — | Knowledge-base path, for curator-style agents |

**Identity and roster**

| Variable | Default | Purpose |
|---|---|---|
| `MESH_AGENT` | — | Which agent this process is. **Inside a tmux pane the pane's session name wins** — a shared tmux server exports the name of whoever started it into every pane it creates |
| `MESH_HUMAN_FACADES` | from the roster | Extra peers treated as the operator, on top of the roster's own facade peers |
| `MESH_DISPATCH_AGENTS` | the whole roster | Narrow the ticket dispatcher to a subset |
| `MESH_OPERATOR_NODE` | `operator` | Id of the human node in the activity graph |
| `MESH_NODE_COLORS` | `$MESH_HOME/node-colors.json` | Optional `{"<agent>": "#RRGGBB"}` file colouring the graph nodes and the agent cards. Absent = every node falls back to one neutral colour, which is what a fresh install shows |

#### A clone may not type into your agents

`push_local` in the watcher, and `_push_local` in the ticket dispatcher, end with
`send-keys Enter`: they do not hand text to a pane, they **run** it. The only thing
keeping a checkout of this repository from doing that to your live sessions is which
tmux server it aims at — and in a real deployment the sessions are *named after the
agents*, so a bench whose roster happens to hold one of those names reaches the agent
itself.

The rule these components apply, each in its own copy of the same predicate:

- a **private** tmux server (`tmux -L NAME`, `tmux -S PATH`) is accepted for the
  **local** push — it holds nothing but what this deployment put there. It buys
  nothing over SSH: the remote command names no socket, so it always lands on the
  remote host's shared server, and the push there is refused unless one of the two
  clauses below holds;
- the **shared** server is accepted when the mesh home is the default `~/mesh`, i.e.
  when this *is* the installation;
- otherwise it is refused, unless `MESH_ALLOW_SHARED_TMUX=1` says out loud that the
  installation lives somewhere else. `bootstrap.sh` writes that line into the units it
  installs; a clone run by hand carries nothing.

Until 2026-09-09 the predicate accepted *any* declared server — and `MESH_TMUX=tmux`
is a declaration naming the shared one, so a mesh home under `/tmp` was cleared to
type into a live agent's pane. A refusal names both paths and both ways out; a
deployment that hits one is a deployment whose units were not written by
`bootstrap.sh`.

**Behaviour**

| Variable | Default | Purpose |
|---|---|---|
| `MESH_SCOPE_ENFORCE` | unset (log only) | `1` makes the PreToolUse hook actually deny |
| `MESH_TZ` | system local time | IANA timezone for message timestamps |
| `MESH_NO_REPLY` | unset | `1` marks a message one-way (no ack expected) |
| `MESH_TMUX` | `tmux` | tmux invocation, e.g. `tmux -L <socket>` |
| `MESH_ALLOW_SHARED_TMUX` | unset | `1` lets a mesh whose home is **not** `~/mesh` drive the shared tmux server. The units `bootstrap.sh` installs set it; a clone run by hand does not, and is refused — see below |
| `MESH_BUSY_PATTERN` / `MESH_IDLE_PATTERN` | see `bus/watcher.sh` | Idle heuristic, if your agent CLI shows something else |
| `MESH_SSH_OPTS` | batch mode, 10s timeout | Extra ssh options for pushes to remote agents |
| `MESH_CHECK_WAIT` | `5` | Seconds `mesh-send-checked.sh` waits before reporting |
| `MESH_BRIDGE_PEERS` / `MESH_BRIDGE_STATE` | — | Peers delivered by the chat bridge rather than the watcher, and its state file |
| `MESH_WATCHED_SERVICES` | — | Extra systemd units to report in `/status` |
| `MESH_WATCHED_HTTP` | — | Extra HTTP services to report, as `name=url` pairs |
| `MESH_REMOTE_USER` / `MESH_REMOTE_HOST` | — | Fallback SSH target for remote agents (per-agent entries in `hosts.json` win) |

**Safety**

| Variable | Default | Purpose |
|---|---|---|
| `MESH_API_NO_AUTH` | unset | `1` opens **reads** without a token. Writes always need one |
| `MESH_API_BIND` | set by the systemd unit | Tells the API where it listens, so it can refuse to start with auth off on a reachable address |
| `MESH_API_ALLOW_OPEN_BIND` | unset | `1` overrides that refusal — you are saying a firewall restricts the port and you meant it |
