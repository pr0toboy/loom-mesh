#!/usr/bin/env bash
# mesh-send-checked: send, then wait ~5s and report whether the recipient was
# actually reached — instead of assuming a written file means a delivered
# message.
#
# Use it when the answer matters now, so you do not sit waiting for a reply that
# cannot come because the recipient was asleep, busy, or unreachable.
#
# Usage: same as mesh-send.sh
#   mesh-send-checked.sh <from> <to> <priority> <body...>
#
# Exit codes:
#   0 = written and pushed to the recipient (delivered live)
#   1 = the send itself failed (no id to check)
#   2 = written, but the push was skipped (recipient busy or unreachable);
#       they will see it on their next session start or manual read
#   3 = written, and no trace of the watcher after MESH_CHECK_WAIT seconds —
#       the watcher may be down, look at $MESH_HOME/logs/watcher.log
#
# Chat facades are checked differently on purpose. A message to a human's chat
# room is not delivered by the watcher but by the bridge, which writes nothing
# to the watcher log — so the log check reported "watcher may be down" on every
# perfectly delivered message, and senders resent, doubling the human's
# notifications. For those peers we ask the bridge's own state file instead.
#
# Env:
#   MESH_CHECK_WAIT    seconds to wait before checking (default 5)
#   MESH_BRIDGE_STATE  bridge state file (default $MESH_HOME/bridge-state.json)
#   MESH_BRIDGE_PEERS  comma-separated peers delivered by the bridge, not the
#                      watcher (default: none)

set -uo pipefail

MESH_DIR="${MESH_HOME:-$HOME/mesh}"
BUS_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
LOG="$MESH_DIR/logs/watcher.log"
WAIT_SECONDS="${MESH_CHECK_WAIT:-5}"
BRIDGE_STATE="${MESH_BRIDGE_STATE:-$MESH_DIR/bridge-state.json}"

if [[ $# -lt 4 ]]; then
    echo "usage: $0 <from> <to> <priority> <body...>" >&2
    exit 2
fi

TO="$2"

SEND_OUT=$(bash "$BUS_DIR/mesh-send.sh" "$@" 2>&1)
SEND_RC=$?
printf '%s\n' "$SEND_OUT"
if [ $SEND_RC -ne 0 ]; then
    exit 1
fi

ID=$(printf '%s\n' "$SEND_OUT" | grep -oE 'id=[0-9a-f]+' | head -1 | cut -d= -f2)
if [ -z "$ID" ]; then
    echo "WARN: could not parse the message id, skipping the delivery check" >&2
    exit 0
fi

# ── Peers delivered by the chat bridge ────────────────────────────────────────
IFS=',' read -r -a BRIDGE_PEERS <<< "${MESH_BRIDGE_PEERS:-}"
for peer in "${BRIDGE_PEERS[@]:-}"; do
    [ -z "$peer" ] && continue
    [ "$TO" != "$peer" ] && continue

    # Poll rather than sleep flat: the bridge usually relays in under a second.
    # A read can also land mid-rewrite (truncated JSON), hence the retry.
    for _ in $(seq 1 "$WAIT_SECONDS"); do
        if python3 - "$BRIDGE_STATE" "$ID" <<'PYCHK'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        state = json.load(fh)
except Exception:
    sys.exit(1)          # unreadable / being written — retry
sys.exit(0 if sys.argv[2] in state.get("relayed_ids", []) else 1)
PYCHK
        then
            echo "[mesh-check] ✓ relayed to the chat room by the bridge (id=$ID)"
            exit 0
        fi
        sleep 1
    done

    echo "[mesh-check] ⚠ not relayed after ${WAIT_SECONDS}s (id=$ID)" >&2
    echo "  It is written in inbox-$TO.jsonl; the bridge resumes from its cursor." >&2
    echo "  Do NOT resend — check the bridge service before assuming it was lost." >&2
    exit 3
done

sleep "$WAIT_SECONDS"

TRACE=$(grep -- "$ID" "$LOG" 2>/dev/null | tail -5)

# "skip push" is checked first: the word "push" also appears inside it.
if printf '%s\n' "$TRACE" | grep -q 'skip push'; then
    echo "[mesh-check] ⚠ push SKIPPED — $TO is busy or unreachable (id=$ID)"
    echo "  They will see it on their next session start or read." >&2
    exit 2
elif printf '%s\n' "$TRACE" | grep -qE 'push +(local|ssh|remote)? *OK'; then
    echo "[mesh-check] ✓ pushed to $TO (id=$ID)"
    exit 0
elif [ -z "$TRACE" ]; then
    echo "[mesh-check] ⚠ no watcher trace after ${WAIT_SECONDS}s for id=$ID — watcher may be down" >&2
    exit 3
else
    echo "[mesh-check] ? unexpected watcher trace for id=$ID:"
    printf '%s\n' "$TRACE"
    exit 4
fi
