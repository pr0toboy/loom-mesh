#!/usr/bin/env bash
# Push new messages into their recipient's session, within about a second.
#
# The bus is durable on its own: send.py appends and fsyncs, and any agent can
# read its inbox at any time. This process only removes the waiting. It watches
# the bus directory with inotify and, when an inbox grows, types the new message
# into that agent's terminal — locally with tmux, or over SSH for an agent
# living on another machine.
#
# Two rules shape everything below.
#
# 1. **Never type into a working session.** Injecting text while an agent is
#    mid-task interrupts it, and can even land inside a prompt it was writing.
#    So a push happens only when the pane looks idle; when it does not, the
#    message stays where it is and the agent picks it up on its next read. A
#    skipped push is a delay, never a loss — which is why this process is
#    allowed to be simple and to crash.
# 2. **What is typed is data, not commands.** The body is sanitised and never
#    reaches a shell: the recipient is told *that* a message arrived and from
#    whom, and reads it itself. An inbox is written by other agents, and an
#    agent's terminal executes what it is given.
#
# Configuration:
#   $MESH_HOME/hosts.json   optional. Per-agent overrides:
#                             {"carol": {"ssh": "user@host", "session": "carol"}}
#                           An agent with an "ssh" entry is pushed over SSH; an
#                           agent absent from the file is local, with a tmux
#                           session named after it.
#
# Environment:
#   MESH_HOME     bus directory (default ~/mesh)
#   MESH_TMUX     tmux binary or socket invocation (default "tmux")
#   MESH_SSH_OPTS extra ssh options (default: batch mode, 10s connect timeout)
#
# Exit: runs until killed. systemd restarts it; inotify state does not survive,
# which is fine — messages missed while it was down are read on the next pull.

set -uo pipefail

MESH_DIR="${MESH_HOME:-$HOME/mesh}"
LOG_DIR="$MESH_DIR/logs"
LOG_FILE="$LOG_DIR/watcher.log"
HOSTS_FILE="$MESH_DIR/hosts.json"
TMUX_BIN="${MESH_TMUX:-tmux}"
DEFAULT_MESH_DIR="$HOME/mesh"

# ── The mesh and the tmux server must come from the same place ────────────────
#
# `push_local` ends with `send-keys Enter`: it does not hand text to a pane, it
# RUNS it. The only thing separating a test bench from a live agent used to be
# that no session happened to be named like one — `has-session -t alice` simply
# failed. That is isolation by naming convention, and this repository's own
# production deployment breaks it by construction: there, sessions ARE named
# after the agents. A bench run with a roster holding a real agent name would
# type a command into that agent's pane and press Enter.
#
# Observed on 2026-09-03: a read-only probe of this repository, running the
# watcher from a copied mesh with the alice/bob fixture, aimed at the DEFAULT
# tmux server — the production one. Nothing was executed, only because no
# session carried those names.
#
# So the rule is intrinsic, and needs no notion of "production": a mesh home
# that is not the default one, driving the default tmux server, is an
# inconsistency rather than a configuration. Say so and refuse, rather than
# discover it in someone's pane.
# Paths are compared resolved, not as strings: a MESH_HOME with a trailing slash,
# or reached through a symlink, is the same deployment and must not be refused.
_same_path() {
    [ "$(cd "$1" 2>/dev/null && pwd -P || printf '%s' "$1")" \
      = "$(cd "$2" 2>/dev/null && pwd -P || printf '%s' "$2")" ]
}

# A private server (-L NAME / -S PATH) is one this deployment created: whatever
# sessions it holds, they are not the operator's agents. The SHARED default
# server is the only dangerous target, so it is the only one worth testing for.
_targets_shared_server() {
    local a first=""
    for a in $TMUX_BIN; do
        [ -z "$first" ] && first="$a"
        if [ "$a" = "-L" ] || [ "$a" = "-S" ]; then return 1; fi
    done
    # Only the tmux binary itself reaches the shared server. A deployment that
    # points MESH_TMUX at something else — a wrapper, a stub, the fake used by
    # the bus tests — has substituted the program on purpose, which is as
    # explicit as naming a socket. Checking merely "no -L" instead broke that
    # case, and the suite's own control test caught it.
    case "${first##*/}" in (tmux) return 0 ;; (*) return 1 ;; esac
}

