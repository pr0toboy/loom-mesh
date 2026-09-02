#!/usr/bin/env bash
# mesh-send: thin wrapper around send.py, for humans and agents alike.
#
# Usage:
#   mesh-send.sh <from> <to> <priority> <body...>
#
# Examples:
#   mesh-send.sh alice bob normal "your build finished"
#   mesh-send.sh bob carol urgent "look at the deploy log"
#
# This only appends to the recipient's inbox. The watcher notices the file
# change and pushes the message into the recipient's session within about a
# second. If the recipient is busy, the message stays in pull mode and is seen
# on their next read — nothing is lost when the push does not happen.

set -euo pipefail

if [[ $# -lt 4 ]]; then
    echo "usage: $0 <from> <to> <priority> <body...>" >&2
    exit 2
fi

FROM="$1"
TO="$2"
PRI="$3"
shift 3

# The rest is forwarded as separate arguments rather than joined into one.
# Joining it first collapsed an option and its value into the body, so
# `mesh-send.sh a b normal --reply-to <id> "text"` sent the literal string
# "--reply-to <id> text" as the message. send.py joins the remaining words
# itself, so the body is unaffected.
exec python3 "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/send.py" \
    "$FROM" "$TO" "$PRI" "$@"
