# Design principles

The choices that shaped the mesh. Each one has a tradeoff in the *other* direction that was considered and rejected — that's what makes it a principle.

## 1. Files, not services

> "What does this state look like on disk?" is the first question for every new feature.

If you can't answer it, you don't add the feature.

This rules out:
- Caching layers (Redis, Memcached)
- Message brokers (RabbitMQ, NATS, Kafka)
- Relational databases (PostgreSQL, MySQL)
- KV stores (etcd, Consul)

It rules in:
- JSONL append-only logs
- JSON files for state
- Directory structure encoding state machines

**Why**: every debugging session is faster. Every backup is `cp -a`. Every replay is appending to a file. The cognitive load of "where does X live?" is zero — it's a file path.

**Tradeoff**: this caps the throughput. A real Redis pubsub handles 100k messages/sec; the bus handles maybe 50/sec before flock contention is felt. That's fine — humans don't send 100k messages/sec, and agents talking to agents at human speeds rarely exceed 1/sec.

## 2. Push, not poll — but degrade gracefully to poll

The watcher uses `inotify` for sub-second delivery. But every consumer (the `read.py` CLI, the `/inbox` API endpoint) **also** does the right thing if it's invoked cold without any push having happened — it scans the file from the last cursor.

**Why**: push is fast when it works. Poll is what saves you when it doesn't.

If the watcher service is down, messages still land in inboxes; agents will see them at their next `SessionStart` (a hook reads the inbox eagerly on every session start). If the API's WS dies, the webui refetches `/status` on reconnect and the Matrix bridge keeps its own `/sync` loop — neither depends on the live stream to stay correct. Everything has two paths.

**Tradeoff**: each consumer duplicates the read logic. Fix: share a `read.py` library between CLI, hook, and API.

## 3. State on disk, behavior in code

The bus directory has every piece of state. The scripts in the bus directory have all the behavior. **No state in code** (no hardcoded message lists, no in-memory queues that the script doesn't persist).

This means:
- You can `kill -9` the watcher and `systemctl --user restart mesh-watcher` — it picks up exactly where it was.
- You can `pkill -9 claude` on any agent — its messages are still in its inbox, its open tickets are still on disk.
- You can `cp -a ~/mesh ~/mesh-backup` for a true snapshot.

**Why**: the worst category of bug is "the system disagrees with itself". When state is on disk and behavior is in code, the disk is always right, and you can reload behavior with a restart.

**Tradeoff**: more disk I/O. Mitigated because individual operations are tiny (KB-scale JSON files).

## 4. Per-agent isolation by directory and hook

Each agent has:
- Its own working directory (`~/<agent>/`)
- Its own `CLAUDE.md` declaring its charter
- Its own `tmux` session
- The same `~/.claude/settings.json` (one user) but the `PreToolUse` hook reads the agent identity from `tmux_session` or working directory

There is **no separate user account per agent**. They all run as the same OS user. Isolation is convention-based, not OS-enforced.

**Why**: simpler operations. One user, one home directory, one Tailscale identity. The cost of full OS-level isolation (separate UIDs, polkit, AppArmor profiles) is not worth it for a personal mesh.

**Tradeoff**: an agent compromised at the CLI level can in theory touch anything the user can. Mitigation:
- `PreToolUse` hook detects out-of-scope writes (audit trail in classifier.log)
- Each agent's charter is explicit about what it owns vs. what it must not touch
- The supervisor agent reviews the classifier log

For a personal mesh with you as sole operator, this is acceptable. For a team, you'd want OS-level isolation per agent.

## 5. Charter-first, code-second

Each agent's behavior is shaped 80% by its `CLAUDE.md` (natural-language charter) and 20% by code (hooks, scripts, the tools available).

When the user wants an agent to behave differently, the *first* thing to change is the charter. The second thing to change is the code.

**Why**: charters are easy to iterate, version-control, share, copy. Code changes that override charter intent are a smell.

**Example**: "this agent must reject mail requests on weekends" — first try is to add a sentence to the charter and let the agent self-enforce. If that's unreliable, only then add a guard in code (e.g., the API rejects POST /send between 23h–7h for that agent's identity).

## 6. ACK conversational protocol

When agent A sends agent B work, B must acknowledge in writing before starting:

```
"ACK <message-id>, starting on Y now"
```

This is **not** a protocol-level feature. It's a charter-level expectation: every agent's `CLAUDE.md` says "on receipt of non-trivial work, send a short ACK first". The watcher detects stale messages (>30s without ACK) and alerts the sender.

**Why**: visibility. Without the ACK, you don't know if the agent is working, blocked, asleep, or crashed. With it, you have a heartbeat per task.

**Tradeoff**: every task has an extra round-trip. For 10-second tasks, the ACK is 5% overhead. For 10-minute tasks, it's negligible. For batched tasks (bulk operations), the ACK is per-batch, not per-item.

## 7. Tickets vs. messages

These are **two distinct primitives**, not one with an extra field:

| Property | Message | Ticket |
|---|---|---|
| Lifecycle | one-shot append to inbox | state machine (draft → done) |
| Persistence | line in JSONL | separate JSON file per directory |
| Reply expected | conversational (ACK) | structured (TL;DR via ticket-complete.py) |
| User-visible | scrollback chatter | actionable units in a UI |
| Dependencies | reply_to (informational) | depends_on (enforced by dispatcher) |
| Default lifetime | append-only history | moved between directories |