# Merely DECLARING a server used to be enough to pass here, and that is the hole
# this closes: `MESH_TMUX=tmux` is a declaration, and it names the shared server.
# Measured on 2026-09-09 — MESH_HOME=/tmp/bench-mesh with MESH_TMUX=tmux was
# accepted, which is a bench cleared to type into live agent panes, in a mesh
# whose sessions ARE named after its agents, with an Enter at the end. The rule
# the comment above always claimed is now the rule the code applies.
_tmux_scope_is_consistent() {
    _targets_shared_server || return 0                    # a private server: never the agents
    _same_path "$MESH_DIR" "$DEFAULT_MESH_DIR" && return 0 # the installed mesh, at its default home
    [ "${MESH_ALLOW_SHARED_TMUX:-}" = "1" ] && return 0    # an install elsewhere, saying so out loud
    return 1
}

_scope_error() {
    printf 'refusing to drive the default tmux server: this mesh has not declared one.\n'
    printf '  MESH_HOME = %s\n  default   = %s\n' "$MESH_DIR" "$DEFAULT_MESH_DIR"
    printf '  push_local ends with Enter, so it RUNS what it types. Without a declared\n'
    printf '  server, a copy of this repository would type into whatever sessions the\n'
    printf '  default server happens to hold — including live agents whose sessions are\n'
    printf '  named after them.\n'
    printf '  An installed mesh says so: the units bootstrap.sh writes carry both\n'
    printf '  Environment=MESH_TMUX and Environment=MESH_ALLOW_SHARED_TMUX=1. Re-run\n'
    printf '  bootstrap, or choose one of these yourself:\n'
    printf '    MESH_TMUX="tmux -L NAME"       a private server — accepted for the LOCAL\n'
    printf '                                   push only. The SSH push names no socket,\n'
    printf '                                   so it always lands on the remote shared\n'
    printf '                                   server and needs the line below;\n'
    printf '    MESH_ALLOW_SHARED_TMUX=1       this IS the install, drive the shared\n'
    printf '                                   server from a non-default mesh home.\n'
    printf '  For reference, what the two settings mean:\n'
    printf '    MESH_TMUX=tmux            the shared default server, where agent@ and\n'
    printf '                              tmux-server put the agent sessions;\n'
    printf '    MESH_TMUX="tmux -L NAME"  a private server — use this ONLY if the agent\n'
    printf '                              sessions live on that socket too, otherwise\n'
    printf '                              delivery goes to an empty server and every\n'
    printf '                              push is silently skipped.\n'
}
SSH_OPTS_DEFAULT=(-o BatchMode=yes -o ConnectTimeout=10)
read -r -a SSH_OPTS <<< "${MESH_SSH_OPTS:-}"
[ ${#SSH_OPTS[@]} -eq 0 ] && SSH_OPTS=("${SSH_OPTS_DEFAULT[@]}")

mkdir -p "$LOG_DIR"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG_FILE"; }

# ── Peer name validation ──────────────────────────────────────────────────────
# The recipient is derived from a FILE NAME, and then used to build an SSH
# command and a tmux target. Anything that is not a plain peer name is dropped
# here rather than sanitised later: a file called `inbox-$(id).jsonl` is not a
# recipient, it is an attempt.
_is_valid_peer() {
    [[ "$1" =~ ^[a-z][a-z0-9-]{0,31}$ ]]
}

# ── Per-agent transport, read from hosts.json ─────────────────────────────────
_agent_field() {   # _agent_field <agent> <field>
    [ -r "$HOSTS_FILE" ] || return 1
    python3 - "$HOSTS_FILE" "$1" "$2" <<'PYEOF' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1]) as fh:
        hosts = json.load(fh)
except Exception:
    sys.exit(1)
entry = hosts.get(sys.argv[2]) or {}
value = entry.get(sys.argv[3])
if not isinstance(value, str) or not value:
    sys.exit(1)
print(value)
PYEOF
}

