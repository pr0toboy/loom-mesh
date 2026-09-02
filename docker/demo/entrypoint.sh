#!/usr/bin/env bash
# Put the demo roster in the shared volume before anything runs.
#
# Every container in the demo does this, and it is idempotent (`cp -n`): doing it
# from a single init container instead leaves a race, since the agents may start
# before it has finished — and the bus refuses an unknown peer, so the race shows
# up as "unknown from peer" rather than as a missing file.
set -euo pipefail

MESH_DIR="${MESH_HOME:-/mesh}"
mkdir -p "$MESH_DIR"
cp -n /opt/loom-demo/mesh_roster.py "$MESH_DIR/mesh_roster.py" 2>/dev/null || true

exec "$@"
