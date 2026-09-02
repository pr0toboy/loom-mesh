## LoomMesh — demo bus runtime image
##
## A minimal image used by docker-compose.demo.yml to demonstrate the
## bus mechanic (JSONL inbox + inotify watcher + push-via-signal) with
## fake bash agents instead of real Claude Code sessions.
##
## This is NOT a runtime image for the real mesh — see ../Dockerfile
## for the installer/CI image, and see ../../README.md for actually
## running the mesh on a host.
##
## Build:  docker build -t loom-mesh-demo -f docker/demo/Dockerfile docker/demo/

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        jq \
        inotify-tools \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/loom-demo

# The real bus, not a copy of it. This image used to carry its own `send.py` and
# `read.py`: a demo that claims to show the bus mechanic while running different
# code shows nothing, and the two drifted apart the moment either was fixed.
#
# `watcher.sh` is the one thing that stays a stand-in, and the reason is
# structural: the real watcher types into a tmux pane, and there is no terminal
# in this container. It writes to a named pipe instead. Everything else here is
# the code a real deployment runs.
#
# Build context is the repository root: docker build -f docker/demo/Dockerfile .
COPY bus/send.py bus/read.py bus/roster.py ./
COPY docker/demo/watcher.sh docker/demo/fake-agent.sh docker/demo/entrypoint.sh ./
COPY docker/demo/mesh_roster.py ./
RUN chmod +x send.py read.py watcher.sh fake-agent.sh entrypoint.sh

## Unprivileged: the demo writes to a shared volume, and root in a container
## writing to a bind mount produces files the host user cannot delete.
RUN useradd --create-home --uid 10001 loom \
    && chown -R loom:loom /opt/loom-demo
USER loom

ENTRYPOINT ["/opt/loom-demo/entrypoint.sh"]
CMD ["bash"]
