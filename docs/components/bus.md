# The message bus

The core primitive of the mesh: a filesystem-backed message log with sub-second push delivery.

## Anatomy

The bus is **one directory** with **a handful of scripts** acting on it:

```
$MESH_HOME/
├── inbox-<agent>.jsonl    # one per recipient
├── state-<agent>.json     # one per recipient (read cursors)
├── peers.py, peers.sh     # the roster, written by bootstrap.sh
├── roster.py              # reads whichever roster file exists
├── send.py                # write a message
├── read.py                # read your own inbox
├── mesh-send.sh           # ergonomic wrapper around send.py
├── mesh-send-checked.sh   # send, then report whether it was delivered
├── ticket-complete.py     # close a ticket you finished
├── watcher.sh             # inotify loop, pushes to recipients
├── hosts.json             # optional: per-agent SSH transport
├── fleet-policy.json      # optional: who may currently receive work
└── logs/watcher.log
```

`bootstrap.sh` copies those scripts there from `bus/` in the checkout. That is
deliberate: every consumer looks for them under `$MESH_HOME` (the systemd units
start `$MESH_HOME/watcher.sh`, the API shells out to
`$MESH_HOME/mesh-send-checked.sh`), so a mesh keeps working when the checkout
moves, and an upgrade is a re-run of the installer rather than a side effect of a
`git checkout`.

That's it. No daemons, no broker, no database connection. The bus *is* the
directory. If you also drop a `render.py` in there, `send.py` and `read.py` will
run it after each write to regenerate a human-readable view — an optional hook,
not something this repository ships.

## Identities

The "peers" of the mesh are listed in `peers.py`:

```python
# peers.py
AGENTS = {"agent-1", "agent-2", "agent-3", "supervisor", "agent-4", "agent-5"}
PILOT_PEERS = {"user-web", "pilot-matrix"}   # the human pilot's passive peers
ALL_PEERS = AGENTS | PILOT_PEERS
```

`user-web` is the webui's sender identity; `pilot-matrix` is the Matrix bridge's peer (it has its own `inbox-pilot-matrix.jsonl`). Neither runs an agent — they're just valid `from`/`to` endpoints.

`bootstrap.sh` writes an equivalent `peers.sh` for shell scripts that want the
same list.

Nothing reads those files directly. `bus/roster.py` does, and everything else
imports *it* — the bus scripts, the API, the ticket dispatcher. That indirection
is not decoration: the API used to read only a richer `mesh_roster.py` (the shape
a grown deployment ends up with) while the installer wrote `peers.py`, so a
freshly installed mesh had an API that knew no agents at all, answered 422 to
every send, and pointed at a generator that does not exist. One reader, three
accepted shapes:

| File | Written by | Exposes |
|---|---|---|
| `mesh_roster.py` | a deployment's own generator | `INBOX_PEERS`, `FACADE_PEERS` |
| `peers.py` | `bootstrap.sh` | `AGENTS`, `PILOT_PEERS` |
| *(neither)* | — | an empty roster: every peer is unknown, and the bus says so |

The empty case fails closed on purpose. Writing to an inbox is close to running
code as its owner, so an unknown sender is refused rather than accepted under a
default name.

**Do not** hardcode the list anywhere else — it duplicates and drifts within a
week.

## Message format

A message is one JSON object per line, appended to `inbox-<to>.jsonl`:

```json
{"id":"a1b2c3d4","ts":"2026-05-24T14:30:00+02:00","from":"agent-1","to":"agent-2","priority":"normal","body":"hello","acked":false}
```

Fields:
- `id` — 8 hex chars, `sha1(from|ts|body[:200]|nonce)[:8]`. The seconds and the
  random nonce are both load-bearing: without them two identical messages inside
  the same minute collided, and an ack then marked the wrong one handled.
- `ts` — ISO 8601 with offset. The local timezone by default; set `MESH_TZ` to an
  IANA name to pin it. Never a hardcoded offset — DST will bite.
- `from`, `to` — must be known to the roster, or match `topic-<slug>` (chat
  rooms created on the fly, which cannot be in a roster generated at deploy
  time; the namespace is all the authority they get).
- `priority` — `urgent`, `high`, `normal`, `low`. The bus treats them the same;
  they are a signal to the reader and to whatever renders the inbox.
- `body` — UTF-8 text, **at most 64 KiB**, and control bytes are **refused, not
  stripped** (tab and newline excepted). The body is printed to terminals and
  typed into panes: an escape sequence there rewrites what a human sees, and a
  stripped body would deliver something the sender did not write. Past 64 KiB it
  is not a message, it is a file — send a path.
