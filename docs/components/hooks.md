# Claude Code hooks

Three [Claude Code hooks](https://docs.claude.com/en/docs/claude-code/hooks) fold
the bus into each agent's session lifecycle. They are what makes a fresh `claude`
invocation feel like the agent never left, and what keeps an agent inside its own
scope.

All three are small, single-purpose Python scripts that fail open: if anything
about them breaks — bad payload, missing config, a bug of their own — the agent
keeps working. A hook that blocks work when it malfunctions gets removed by
whoever is trying to get something done, and then protects nothing. What they
must never do is fail *silently*, so each one writes what it decided.

| Event | Script | Job |
|---|---|---|
| `SessionStart` | `hooks/session_start.py` | hand the agent its unread messages as it opens |
| `PreToolUse` | `hooks/scope_check.py` | refuse tool calls that leave the agent's scope |
| `Stop` | `autonomy/hooks/work_drain_stop.py` | during an autonomy run, decline to go idle while work is open |

## Why hooks rather than a launcher

An earlier prototype wrapped the `claude` binary in a script that piped a digest
of unread messages into a prompt file. It worked, and it cost two things: every
way of starting a session (CLI, IDE, a terminal opened by the IDE) needed the
wrapper, and missing one produced a session that silently knew nothing about the
mesh; and the wrapper ran *before* Claude Code set up its own context, so it
could not use any of it.

Hooks fire from inside the session, for every entry path. That is the right
surface.

## Wiring

Hooks are declared in `~/.claude/settings.json`. `bootstrap.sh` does not write
this file — it belongs to your Claude Code installation, and rewriting it under
an operator is not the installer's business. Add the entries yourself, with the
absolute path of your checkout:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python3 /path/to/loom-mesh/hooks/session_start.py" } ] }
    ],
    "PreToolUse": [
      { "matcher": ".*",
        "hooks": [ { "type": "command", "command": "python3 /path/to/loom-mesh/hooks/scope_check.py" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "python3 /path/to/loom-mesh/autonomy/hooks/work_drain_stop.py" } ] }
    ]
  }
}
```

Each hook needs to know which agent it is running for. It reads `MESH_AGENT`,
**except inside a real tmux pane, where the pane's session name wins** — see
below, because that inversion is a safety property and not a detail.

## SessionStart — the unread inbox

`hooks/session_start.py` reads the agent's inbox and returns the messages it has
not dealt with as `additionalContext`. Without it, a message that arrived while
an agent was down waits until somebody thinks to read the file.

Three decisions in it are worth knowing.

**Who am I?** Getting this wrong makes an agent read — and advance the read
cursor of — somebody else's inbox. So identity is not taken on trust. When the
process really is inside a tmux pane (`$TMUX` is set), the pane's session name
wins over `MESH_AGENT`: a shared tmux server started by one agent's service
exports *its* name into every pane it later creates, so the variable can name a
different agent entirely. When there is no pane — the hook invoked over SSH from
another machine — tmux would answer with whatever session lives *there*, so the
environment is trusted instead. A conflict is reported on stderr (never stdout:
stdout is injected into the agent's context).

**What counts as unread?** Not simply "newer than the cursor". A message pushed
live mid-session and acknowledged then is still newer than the cursor, which only
moves here — replaying it makes the agent do the work twice, which on a mesh
means sending the same instruction to somebody else twice. An acknowledged
message is never unread, whatever the cursor says.

**Say something after a reset.** After a `/compact` or `/clear` the harness hands
the agent a fresh turn with no instruction. Staying silent there — the natural
behaviour when the inbox is empty — makes an agent acknowledge and stop, mid
task, looking like it decided to quit. On those two sources the hook always emits
a resume directive.

The cursor advances only for the messages it actually injected, and it records
the ids seen at that exact timestamp: two messages sent in the same second are
otherwise indistinguishable, and the sibling that was not injected would be
skipped forever.

## PreToolUse — scope

`hooks/scope_check.py` sees every tool call and refuses the ones that write
outside the agent's declared scope. Every agent runs as the same OS user, so
nothing underneath draws that boundary.

Scopes live in `$MESH_HOME/scopes.json`, not in the script — they describe one
deployment's agents, and hardcoding them would mean editing shipped code to
install it:

```json
{
  "curator": {
    "blocked_paths": ["^{home}/builder/.*"],
    "blocked_bash_patterns": ["systemctl\\s+.*\\bdatabase\\b"]
  }
}
```

`{home}` and `{mesh_home}` are substituted (regex-escaped) before matching. A
pattern that cannot be expanded — a regex quantifier such as `.{0,3}` makes
`str.format` raise — is kept, but logged as `CONFIG_ERROR`, because a rule that
silently stops matching is worse than no rule.

**It runs in log-only mode until you say otherwise.** With `MESH_SCOPE_ENFORCE`
unset, a violation is recorded as `WOULD_DENY` and the call proceeds. Run it that
way first and read `$MESH_HOME/logs/scope-check.log`: a scope table written from
imagination denies legitimate work on its first day.

What it inspects, and why the list is longer than it looks:

- `Edit`, `Write`, `NotebookEdit` — the target path, read from `file_path` *or*
  `notebook_path` (NotebookEdit uses the latter, and reading only the former let
  every notebook through).
- `Bash` — the write targets it can extract: redirections, `tee`, `dd of=`, and
  the argument paths of commands that create, move, delete or overwrite
  (`cp`, `mv`, `rm`, `mkdir`, `git`, `curl`, `python3 -c`, `cd`, …). Path scope
  enforced only on file tools is decoration the moment a shell is available.
- Command patterns — anything matching `blocked_bash_patterns`.

Paths are compared in every plausible spelling: `~` expanded, `..` collapsed,
symlinks resolved, Unicode normalised, and with a trailing slash so that acting
on a blocked *directory* (removing it, entering it) matches a pattern written for
its contents.

This is a guard rail, not a security boundary. It reads the tool call it is
given; a determined bypass through a command nobody modelled is always possible.

## Stop — don't go idle on open work

`autonomy/hooks/work_drain_stop.py` belongs to the autonomy engine
(`docs/components/autonomy.md`). During an active run it blocks the agent's stop
while it still owns open tasks, with an anti-thrash backstop: if the work
signature has not moved for several turns, it lets the agent stop and marks the
tasks blocked so the coordinator can take over.

Outside a run it is a no-op, so ordinary mesh life is untouched. There is no
ticket-closing Stop hook in this repository; closing a ticket is an explicit
`bus/ticket-complete.py` call by the agent that did the work.

## The contract

- **stdin**: one JSON object from Claude Code (`source` for SessionStart,
  `tool_name`/`tool_input` for PreToolUse, `session_id` for Stop).
- **stdout**: JSON, or nothing. `SessionStart` returns
  `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}`;
  `PreToolUse` returns a `permissionDecision` of `allow` or `deny` (with a
  `permissionDecisionReason`); `Stop` prints `{"decision": "block", "reason": ...}`
  to keep the turn going.
- **exit code**: always 0. These hooks express refusal in their payload, never by
  failing.
- **stderr**: diagnostics. It does not reach the agent's context, which is
  exactly why identity conflicts are reported there.

Test one by hand — it is a script reading stdin:

```sh
echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | MESH_AGENT=curator python3 hooks/scope_check.py
echo '{"source":"startup"}' | MESH_AGENT=curator python3 hooks/session_start.py
```

## Where hooks fit

The watcher pushes a message into a pane within a second when the agent is idle;
`SessionStart` covers everything else — the agent that was down, busy, or
restarted. Together they mean a message is never lost, only delayed. See
`docs/components/bus.md`.
