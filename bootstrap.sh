#!/usr/bin/env bash
# bootstrap.sh — Idempotent mesh deployment from a mesh.toml config.
#
# Usage:
#   ./bootstrap.sh [OPTIONS] [mesh.toml]
#
# Options:
#   --dry-run        Print what would be done, write nothing
#   --no-systemd     Skip systemd unit installation (useful in containers/CI)
#   --no-ssh         Skip remote host deployment
#   --skip-health    Skip health checks at the end
#   -h, --help       Show this help
#
# If mesh.toml is not provided, looks for it in the current directory.
#
# Exit codes:
#   0  success
#   1  validation / prerequisite error
#   2  partial failure (some health checks failed)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/tmp/mesh-install-$(date +%Y%m%dT%H%M%S).log"

# ── Option parsing ──────────────────────────────────────────────────────────────

DRY_RUN=false
NO_SYSTEMD=false
NO_SSH=false
SKIP_HEALTH=false
CONFIG_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true ;;
        --no-systemd) NO_SYSTEMD=true ;;
        --no-ssh)     NO_SSH=true ;;
        --skip-health) SKIP_HEALTH=true ;;
        -h|--help)
            sed -n '/^# Usage:/,/^[^#]/{ s/^# \?//; p }' "$0" | head -20
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2; exit 1 ;;
        *)
            CONFIG_PATH="$1" ;;
    esac
    shift
done

[[ -z "$CONFIG_PATH" ]] && CONFIG_PATH="mesh.toml"

# ── Logging ─────────────────────────────────────────────────────────────────────

_log() { echo "$(date -Iseconds) $*" | tee -a "$LOG_FILE"; }
_info()  { _log "[INFO]  $*"; }
_ok()    { _log "[OK]    $*"; }
_warn()  { _log "[WARN]  $*"; }
_error() { _log "[ERROR] $*" >&2; }
_dry()   { _log "[DRY]   $*"; }

_run() {
    # _run <description> <cmd...>
    local desc="$1"; shift
    if $DRY_RUN; then
        _dry "$desc: $*"
    else
        _info "$desc"
        "$@" >> "$LOG_FILE" 2>&1 || { _error "$desc FAILED (exit $?)"; return 1; }
        _ok "$desc"
    fi
}

_run_silent() {
    # Like _run but suppress stdout/stderr from log (sensitive data)
    local desc="$1"; shift
    if $DRY_RUN; then
        _dry "$desc"
    else
        _info "$desc"
        "$@" >/dev/null 2>&1 || { _error "$desc FAILED (exit $?)"; return 1; }
        _ok "$desc"
    fi
}

_expand_path() {
    # Safely expand a leading ~ to $HOME without invoking python.
    echo "${1/#\~/$HOME}"
}