- `reply_to` — optional, the id this message answers (`send.py --reply-to <id>`).
  Absent when it starts a thread.
- `acked` — `false` on write, flipped to `true` by `read.py --ack <id>`.
- `no_reply` — present and `true` when the sender set `MESH_NO_REPLY=1`: a
  one-way notification the recipient acts on but never answers, so no-ack
  tracking must not nag about it.

## Writing a message: `bus/send.py`

```sh
python3 send.py <from> <to> <priority> [--reply-to <id>] <body...>
```

Read the file rather than an excerpt of it — it is 200 lines and every branch
carries the reason it exists. What matters here is the order of the checks and
one detail of the write.

**Everything is validated before anything is written**, and each refusal exits
non-zero with nothing appended: unknown sender or recipient, sender equal to
recipient, unknown priority, empty body, body over 64 KiB, control bytes,
malformed `--reply-to`. A recipient the fleet policy has taken out of play is
refused here too, at send time — delivering it silently would leave the sender
believing it had dispatched work, waiting for an answer that cannot come, and on
a mesh where arrival wakes a sleeping agent it would wake the very agent that was
put to rest.

**The append holds an exclusive `flock`, and flushes and fsyncs *inside* it.**
That last part is not pedantry. `with` flushes on block exit — after the unlock —
so a deferred write could land inside a concurrent ack rewrite, between its
truncate and its write, and be clobbered: a message delivered, then lost. The
same rule applies to every writer of these files.

Failure direction is chosen, not accidental. A missing *or corrupt*
`fleet-policy.json` applies **no** restriction, and says so on stderr: a policy
that failed closed would cut the bus on a JSON typo and strand agents mid-task,
while the worst case in this direction is a message reaching a paused agent —
visible and reversible.

## Reading: `bus/read.py`

```sh
python3 read.py <agent>              # unread: past your cursor, not acked
python3 read.py <agent> --all        # everything in the file
python3 read.py <agent> --unacked    # everything unacked, cursor ignored
python3 read.py <agent> --ack <id>   # mark handled
```

Three views, because *have I seen this* and *have I dealt with this* are
different questions.

- **unread** is cursor-based, and it is what a session opens with.
- **`--unacked` ignores the cursor entirely.** It exists for agents that sleep
  and are woken by a message: the wake path advances the cursor at boot, so the
  cursor view goes empty while the work is still owed.
- **`--ack` is the only thing that marks a message handled.** Displaying one
  deliberately does not, so an interrupted session keeps its queue.

The cursor stores a timestamp *and* the ids seen at that exact timestamp. A plain
`ts >` comparison hid same-second siblings of the cursor message — newer than
nothing, older than nothing — so they never appeared in the normal view and only
`--unacked` revealed them.

`--ack` rewrites the whole inbox, and holds **one** lock across read → mutate →
rewrite. Two separate locks leave a window where a delivery lands in a snapshot
that is then truncated away.

A message from a peer the roster marks as a human facade prints a reminder to
answer *through the bus*: that person is reading a chat client, not this
terminal, and an agent that answers locally looks mute to whoever is waiting.

## The watcher: live push

`bus/watcher.sh` is one `inotifywait` loop over `$MESH_HOME`. When an inbox
grows, it types a short notice into the recipient's session — locally through
tmux, or over SSH for an agent on another machine, according to `hosts.json`:

```json
{ "carol": { "ssh": "user@second-host", "session": "carol" } }
```

An agent absent from that file is local, in a tmux session named after it.

Two rules shape the whole script.

**Never type into a working session.** Injecting text mid-task interrupts it, and
can land inside a prompt the agent was composing. A push happens only when the
pane looks idle; otherwise the message stays where it is and is picked up on the
next read. A skipped push is a delay, never a loss — which is why this process is
allowed to be simple, and to crash.

**What is typed is data, not commands.** The notice says *that* a message
arrived and from whom, and the agent reads it itself. The body never reaches a
shell. The recipient name comes from a *file name* and the id from a file another
agent wrote, so both are validated (`^[a-z][a-z0-9-]{0,31}$`, `^[0-9a-f]{4,32}$`)
before they are typed; `send-keys -l` sends text literally, with `Enter` as a
separate key, so a body can never submit itself.

