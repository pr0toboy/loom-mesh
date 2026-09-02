# Scope enforcement

Each agent in the mesh has a defined **scope**: what it's allowed to read, write, and run. Scope is enforced in two layers — a soft layer (natural language) and an auditable layer (hook inspection). Neither is a hard security boundary; both together are enough for a personal mesh where the owner trusts every agent's model.

## Why scope at all

Without scope, agents accumulate permissions by convenience: the first time an agent needs to touch a file outside its zone, it does, no one objects, and soon every agent can reach every file. This creates three real problems:

1. **Debugging becomes hard.** When a file changes unexpectedly, "which agent touched it?" requires reading the full transcript of every session.
2. **Mistakes cascade.** An agent that overwrites a config it shouldn't touch can break another agent's running session.
3. **No audit trail.** Without scope signals, you can't distinguish "agent acted within its charter" from "agent drifted."

The mesh's answer is to make scope explicit in the charter and automatically logged at the tool level.

## Layer 1 — Working directory and charter

Each agent's tmux session starts in its **working directory** (e.g., `~/curator`, `~/builder`). The directory contains a `CLAUDE.md` file — Claude Code's project-level instruction file — that declares the agent's charter in natural language:

```markdown
## Scope
- Read and write freely within `~/builder`.
- Do not modify other agents' inboxes or state files directly.
- Do not touch the knowledge vault unless explicitly tasked.
```

This is **soft scope**: the model reads the charter at the start of every session and incorporates it into its behavior. A well-written charter covers:

- **Allowed zones** — directories the agent owns and can write freely.
- **Forbidden zones** — directories it must not touch (other agents' working dirs, bus state files, production configs).
- **Escalation** — what to do when a task requires crossing a boundary ("send a message to the curator agent via mesh, do not access the vault directly").

Charter-first (see [principles.md](../principles.md#5-charter-first-code-second)): when you want an agent to behave differently, edit the charter. Only add hook-level enforcement when charter-based self-enforcement proves unreliable.

The charter templates in `templates/charter/` differentiate by role:

| Template | Role | Key scope additions |
|---|---|---|
| `worker` | Coding agent | Free in workdir; no vault, no cross-agent state |
| `curator` | Knowledge-base agent | Free in workdir; no builds or deploys; vault discipline |
| `supervisor` | Health + monitoring | Read-only across agents; writes only to mesh state files |

Render your charter by adapting the template:

```bash
# bootstrap.sh does this automatically for you
sed -e "s/{{agent.name}}/my-agent/g" \
    -e "s|{{agent.workdir}}|~/my-agent|g" \
    templates/charter/worker.md > ~/my-agent/CLAUDE.md
```

## Layer 2 — PreToolUse hook (auditable enforcement)

Claude Code executes a `PreToolUse` hook before every tool call. The hook receives the tool name and arguments and can either allow or deny the call.

The mesh wires this hook in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|Read",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/mesh/scope-check.py"
          }
        ]
      }
    ]
  }
}
```

`scope-check.py` receives the hook context on stdin (JSON with `tool_name`, `tool_input`, `session_id`, `cwd`):

```json
{
  "tool_name": "Write",
  "tool_input": {"file_path": "/home/user/other-agent/config.py"},
  "cwd": "/home/user/builder",
  "session_id": "abc123"
}
```

The script:

1. Derives the calling agent's identity from `cwd` (the working directory uniquely identifies each agent).
2. Loads that agent's scope profile — a list of allowed path prefixes and denied path prefixes.
3. Checks whether `file_path` (or the shell command for Bash tools) targets an allowed path.
4. Emits a decision.

### LOG mode vs ENFORCE mode

The hook runs in one of two modes, configured per agent in its scope profile:

**LOG mode** (default for new agents):

```python
if decision == DENY:
    log(f"WOULD_DENY agent={agent} tool={tool} path={path}")
    sys.exit(0)   # allow anyway — scope drift is logged, not blocked
```

Every out-of-scope access is written to `~/mesh/logs/classifier.log` with a `WOULD_DENY` marker. The supervisor reviews this log periodically to identify drift patterns before moving the agent to ENFORCE mode.

**ENFORCE mode** (after the profile is validated):

```python
if decision == DENY:
    log(f"DENIED agent={agent} tool={tool} path={path}")
    print(json.dumps({"decision": "block", "reason": f"Out-of-scope: {path} not in allowed zones"}))
    sys.exit(0)   # exit 0 required — non-zero would crash Claude Code
