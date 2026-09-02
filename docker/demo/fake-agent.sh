#!/usr/bin/env bash
# Demo fake-agent: ping/pong loop, blocks on FIFO between rounds.
#
# This is NOT Claude Code. It's a 40-line bash script that demonstrates
# the agent-side half of the mesh bus: drain unread messages, reply,
# then block until the watcher pushes a wake signal.
#
# Env:
#   MESH_AGENT    — this agent's name (required)
#   MESH_PARTNER  — agent to ping initially (required)
#   MESH_HOME     — bus directory (default: /mesh)
#   MAX_PINGS     — stop after N rounds (default: 3)

set -uo pipefail

SELF="${MESH_AGENT:?MESH_AGENT must be set}"
PARTNER="${MESH_PARTNER:?MESH_PARTNER must be set}"
MESH_DIR="${MESH_HOME:-/mesh}"
MAX_PINGS="${MAX_PINGS:-3}"

INBOX="$MESH_DIR/inbox-$SELF.jsonl"
FIFO="$MESH_DIR/trigger-$SELF"
SEND_PY="/opt/loom-demo/send.py"
READ_PY="/opt/loom-demo/read.py"

mkdir -p "$MESH_DIR"
touch "$INBOX"
[[ -p "$FIFO" ]] || mkfifo "$FIFO"

echo "[$SELF] online (partner=$PARTNER, max=$MAX_PINGS)"

# Whoever has the alphabetically-smaller name opens the conversation.
if [[ "$SELF" < "$PARTNER" ]]; then
    sleep 2
    python3 "$SEND_PY" "$SELF" "$PARTNER" normal "ping #1"
fi

rounds=0
while (( rounds < MAX_PINGS )); do
    # Drain unread
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        id=$(printf '%s' "$line" | jq -r 'select(.acked == false) | .id // empty')
        [[ -z "$id" ]] && continue
        from=$(printf '%s' "$line" | jq -r .from)
        body=$(printf '%s' "$line" | jq -r .body)
        echo "[$SELF] recv from=$from id=$id body=\"$body\""
        python3 "$READ_PY" "$SELF" --ack "$id" > /dev/null
        rounds=$((rounds + 1))
        if (( rounds <= MAX_PINGS )); then
            python3 "$SEND_PY" "$SELF" "$from" normal "pong #$rounds (re $id)"
        fi
    done < "$INBOX"

    (( rounds >= MAX_PINGS )) && break

    # Block until watcher wakes us. Timeout so we don't hang forever
    # if the partner crashes — re-drain inbox just in case.
    if read -r -t 30 _ < "$FIFO"; then
        :  # got woken
    fi
done

echo "[$SELF] done after $rounds round(s) — exiting clean"