agent_ssh()     { _agent_field "$1" ssh; }
agent_session() { _agent_field "$1" session || printf '%s' "$1"; }

# ── Idle detection ────────────────────────────────────────────────────────────
# Heuristic, and deliberately biased towards "busy": a false "busy" costs a
# delay, a false "idle" interrupts work in flight.
#
# Busy is judged first and on the strongest signal available — an elapsed timer
# ("(12s", "(1m 4s") or an interrupt hint means the session is producing output
# right now. Idle is then judged on the LAST non-empty line, because that is
# where a prompt lives; scanning the whole capture matched prompt-looking text
# in scrollback and pushed into working sessions.
#
# The patterns are overridable because they are the one part of this file that
# depends on which CLI the agent runs. Getting them wrong is not silent: the
# watcher logs "skip push" for every message it declines, so a session that
# never receives anything says so in the log.
#
#   MESH_BUSY_PATTERN   extended regex; matching anywhere in the capture = busy
#   MESH_IDLE_PATTERN   extended regex; matching the last line = idle
# Written with single quotes and a separate default: inside a double-quoted
# ${VAR:-default} the shell expands the default too — `$#` became the argument
# count, the pattern stopped matching any prompt, and every push was skipped
# while the log claimed the session was "busy".
_BUSY_PATTERN="${MESH_BUSY_PATTERN:-}"
[ -z "$_BUSY_PATTERN" ] && _BUSY_PATTERN='\([0-9]+[ms]|esc to interrupt'
_IDLE_PATTERN="${MESH_IDLE_PATTERN:-}"
[ -z "$_IDLE_PATTERN" ] && _IDLE_PATTERN='(shift\+tab to cycle|for agents)|[$#>❯][[:space:]]*$'

_pane_is_idle() {
    local pane_text="$1" last_line
    [ -z "$pane_text" ] && return 1
    grep -qE "$_BUSY_PATTERN" <<< "$pane_text" && return 1
    last_line="$(grep -vE '^[[:space:]]*$' <<< "$pane_text" | tail -1)"
    [ -z "$last_line" ] && return 1
    grep -qE "$_IDLE_PATTERN" <<< "$last_line"
}

# Blank lines are dropped BEFORE the tail. A pane is as tall as the terminal, so
# a session sitting at its prompt has that prompt near the top and dozens of
# empty lines under it: tailing first returns nothing but blanks, every session
# reads as "not idle", and nothing is ever delivered.
capture_local()  { $TMUX_BIN capture-pane -p -t "$1" 2>/dev/null | grep . | tail -6; }
# Reading is still reaching out: this opens an SSH connection to the operator's
# remote host. It used to run BEFORE any scope check, so a clone contacted that
# machine and only then got refused — the refusal was on the write, the contact
# had already happened. Guarded here too, in the primitive that does the effect.
capture_remote() {
    _remote_scope_is_consistent || return 1
    ssh "${SSH_OPTS[@]}" "$1" "tmux capture-pane -p -t $(printf '%q' "$2") 2>/dev/null | grep . | tail -6" 2>/dev/null
}

# ── Injection ─────────────────────────────────────────────────────────────────
# Strip control characters and newlines from anything that goes through
# send-keys. Keystrokes are not a data channel: a newline in the middle would
# submit half a line as a command.
_sanitize() {
    tr -d '\000-\010\013\014\016-\037' | tr '\n\r' '  ' | cut -c1-400
}