```

When denied, the hook outputs a JSON block to stdout that Claude Code surfaces as an error. The agent sees "PreToolUse hook denied this call: Out-of-scope: ..." and can decide to escalate via mesh rather than trying to force the access.

Start in LOG mode, validate the profile over a week of normal operation, then switch to ENFORCE.

### Scope profile format

Each agent has a profile file at `~/mesh/scope-profiles/<agent>.json`:

```json
{
  "agent": "builder",
  "mode": "log",
  "allowed_paths": [
    "~/builder",
    "~/mesh/inbox-builder.jsonl",
    "~/mesh/state-builder.json",
    "~/mesh/tickets/builder"
  ],
  "denied_paths": [
    "~/curator",
    "~/supervisor",
    "~/.claude/settings.json",
    "~/mesh/api-tokens.json"
  ]
}
```

Paths are prefix-matched after `os.path.expanduser()` normalization. A write to `~/builder/src/foo.py` matches the `~/builder` prefix → allowed. A write to `~/curator/notes.md` doesn't match any allowed prefix → WOULD_DENY (or DENY in enforce mode).

## Audit trail: classifier.log

`~/mesh/logs/classifier.log` records every hook decision in structured text:

```
2026-05-25T01:45:00+02:00 ALLOW  agent=builder tool=Edit   path=~/builder/app.py
2026-05-25T01:45:12+02:00 WOULD_DENY agent=builder tool=Write path=~/curator/index.md
2026-05-25T01:45:30+02:00 ALLOW  agent=builder tool=Bash   cmd=git status
```

The supervisor scans this log for `WOULD_DENY` entries and groups them by agent and target path. Two useful queries:

```bash
# All out-of-scope accesses in the last hour
grep WOULD_DENY ~/mesh/logs/classifier.log | awk -F' ' '$1 > "2026-05-25T00:45"'

# Most-accessed out-of-scope targets by agent
grep WOULD_DENY ~/mesh/logs/classifier.log | awk '{print $4, $6}' | sort | uniq -c | sort -rn
```

A pattern like "builder WOULD_DENY ~/curator 12 times in 10 minutes" usually means the charter boundary is wrong — the task genuinely requires cross-agent access, which should be achieved via mesh ticket (delegate to the curator) rather than by widening the builder's allowed paths.

## Limitations

**This is not OS-level isolation.** All agents run as the same OS user. A compromised agent (e.g., via prompt injection through a message body) can:

- Write to any file the user owns — the hook is a voluntary gate, not a kernel enforcement.
- Read any file the user owns, including other agents' inboxes and `api-tokens.json`.
- Kill other agents' tmux sessions.

Mitigations already in place:
- Tailscale-only API access limits who can inject malicious messages.
- Bearer token auth on the API limits which devices can push to agent inboxes.
- The bus body is validated for size and null bytes but not sanitized for prompt injection — a known gap (see README Known Issues).

**For a personal mesh**, this level of isolation is acceptable. The threat model is "model makes a mistake," not "attacker gains shell access." The hook provides an audit trail that makes mistakes visible, and ENFORCE mode stops them.

**For a team mesh**, you'd want:

- Separate OS users per agent (so kernel-enforced isolation applies).
- AppArmor or seccomp profiles per agent process.
- Per-token API scopes (token X can only write as agent Y).
- Stricter prompt-injection mitigations on message bodies.

These are out of scope for this repo, which is explicitly a personal-mesh pattern.

## Scope drift patterns to watch for

Based on operational experience, common drift patterns and their fixes:

| Pattern | Symptom in classifier.log | Root cause | Fix |
|---|---|---|---|
| Cross-vault write | builder WOULD_DENY ~/curator | Task requires KB update | Mesh ticket to curator instead |
| Mesh state write | worker WOULD_DENY ~/mesh/api-tokens.json | Debugging gone wrong | Clarify charter: "mesh state files are read-only for workers" |
| Other agent's workdir | agent-A WOULD_DENY ~/agent-B | Wrong workdir in prompt | Fix the prompt/brief that misidentified the target |
| Shared config write | builder WOULD_DENY ~/.claude/settings.json | Over-eager hook reconfiguration | Never let workers touch settings — add to denied_paths |

When a new `WOULD_DENY` pattern appears more than twice in the same session, treat it as signal: either the charter boundary is drawn wrong, or the agent is off-script.