**Why**: the human pilot needs a different mental model for "I had a quick chat with this agent" (message) vs. "I asked this agent to do something concrete and will check back on it later" (ticket). The webui surfaces them as distinct UI elements, and on Matrix they read naturally as chat vs. a tracked task.

**Tradeoff**: more code paths. Worth it for clarity.

## 8. The pilot is a first-class peer, not a viewer

The human pilot has its own bus identity — `inbox-pilot-matrix.jsonl` for the Matrix bridge, plus a `user-web` sender for the webui's POSTs. Agents send TL;DRs to the pilot like they send messages to other agents — same API, same bus, same delivery mechanics (the bridge tails the pilot's inbox into Matrix instead of `tmux send-keys`).

**Why**: writing the pilot's experience as just-another-peer avoids a thousand special cases. The pilot peer has different infrastructure (a Matrix homeserver + bridge, a browser holding a token), but the *contract* with the bus is identical — an inbox file and the `send.py` whitelist.

**Tradeoff**: minor — the pilot identities had to be added to the peer whitelist as exceptions (they're not agent processes, just destinations/senders). Acceptable.

## 9. Tailscale-only, never internet-exposed

The API binds to loopback by default. In the original deployment it is opened on the VPN interface and nowhere else — no public port, no proxy, no port forward — but that is enforced by the host's firewall, not by this code. Binding wider without that rule exposes bearer-token auth to the whole network.

**Why**:
- Cloudflare-as-TLS-terminator means Cloudflare can see your traffic. Hard pass.
- Public ports require constant CVE vigilance. Tailscale (WireGuard mutual auth) shifts the trust boundary to "anyone on my tailnet" — a small, known set.

**Tradeoff**: collaborators need to be on the tailnet. Not a problem for a personal mesh.

## 10. Honest about identity drift

When you spread tokens, hostnames, file paths across many files, you eventually want a single source of truth. The system has `peers.py` and `peers.sh` for "who are the agents", and a shared `api-tokens.json` for "who can hit the API". Wherever a list of agents existed in code before, it's now imported from these.

The honest part: this isn't done for every constant. Tailscale IPs are still in 3 places. File paths are still hardcoded. The principle is "consolidate when the second copy appears", not "build a config system before you have one".

**Why**: premature configurability is a real cost. The bus shipped fast because the first phases used hardcoded paths. Then a duplicate was added, then a third, then it hurt — and *then* `peers.py` was extracted. Same for everything else.

**Tradeoff**: portability suffers (cloning to another machine requires search-and-replace). Documented as a known limitation. Acceptable for a personal mesh.

## 11. Auto-recovery before auto-everything

The supervisor can restart services and agents. It can compact contexts. It can dismiss survey popups. It can mark stuck tickets as failed.

It does **not** automatically:
- Code its own fixes
- Modify other agents' charters
- Push to public repositories
- Send mails to non-test addresses
- Make purchases

**Why**: every auto-action that's hard to reverse is a foot-gun. The supervisor's superpowers stay in the "operations" lane: restarts, cleanups, alerts. The "decisions" lane stays with the human or the agents themselves.

**Tradeoff**: the supervisor is useful but limited. That's fine. A more aggressive supervisor would require human review of its action log, defeating its purpose.

## 12. Backwards compatibility is not a goal

When a refactor is right, the system rolls forward without compatibility shims. There is no V1 / V2 fork. Old code is deleted, not re-exported. Old data formats are migrated in place by the script that first reads them.

**Why**: the system has one operator. Versioning would be cosplay. Honest refactoring is faster than fake compatibility.

**Tradeoff**: if you fork this for a multi-user mesh, you'd need to reintroduce versioning. Worth it then; not worth it now.

## 13. Small, share-able iteration

Every change should be visible to other agents within minutes. The bus is the audit trail of work. When agent A delivers a feature, it sends a delivery message ("v1.2 shipped, commit abc1234, artifact at /path/to/build/"). Agent B sees this in its inbox and knows: the feature is available, here's where the artifacts are, here's what to test.

**Why**: this is what makes multi-agent work *feel* collaborative instead of "isolated workers behind a coordinator". The bus is the shared mind.

**Tradeoff**: agents that don't broadcast their work feel invisible. Charters explicitly require broadcasting deliveries.

## 14. Recover before you replicate

Before you spin up agent N+1, make sure agent N's failure modes are handled:
- `Stop` hook clears running tickets when a session ends
- `SessionStart` hook hydrates new sessions with pending inbox
- `cron` heartbeat detects agents whose tmux sessions died
- supervisor restarts agents in `inactive` state

**Why**: adding agents amplifies whatever failure modes already exist. Get the basics right, then scale.

**Tradeoff**: initial phase 0 is slower (basics first). Pays back from phase 3 onward.

---

These aren't laws. They're the regret-tested choices of a system that's been refactored maybe 6 times. Each one has a corresponding pull request from earlier in the project's history where the *opposite* choice was tried and then reversed. If you fork the pattern, expect to revisit them — but expect to converge on similar tradeoffs.