build_notice() {   # build_notice <from> <to> <id>
    local from="$1" to="$2" id="$3"
    # Plain ASCII, no em dash: the remote push quotes this with printf %q, which
    # escapes non-ASCII into a bash-only $'...' form that other shells mangle.
    printf '[mesh] new message from %s (id=%s). Read it: python3 %s/read.py %s - then acknowledge: python3 %s/read.py %s --ack %s' \
        "$from" "$id" "$MESH_DIR" "$to" "$MESH_DIR" "$to" "$id" | _sanitize
}

push_local() {   # push_local <session> <text>
    local session="$1" text="$2"
    # Last-resort guard, here rather than only at startup: this is the function
    # that presses Enter, and it is reachable by sourcing this file.
    if ! _tmux_scope_is_consistent; then
        log "REFUSED push: $(_scope_error | head -1) (MESH_HOME=$MESH_DIR)"
        return 1
    fi
    $TMUX_BIN has-session -t "$session" 2>/dev/null || return 1
    # -l sends the text literally: without it tmux reads words like "Enter" or
    # "C-c" inside the message as key names. Enter is then sent as its own key,
    # so the body can never submit itself. Each call is bounded: one wedged
    # send-keys once froze delivery for the whole mesh, not just this message.
    timeout 5 $TMUX_BIN send-keys -t "$session" C-u 2>/dev/null
    timeout 5 $TMUX_BIN send-keys -t "$session" -l "$text" 2>/dev/null || return 1
    sleep 0.4
    timeout 5 $TMUX_BIN send-keys -t "$session" Enter 2>/dev/null || return 1
    return 0
}

# The SSH branch had NO scope check at all, found by the 2026-09-09 gate: I had
# hardened push_local and left its twin open. The command sent over SSH is a bare
# `tmux send-keys` — so it always drives the REMOTE host's SHARED server, where
# that host's agents live, and it ends with Enter. A bench with a hosts.json entry
# therefore ran a command in a remote agent's pane; proven with a stub ssh.
#
# There is no private-socket escape hatch here, since the remote command names no
# socket: what remains is the same question as locally — is this the installation,
# or a copy of it? Default mesh home, or the deployment saying so out loud.
_remote_scope_is_consistent() {
    _same_path "$MESH_DIR" "$DEFAULT_MESH_DIR" && return 0
    [ "${MESH_ALLOW_SHARED_TMUX:-}" = "1" ] && return 0
    return 1
}

push_remote() {   # push_remote <ssh-target> <session> <text>
    local target="$1" session="$2" text="$3"
    if ! _remote_scope_is_consistent; then
        log "REFUSED push (ssh): mesh home is not the installed one (MESH_HOME=$MESH_DIR)"
        printf 'refusing to push over SSH from a mesh that is not the installed one.\n' >&2
        printf '  MESH_HOME = %s\n  default   = %s\n' "$MESH_DIR" "$DEFAULT_MESH_DIR" >&2
        printf '  The remote command is a bare `tmux send-keys ... Enter`, so it RUNS in\n' >&2
        printf '  whatever session the remote shared server holds under that name.\n' >&2
        printf '  An installed mesh carries MESH_ALLOW_SHARED_TMUX=1 in its units.\n' >&2
        return 1
    fi
    # printf %q under a C locale escapes any non-ASCII byte as $'\nnn', which
    # only bash understands — a remote shell that is not bash then receives
    # gibberish. The notice is plain ASCII for that reason; this keeps it true
    # even when the caller's locale is not.
    local LC_ALL=C.UTF-8
    ssh "${SSH_OPTS[@]}" "$target" \
        "tmux has-session -t $(printf '%q' "$session") 2>/dev/null" || return 1
    ssh "${SSH_OPTS[@]}" "$target" \
        "tmux send-keys -t $(printf '%q' "$session") -l $(printf '%q' "$text") && sleep 0.4 && tmux send-keys -t $(printf '%q' "$session") Enter" \
        >/dev/null 2>&1 || return 1
    return 0
}