Idle detection is a heuristic, and it is biased towards *busy*: a false "busy"
costs a delay, a false "idle" interrupts work in flight. Busy is judged first, on
the strongest signal available — an elapsed timer (`(12s`, `1m 4s`) or an
interrupt hint means output is being produced right now. Idle is then judged on
the **last non-empty line**, which is where a prompt lives. Both patterns are
overridable (`MESH_BUSY_PATTERN`, `MESH_IDLE_PATTERN`): they are the one part of
the script that depends on which CLI the agent runs.

Two bugs found by running it rather than reading it, both of which made delivery
die in silence while the log claimed the session was busy:

1. `capture-pane -p | tail -6` returns blank lines. A pane is as tall as the
   terminal, so an idle session has its prompt near the top and dozens of empty
   lines under it. Filter *then* tail: `capture-pane -p | grep . | tail -6`.
2. `"${VAR:-[$#>]}"` expands `$#` — inside double quotes the shell expands the
   default too, and the idle pattern stopped matching any prompt.

Every decision is logged, `skip push` included: that line is what tells you why a
message did not reach someone. `bus/mesh-send-checked.sh` greps for it, so a
sender can know within seconds whether a message was delivered live, deferred, or
whether the watcher is down.

The same append arrives twice (inotify reports create *and* modify), so the last
id pushed per recipient is remembered and a repeat is dropped — otherwise the
notice is typed twice.

Supervise it with `systemd --user`, `Restart=always`. A crash leaves the bus
fully operational: messages still land in inboxes and agents read them at their
next session start. Only live push is lost.

## Failure modes and recovery

### Watcher crashes

- Symptom: messages land in inboxes but no push happens. Agents see them late (next `SessionStart`).
- Detection: `systemctl --user status mesh-watcher` shows failed, or `~/mesh/logs/watcher.log` has no recent lines.
- Recovery: `systemctl --user restart mesh-watcher`. Inboxes are intact, no data loss.

### A new agent's inbox is not watched

Not a failure mode here, and it is worth knowing why: an earlier version watched
`inbox-*.jsonl`, a glob expanded once at startup, so an inbox created afterwards
was invisible until the watcher was restarted — an agent added at 3pm silently
received nothing until someone noticed. The shipped watcher watches the
**directory** and filters events by name, so a new inbox is picked up the moment
it appears.

### A corrupt line in an inbox

- Symptom: a line that is not valid JSON — a writer that did not take the lock,
  a disk that filled mid-append.
- Behaviour: readers skip it and say so on stderr. One bad record must never
  blind an agent to the rest of its inbox, which is why nothing here treats a
  parse error as fatal.
- Recovery: `jq -c . < inbox-<agent>.jsonl` to find it, drop the line, save back.

### Recipient's tmux session was killed

- Symptom: `tmux send-keys -t <agent>` returns exit 1.
- Detection: the watcher logs `event push local FAILED`.
- Recovery: `systemctl --user restart <agent>.service` (the agent's session is restarted; `SessionStart` hook hydrates the inbox).

## Performance characteristics

Measured on the deployment this pattern grew in — a Raspberry Pi 4, 8 agents,
~50,000 cumulative messages on disk. Orders of magnitude, not guarantees:

- **`send.py` latency**: 8–15 ms (dominated by Python startup + flock)
- **End-to-end inbox-update → recipient sees prompt**: 200–800 ms local, 1–2 s remote (SSH dominates)
- **Watcher CPU**: <1 % at rest, ~3 % during a burst of 10 messages/s
- **Inbox growth**: ~150 KB / 100 messages (acceptable for years of operation; rotate at 100 MB if you care)

`/conversation` API endpoint times scale with inbox size — at 50 MB cumulative across inboxes, a 100-message `/conversation/<agent>` query takes ~150 ms. Add a TTL cache if you serve a busy UI.

## Migration and replay

Want to copy the bus to a new machine? It's just files:

```bash
rsync -a --info=progress2 ~/mesh/ newhost:~/mesh/
ssh newhost 'systemctl --user enable --now mesh-watcher mesh-api ticket-dispatcher'
```

Want to replay yesterday's messages against a fresh agent? Append them to its inbox:

```bash
# replay everything sent to agent-2 after 2026-05-22:
jq -c 'select(.ts > "2026-05-22")' ~/mesh-backup/inbox-agent-2.jsonl \
    >> ~/mesh/inbox-agent-2.jsonl
```

The watcher picks up the new lines and pushes them.

The bus's biggest feature is also its biggest limitation: **it is its data**. Lose `~/mesh/`, you've lost the state of the mesh. Back it up.