_sed_escape() {
    # Escape \, &, and | (our sed delimiter) in replacement strings.
    printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

# ── Step 1: Prerequisites ────────────────────────────────────────────────────────

check_prerequisites() {
    _info "Step 1: Checking prerequisites"
    local missing=()

    for cmd in python3 tmux jq inotifywait; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    # claude CLI — warn only (not required for --dry-run)
    if ! command -v claude &>/dev/null; then
        if $DRY_RUN; then
            _warn "claude CLI not found (OK for --dry-run)"
        else
            missing+=("claude (https://claude.ai/claude-code)")
        fi
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        _error "Missing prerequisites: ${missing[*]}"
        _error "Install them and re-run bootstrap.sh"
        exit 1
    fi

    # Python deps
    if ! python3 -c "import pydantic, tomllib" 2>/dev/null; then
        _error "Python deps missing. Run: pip install pydantic"
        exit 1
    fi

    _ok "All prerequisites present"
}

# ── Step 2: Validate config ──────────────────────────────────────────────────────

MESH_HOME=""
API_PORT=""
API_BIND=""
LOG_DIR=""
declare -A AGENT_NAMES=()
declare -A AGENT_MODELS=()
declare -A AGENT_ROLES=()
declare -A AGENT_WORKDIRS=()
declare -A AGENT_HOSTS=()
declare -A AGENT_WSL=()
declare -A AGENT_CHARTERS=()

validate_config() {
    _info "Step 2: Validating $CONFIG_PATH"

    if [[ ! -f "$CONFIG_PATH" ]]; then
        _error "Config file not found: $CONFIG_PATH"
        exit 1
    fi

    local result
    result=$(python3 -c "
import sys, json
sys.path.insert(0, '$SCRIPT_DIR')
from config_schema.schema import load_config
from pydantic import ValidationError
try:
    cfg = load_config('$CONFIG_PATH')
    agents = [{'name': a.name, 'model': a.model, 'role': a.role,
               'workdir': a.workdir, 'host': a.host,
               'wsl': a.wsl_distro or '', 'charter': a.charter_template}
              for a in cfg.agents]
    print(json.dumps({
        'ok': True,
        'mesh_home': str(cfg.mesh.home_path),
        'api_port': cfg.mesh.api_port,
        'api_bind': cfg.mesh.api_bind,
        'log_dir': str(cfg.mesh.log_dir_path),
        'agents': agents,
        'remote_hosts': [{'name': h.name, 'user': h.user, 'host': h.host,
                          'ssh_key': h.ssh_key or '', 'wsl': h.wsl_distro or ''}
                         for h in cfg.hosts.remote],
    }))
except ValidationError as e:
    print(json.dumps({'ok': False, 'errors': [err['msg'] for err in e.errors()]}))
except Exception as e:
    print(json.dumps({'ok': False, 'errors': [str(e)]}))
" 2>/dev/null) || { _error "Python error while parsing config"; exit 1; }

    if [[ $(echo "$result" | jq -r '.ok') != "true" ]]; then
        _error "Config validation failed:"
        echo "$result" | jq -r '.errors[]' | while read -r err; do
            _error "  $err"
        done
        exit 1
    fi

    MESH_HOME=$(echo "$result" | jq -r '.mesh_home')
    API_PORT=$(echo "$result"  | jq -r '.api_port')
    API_BIND=$(echo "$result"  | jq -r '.api_bind')
    LOG_DIR=$(echo "$result"   | jq -r '.log_dir')

    # Load agents into associative arrays
    while IFS= read -r line; do
        local name model workdir host wsl charter
        name=$(echo "$line"    | jq -r '.name')
        model=$(echo "$line"   | jq -r '.model')
        role=$(echo "$line"    | jq -r '.role')
        workdir=$(echo "$line" | jq -r '.workdir')
        host=$(echo "$line"    | jq -r '.host')
        wsl=$(echo "$line"     | jq -r '.wsl')
        charter=$(echo "$line" | jq -r '.charter')
        AGENT_NAMES[$name]="$name"
        AGENT_MODELS[$name]="$model"
        AGENT_ROLES[$name]="$role"
        AGENT_WORKDIRS[$name]="$workdir"
        AGENT_HOSTS[$name]="$host"
        AGENT_WSL[$name]="$wsl"
        AGENT_CHARTERS[$name]="$charter"
    done < <(echo "$result" | jq -c '.agents[]')

    # How the services will reach tmux. "tmux" is the shared default server —
    # the one agent@ and tmux-server put the sessions on. It is written into the
    # units so an INSTALLED mesh declares it, while a clone run by hand declares
    # nothing and is refused by the writers' scope guard.
    MESH_TMUX="${MESH_TMUX:-tmux}"
    _ok "Config valid: ${#AGENT_NAMES[@]} agents, mesh home=$MESH_HOME, api=$API_BIND:$API_PORT"
}

# ── Step 3: Generate peers.py + peers.sh ─────────────────────────────────────────

generate_peers() {
    _info "Step 3: Generating peers.py and peers.sh"

    local agent_list_py agent_list_sh
    agent_list_py=$(printf '"%s", ' "${!AGENT_NAMES[@]}" | sed 's/, $//')
    agent_list_sh=$(printf '"%s" ' "${!AGENT_NAMES[@]}" | sed 's/ $//')

    if $DRY_RUN; then
        _dry "Would write $MESH_HOME/peers.py with agents: $agent_list_py"
        _dry "Would write $MESH_HOME/peers.sh with agents: $agent_list_sh"
        return
    fi

    mkdir -p "$MESH_HOME"

    cat > "$MESH_HOME/peers.py" <<PYEOF
# Auto-generated by bootstrap.sh — do not edit by hand.
AGENTS = {${agent_list_py}}
# The human pilot's passive peers: the webui (POSTs as user-web) and the
# Matrix bridge (relays as pilot-matrix). Neither runs an agent.
PILOT_PEERS = {"user-web", "pilot-matrix"}
ALL_PEERS = sorted(AGENTS | PILOT_PEERS)
PYEOF

    cat > "$MESH_HOME/peers.sh" <<SHEOF
# Auto-generated by bootstrap.sh — do not edit by hand.
AGENTS=(${agent_list_sh})
# Human pilot's passive peers (webui + Matrix bridge); neither runs an agent.
PILOT_PEERS=("user-web" "pilot-matrix")
SHEOF

    _ok "peers.py and peers.sh written to $MESH_HOME"
}

# ── Step 3b: Install the bus ─────────────────────────────────────────────────────
#
# The bus scripts live in the repository and are copied into the mesh home,
# because that is where every consumer looks for them: the systemd units start
# $MESH_HOME/watcher.sh, the API shells out to $MESH_HOME/mesh-send-checked.sh,
# and agents are told to read with $MESH_HOME/read.py. Leaving them in a
# checkout would make the deployment depend on where the repository happens to
# sit — and on a machine where the checkout is later moved, the mesh stops
# delivering with nothing to explain why.
#
# Copying (rather than symlinking) also means a running mesh keeps working while
# the repository is edited: an upgrade is a re-run of this script, not a side
# effect of a git checkout.

install_bus() {
    _info "Step 3b: Installing the bus into $MESH_HOME"

    local bus_src="$SCRIPT_DIR/bus"
    if [[ ! -d "$bus_src" ]]; then
        _warn "  bus/ not found at $bus_src — skipping"
        return
    fi

    if $DRY_RUN; then
        _dry "Would copy $bus_src/{send.py,read.py,roster.py,ticket-complete.py,mesh-send.sh,mesh-send-checked.sh,watcher.sh} to $MESH_HOME"
        _dry "Would write $MESH_HOME/hosts.json (example) if absent"
        return
    fi

    mkdir -p "$MESH_HOME" "$MESH_HOME/logs"
    local f
    for f in "$bus_src"/*.py "$bus_src"/*.sh; do
        [[ -f "$f" ]] || continue
        cp "$f" "$MESH_HOME/"
        chmod 0755 "$MESH_HOME/$(basename "$f")"
    done

    # Written once, never overwritten: it holds the operator's own hosts.
    if [[ ! -f "$MESH_HOME/hosts.json" ]]; then
        cat > "$MESH_HOME/hosts.json" <<'JSONEOF'
{
  "_comment": "Per-agent transport. An agent absent from this file is local, in a tmux session named after it. Give an agent an 'ssh' entry to have the watcher push to it over SSH.",
  "_example": {"ssh": "user@second-host", "session": "agent-name"}
}
JSONEOF
        _ok "  hosts.json written (edit it to declare remote agents)"
    fi

    _ok "bus installed to $MESH_HOME"
}

# ── Step 4: Generate api-tokens.json ─────────────────────────────────────────────

generate_tokens() {
    _info "Step 4: Generating api-tokens.json"

    local token_file="$MESH_HOME/api-tokens.json"

    if [[ -f "$token_file" ]] && ! $DRY_RUN; then
        _info "  api-tokens.json already exists — skipping (idempotent)"
        chmod 0600 "$token_file"
        return
    fi

    if $DRY_RUN; then
        _dry "Would generate api-tokens.json at $token_file"
        return
    fi

    python3 - <<PYEOF
import json, secrets
tokens = [{"name": "default", "token": secrets.token_hex(32)}]
with open("$token_file", "w") as f:
    json.dump(tokens, f, indent=2)
print(f"  Generated 1 token in $token_file")
PYEOF
    chmod 0600 "$token_file"
    _ok "api-tokens.json generated"
}

# ── Step 5: Create workspace dirs + render CLAUDE.md ─────────────────────────────

render_charters() {
    _info "Step 5: Creating workspace dirs and rendering charters"

    for name in "${!AGENT_NAMES[@]}"; do
        local workdir="${AGENT_WORKDIRS[$name]}"
        local charter="${AGENT_CHARTERS[$name]}"
        local model="${AGENT_MODELS[$name]}"
        local role="${AGENT_ROLES[$name]}"
        local host="${AGENT_HOSTS[$name]}"

        local workdir_expanded
        workdir_expanded=$(_expand_path "$workdir")

        if $DRY_RUN; then
            _dry "Would mkdir -p $workdir_expanded"
            _dry "Would render CLAUDE.md for $name from template $charter"
            continue
        fi

        mkdir -p "$workdir_expanded"

        # Resolve charter template
        local template_path=""
        if [[ "$charter" == /* || "$charter" == ~* ]]; then
            template_path=$(_expand_path "$charter")
        else
            template_path="$SCRIPT_DIR/templates/charter/${charter}.md"
        fi

        local claude_md="$workdir_expanded/CLAUDE.md"

        if [[ -f "$claude_md" ]]; then
            _info "  $name: CLAUDE.md already exists — not overwriting"
        else
            if [[ -f "$template_path" ]]; then
                sed \
                    -e "s|{{agent\.name}}|$(_sed_escape "$name")|g" \
                    -e "s|{{agent\.workdir}}|$(_sed_escape "$workdir_expanded")|g" \
                    -e "s|{{agent\.model}}|$(_sed_escape "$model")|g" \
                    -e "s|{{agent\.role}}|$(_sed_escape "$role")|g" \
                    -e "s|{{agent\.host}}|$(_sed_escape "$host")|g" \
                    "$template_path" > "$claude_md"
                chmod 0600 "$claude_md"
                _ok "  $name: CLAUDE.md rendered from $charter"
            else
                _warn "  $name: template $template_path not found — writing minimal CLAUDE.md"
                printf "# Charter — %s\n\nYou are **%s**. Working dir: \`%s\`.\n" \
                    "$name" "$name" "$workdir_expanded" > "$claude_md"
                chmod 0600 "$claude_md"
            fi
        fi
    done

    _ok "Workspace dirs and charters done"
}

# ── Step 6 + 7: Render and install systemd unit files ────────────────────────────

install_systemd() {
    _info "Step 6+7: Installing systemd unit files"

    if $NO_SYSTEMD; then
        _info "  --no-systemd set, skipping"
        return
    fi

    local unit_dir="$HOME/.config/systemd/user"
    local agent_template="$SCRIPT_DIR/templates/systemd/agent@.service"
    # The API code is in this checkout. It used to default to ~/mesh-api, a
    # directory nothing creates: the unit was installed and enabled, and then
    # failed at every start on a path that had never existed. Overridable for a
    # deployment that copies the API somewhere else.
    local mesh_api_base="${MESH_API_BASE:-$SCRIPT_DIR/mesh-api}"

    # Which interpreter runs it. A virtualenv under the API directory wins when
    # it is there; otherwise the system python3, which is what a reader who
    # followed `pip install -r requirements.txt` actually has. Writing the venv
    # path unconditionally produced units that could never start.
    local mesh_api_uvicorn mesh_api_python
    if [[ -x "$mesh_api_base/.venv/bin/uvicorn" ]]; then
        mesh_api_uvicorn="$mesh_api_base/.venv/bin/uvicorn"
        mesh_api_python="$mesh_api_base/.venv/bin/python3"
    else
        mesh_api_uvicorn="$(command -v python3) -m uvicorn"
        mesh_api_python="$(command -v python3)"
    fi

    if [[ ! -f "$agent_template" ]]; then
        _warn "  systemd template not found at $agent_template — skipping unit install"
        return
    fi

    if $DRY_RUN; then
        for name in "${!AGENT_NAMES[@]}"; do
            _dry "Would install $unit_dir/${name}.service"
        done
        for svc in tmux-server mesh-api ticket-dispatcher mesh-watcher; do
            _dry "Would install $unit_dir/${svc}.service"
        done
        _dry "Would run: systemctl --user daemon-reload && enable --now <services>"
        return
    fi

    mkdir -p "$unit_dir"

    # ── Agent services ────────────────────────────────────────────────────────────
    for name in "${!AGENT_NAMES[@]}"; do
        local workdir="${AGENT_WORKDIRS[$name]}"
        local model="${AGENT_MODELS[$name]}"
        local host="${AGENT_HOSTS[$name]}"

        # Only install local agents
        [[ "$host" != "primary" ]] && continue

        local workdir_expanded
        workdir_expanded=$(_expand_path "$workdir")

        local unit_path="$unit_dir/${name}.service"

        if [[ -f "$unit_path" ]]; then
            _info "  $name: unit file already exists — not overwriting"
            continue
        fi

        sed \
            -e "s|{{agent\.name}}|$name|g" \
            -e "s|{{agent\.workdir}}|$workdir_expanded|g" \
            -e "s|{{agent\.model}}|$model|g" \
            -e "s|{{mesh\.home}}|$MESH_HOME|g" \
            -e "s|{{mesh\.log_dir}}|$LOG_DIR|g" \
            -e "s|{{mesh_api_base}}|$mesh_api_base|g" \
            -e "s|{{mesh_api_uvicorn}}|$mesh_api_uvicorn|g" \
            -e "s|{{mesh_api_python}}|$mesh_api_python|g" \
            -e "s|{{mesh\.api_port}}|$API_PORT|g" \
            -e "s|{{mesh\.api_bind}}|$API_BIND|g" \
            "$agent_template" > "$unit_path"

        _ok "  $name: unit file written to $unit_path"
    done

    # ── Infrastructure services ───────────────────────────────────────────────────
    # tmux-server first: agent units depend on it (After=/Wants=). It carries no
    # {{...}} placeholders, so the sed pass below just copies it through verbatim.
    for svc in tmux-server mesh-api ticket-dispatcher mesh-watcher; do
        local svc_template="$SCRIPT_DIR/templates/systemd/${svc}.service"
        local unit_path="$unit_dir/${svc}.service"

        if [[ ! -f "$svc_template" ]]; then
            _warn "  $svc: template not found at $svc_template — skipping"
            continue
        fi

        if [[ -f "$unit_path" ]]; then
            _info "  $svc: unit file already exists — not overwriting"
            continue
        fi

        sed \
            -e "s|{{mesh\.home}}|$MESH_HOME|g" \
            -e "s|{{mesh\.log_dir}}|$LOG_DIR|g" \
            -e "s|{{mesh\.tmux}}|$(_sed_escape "$MESH_TMUX")|g" \
            -e "s|{{mesh_api_base}}|$mesh_api_base|g" \
            -e "s|{{mesh_api_uvicorn}}|$mesh_api_uvicorn|g" \
            -e "s|{{mesh_api_python}}|$mesh_api_python|g" \
            -e "s|{{mesh\.api_port}}|$API_PORT|g" \
            -e "s|{{mesh\.api_bind}}|$API_BIND|g" \
            "$svc_template" > "$unit_path"

        _ok "  $svc: unit file written to $unit_path"
    done

    systemctl --user daemon-reload
    _ok "daemon-reload done"

    # Enable agent services
    for name in "${!AGENT_NAMES[@]}"; do
        [[ "${AGENT_HOSTS[$name]}" != "primary" ]] && continue
        _unit_is_ours "${name}.service" "$unit_dir" || continue
        if systemctl --user is-enabled "${name}.service" &>/dev/null; then
            _info "  $name: already enabled"
        else
            systemctl --user enable --now "${name}.service" >> "$LOG_FILE" 2>&1 \
                && _ok "  $name: enabled and started" \
                || _warn "  $name: enable failed (check log)"
        fi
    done

    # Enable infrastructure services (tmux-server first so the shared server exists
    # before agents attach; agents also pull it in via Wants= regardless of order).
    for svc in tmux-server mesh-api ticket-dispatcher mesh-watcher; do
        [[ ! -f "$unit_dir/${svc}.service" ]] && continue
        _unit_is_ours "${svc}.service" "$unit_dir" || continue
        if systemctl --user is-enabled "${svc}.service" &>/dev/null; then
            _info "  $svc: already enabled"
        else
            systemctl --user enable --now "${svc}.service" >> "$LOG_FILE" 2>&1 \
                && _ok "  $svc: enabled and started" \
                || _warn "  $svc: enable failed (check log)"
        fi
    done
}

# ── Whose unit is systemd about to start? ────────────────────────────────────
#
# `systemctl --user` talks to the user manager over D-Bus, and that manager
# resolves unit names from ITS OWN environment — not from the $HOME of whoever
# invoked it. So `HOME=/tmp/sandbox bootstrap.sh` writes its units into the
# sandbox, where the manager never looks, and then `enable --now tmux-server`
# reaches the unit of the REAL deployment. Verified on 2026-09-03: a probe unit
# installed in the real ~/.config started fine when invoked with an isolated
# HOME.
#
# That is how a bootstrap run inside a test sandbox becomes an operation on the
# live mesh — and `tmux-server.service` stops by running `tmux kill-server`, so
# stopping the wrong one takes every agent session on the machine with it. On
# 2026-09-03 the whole fleet was down for 3h30 that way.
#
# Hence: before enabling anything, ask the manager which file it would run, and
# refuse if it is not the file we just wrote.
_unit_is_ours() {   # _unit_is_ours <unit-name> <unit-dir>
    local unit="$1" dir="$2" resolved
    resolved=$(systemctl --user show -p FragmentPath --value "$unit" 2>/dev/null)
    if [[ -z "$resolved" ]]; then
        _warn "  $unit: the user manager does not see this unit."
        _warn "      Written to $dir, which is not where it looks — running with an"
        _warn "      isolated HOME? Nothing was enabled."
        return 1
    fi
    if [[ "$resolved" != "$dir/$unit" ]]; then
        _warn "  $unit: REFUSED — the manager would run $resolved,"
        _warn "      not the file this run wrote ($dir/$unit). That unit belongs to"
        _warn "      another deployment; driving it would act on someone else's mesh."
        return 1
    fi
    return 0
}

# ── Step 8: Remote host deployment ───────────────────────────────────────────────

deploy_remote() {
    _info "Step 8: Remote host deployment"

    if $NO_SSH; then
        _info "  --no-ssh set, skipping"
        return
    fi

    # Parse remote hosts from config
    local remote_json
    remote_json=$(python3 - <<PYEOF
import sys, json
sys.path.insert(0, '$SCRIPT_DIR')
from config_schema.schema import load_config
cfg = load_config('$CONFIG_PATH')
print(json.dumps([{'name': h.name, 'user': h.user, 'host': h.host,
                   'ssh_key': h.ssh_key or '', 'wsl': h.wsl_distro or ''}
                  for h in cfg.hosts.remote]))
PYEOF
)

    if [[ "$remote_json" == "[]" ]]; then
        _info "  No remote hosts configured"
        return
    fi

    while IFS= read -r rhost; do
        local rname ruser rhost_addr rssh_key rwsl
        rname=$(echo "$rhost"     | jq -r '.name')
        ruser=$(echo "$rhost"     | jq -r '.user')
        rhost_addr=$(echo "$rhost"| jq -r '.host')
        rssh_key=$(echo "$rhost"  | jq -r '.ssh_key')
        rwsl=$(echo "$rhost"      | jq -r '.wsl')

        local -a ssh_opts=(-o ConnectTimeout=10 -o BatchMode=yes)
        [[ -n "$rssh_key" ]] && ssh_opts+=(-i "$rssh_key")
        local ssh_target="${ruser}@${rhost_addr}"

        if $DRY_RUN; then
            _dry "Would deploy remote agents for host $rname ($ssh_target)"
            continue
        fi

        _info "  Deploying to remote host $rname ($ssh_target)"

        # Check SSH connectivity
        if ! ssh "${ssh_opts[@]}" "$ssh_target" "echo ping" >/dev/null 2>&1; then
            _warn "  $rname: SSH unreachable, skipping remote deploy"
            continue
        fi

        # Deploy agents assigned to this host
        for name in "${!AGENT_NAMES[@]}"; do
            [[ "${AGENT_HOSTS[$name]}" != "$rname" ]] && continue

            local workdir="${AGENT_WORKDIRS[$name]}"
            # Expand ~ locally to get the absolute path to send to the remote
            local workdir_abs
            workdir_abs=$(_expand_path "$workdir")

            # Build the remote command (with optional WSL wrapper)
            local remote_mkdir
            if [[ -n "$rwsl" ]]; then
                remote_mkdir="wsl -d $(printf '%q' "$rwsl") -- mkdir -p '$(printf '%s' "$workdir_abs" | sed "s/'/'\\\\''/g")'"
            else
                remote_mkdir="mkdir -p '$(printf '%s' "$workdir_abs" | sed "s/'/'\\\\''/g")'"
            fi

            # Create workdir on remote
            ssh "${ssh_opts[@]}" "$ssh_target" "$remote_mkdir" \
                >> "$LOG_FILE" 2>&1 \
                && _ok "  $rname/$name: workdir created" \
                || _warn "  $rname/$name: workdir creation failed"
        done

    done < <(echo "$remote_json" | jq -c '.[]')

    _ok "Remote host deployment done"
}

# ── Step 9: Health checks ────────────────────────────────────────────────────────

run_health_checks() {
    _info "Step 9: Health checks"

    if $SKIP_HEALTH; then
        _info "  --skip-health set, skipping"
        return 0
    fi

    local failures=0

    # Check mesh home dir exists
    if [[ -d "$MESH_HOME" ]]; then
        _ok "  mesh home $MESH_HOME exists"
    else
        _warn "  mesh home $MESH_HOME not found"
        ((failures++)) || true
    fi

    # Check peers files and the bus itself. The bus is checked here because a
    # mesh whose watcher or send script is missing looks healthy — services
    # start, the API answers — and simply never delivers anything.
    for f in peers.py peers.sh send.py read.py roster.py watcher.sh mesh-send.sh; do
        if [[ -f "$MESH_HOME/$f" ]]; then
            _ok "  $MESH_HOME/$f present"
        else
            _warn "  $MESH_HOME/$f missing"
            ((failures++)) || true
        fi
    done

    # Check api-tokens.json
    if [[ -f "$MESH_HOME/api-tokens.json" ]]; then
        _ok "  api-tokens.json present"
    else
        _warn "  api-tokens.json missing"
        ((failures++)) || true
    fi

    # Check workspace dirs
    for name in "${!AGENT_NAMES[@]}"; do
        [[ "${AGENT_HOSTS[$name]}" != "primary" ]] && continue
        local workdir
        workdir=$(_expand_path "${AGENT_WORKDIRS[$name]}")
        if [[ -d "$workdir" ]]; then
            _ok "  workdir $workdir exists"
        else
            _warn "  workdir $workdir missing"
            ((failures++)) || true
        fi
    done

    # mesh-api health (only if systemd was used and not dry-run)
    if ! $NO_SYSTEMD && ! $DRY_RUN; then
        if curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then
            _ok "  mesh-api /health OK on :$API_PORT"
        else
            _warn "  mesh-api /health not responding on :$API_PORT (may not be configured yet)"
        fi
    fi

    if [[ $failures -gt 0 ]]; then
        _warn "Health checks: $failures warning(s)"
        return 2
    fi

    _ok "All health checks passed"
    return 0
}

# ── Main ─────────────────────────────────────────────────────────────────────────

main() {
    _info "=== bootstrap.sh starting ==="
    _info "Config: $CONFIG_PATH"
    _info "Dry-run: $DRY_RUN"
    _info "Log: $LOG_FILE"

    check_prerequisites
    validate_config
    generate_peers
    install_bus
    generate_tokens
    render_charters
    install_systemd
    deploy_remote
    run_health_checks
    local health_exit=$?

    _info "=== bootstrap.sh done ==="
    $DRY_RUN && _info "(dry-run: nothing was written)"
    echo ""
    echo "Log: $LOG_FILE"

    exit $health_exit
}

# Sourcing this file (for tests) must not run an installation.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