# ── One message ───────────────────────────────────────────────────────────────
try_push() {   # try_push <to> <from> <id>
    local to="$1" from="$2" id="$3"
    local session ssh_target pane text

    session="$(agent_session "$to")"
    ssh_target="$(agent_ssh "$to" || true)"
    text="$(build_notice "$from" "$to" "$id")"

    if [ -n "$ssh_target" ]; then
        # Checked before the first packet leaves, and named for what it is: a
        # refusal logged as "busy or unreachable" sends whoever reads the log
        # looking at the network for a decision this file made.
        if ! _remote_scope_is_consistent; then
            log "REFUSED push (ssh) -> $to id=$id: mesh home is not the installed one"
            return 2
        fi
        pane="$(capture_remote "$ssh_target" "$session")"
        if ! _pane_is_idle "$pane"; then
            log "skip push (busy or unreachable) -> $to id=$id"
            return 2
        fi
        if push_remote "$ssh_target" "$session" "$text"; then
            log "push ssh OK -> $to id=$id"
            return 0
        fi
        log "skip push (ssh failed) -> $to id=$id"
        return 2
    fi

    pane="$(capture_local "$session")"
    if ! _pane_is_idle "$pane"; then
        log "skip push (busy or no session) -> $to id=$id"
        return 2
    fi
    if push_local "$session" "$text"; then
        log "push local OK -> $to id=$id"
        return 0
    fi
    log "skip push (send-keys failed) -> $to id=$id"
    return 2
}

# ── Event handling ────────────────────────────────────────────────────────────
# Only the last line of the inbox is considered. A burst of appends produces a
# burst of events, and pushing every one of them would type N notices into a
# session that only needs to be told to go and read.
handle_event() {   # handle_event <filename>
    local file="$1" to id from
    case "$file" in
        inbox-*.jsonl) ;;
        *) return 0 ;;
    esac
    to="${file#inbox-}"
    to="${to%.jsonl}"
    if ! _is_valid_peer "$to"; then
        log "ignored event for invalid peer name: $file"
        return 0
    fi

    local last
    last="$(tail -1 "$MESH_DIR/$file" 2>/dev/null)"
    [ -z "$last" ] && return 0

    id="$(python3 -c 'import json,sys
try: print(json.loads(sys.argv[1]).get("id",""))
except Exception: print("")' "$last" 2>/dev/null)"
    from="$(python3 -c 'import json,sys
try: print(json.loads(sys.argv[1]).get("from",""))
except Exception: print("")' "$last" 2>/dev/null)"
    [ -z "$id" ] && return 0
    # The id ends up inside the text typed into a pane. It comes from a file
    # another agent wrote, so it is checked in the same spirit as the peer name:
    # anything that is not an id is not typed. `what is typed is data` has to
    # hold for every field, not only the body.
    if ! [[ "$id" =~ ^[0-9a-f]{4,32}$ ]]; then
        log "ignored message with a malformed id in $file"
        return 0
    fi
    _is_valid_peer "$from" || from="unknown"

    # inotify reports create AND modify for one append, so the same message
    # arrives twice and used to be typed twice into the session. The last id
    # pushed per recipient is remembered; anything already pushed is dropped.
    local marker="$MESH_DIR/.watcher-pushed-$to"
    if [ "$(cat "$marker" 2>/dev/null)" = "$id" ]; then
        return 0
    fi

    if try_push "$to" "$from" "$id"; then
        printf '%s' "$id" > "$marker"
    fi
}

main() {
    command -v inotifywait >/dev/null 2>&1 || {
        echo "inotifywait not found — install inotify-tools" >&2
        exit 1
    }
    if ! _tmux_scope_is_consistent; then
        _scope_error >&2
        exit 2
    fi
    mkdir -p "$MESH_DIR"
    log "watcher started on $MESH_DIR"
    inotifywait -m -e modify -e create --format '%f' "$MESH_DIR" 2>/dev/null \
    | while read -r file; do
        handle_event "$file"
    done
}

# Sourcing this file (for tests) must not start the watch loop.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
