#!/usr/bin/env bash
# Demo watcher: inotifywait on the mesh dir, signal recipient via FIFO.
#
# In the real mesh, this script does `tmux send-keys` into the recipient
# agent's pane. Inside Docker there is no tmux session to wake — so we
# write into a per-agent named pipe instead. The fake-agent blocks on
# that FIFO until it receives a wake signal, then drains its inbox.
#
# Env:
#   MESH_HOME — bus directory (default: /mesh)

set -uo pipefail

MESH_DIR="${MESH_HOME:-/mesh}"

echo "[watcher] watching $MESH_DIR"

inotifywait -m -e modify --format '%f' "$MESH_DIR" 2>/dev/null \
| while read -r file; do
    case "$file" in
        inbox-*.jsonl)
            to="${file#inbox-}"
            to="${to%.jsonl}"
            fifo="$MESH_DIR/trigger-$to"
            if [[ -p "$fifo" ]]; then
                # Non-blocking write — don't hang if agent isn't currently reading
                ( printf 'wake\n' > "$fifo" ) &
                echo "[watcher] $(date -u +%H:%M:%S) push -> $to"
            else
                echo "[watcher] $(date -u +%H:%M:%S) no fifo for $to (msg in pull-mode)"
            fi
            ;;
    esac
done
