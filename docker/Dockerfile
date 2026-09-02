## LoomMesh — installer / CI image
##
## Builds a clean Python 3.11 image with the repo's installer (bootstrap.sh),
## config-schema validator, and test suite. Useful for:
##   - validating a mesh.toml without touching your host
##   - running `bootstrap.sh --dry-run --no-systemd --no-ssh` to preview output
##   - executing the pytest suite in CI
##
## This image does NOT run the mesh at runtime — there is no Claude Code,
## no tmux, no inotify watcher. For a runtime demo of the bus, see
## ../docker-compose.demo.yml.
##
## Build:   docker build -t loom-mesh-installer -f docker/Dockerfile .
## Validate example config:
##   docker run --rm loom-mesh-installer \
##       python3 config_schema/validate.py config_schema/mesh.example.toml
## Dry-run bootstrap on example config:
##   docker run --rm loom-mesh-installer \
##       ./bootstrap.sh --dry-run --no-systemd --no-ssh config_schema/mesh.example.toml
## Run tests:
##   docker run --rm loom-mesh-installer pytest -q

FROM python:3.11-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        jq \
        coreutils \
        tmux \
        inotify-tools \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/loom-mesh

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt pytest

COPY bootstrap.sh ./
COPY config_schema/ ./config_schema/
COPY templates/    ./templates/
COPY docs/         ./docs/
COPY README.md LICENSE ./

RUN chmod +x bootstrap.sh

## A stub `claude` binary so bootstrap.sh's prereq check passes. The image
## never actually runs Claude Code (no auth, no tmux runtime), but the test
## suite and bootstrap require `command -v claude` to succeed.
RUN printf '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "claude-stub 0.0.0 (loom-mesh-installer image)"; exit 0; fi\necho "claude stub: not a real CLI" >&2\nexit 1\n' > /usr/local/bin/claude \
    && chmod +x /usr/local/bin/claude

## Sanity: validate the example config at build time. The config uses $USER
## interpolation, so we provide a value for the validation pass.
ENV USER=loom
RUN python3 config_schema/validate.py config_schema/mesh.example.toml

## Drop to an unprivileged user for everything that runs afterwards. Nothing in
## this image needs root once the packages are installed, and an installer image
## is exactly the kind of thing people run against a directory of their own —
## `docker run -v "$PWD:/in"` with a root process writes root-owned files into
## it, which is a support ticket at best and a mangled home directory at worst.
RUN useradd --create-home --uid 10001 loom \
    && chown -R loom:loom /opt/loom-mesh
USER loom

CMD ["bash"]
