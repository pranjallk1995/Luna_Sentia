#!/usr/bin/env bash
# Start, stop, or inspect Luna Sentia and the Luna apps that share its network.
#
#   ./luna.sh start [--watch] [--build | --no-pull] [APP...]   start Sentia, then the apps
#   ./luna.sh stop [APP...]                                    stop the apps, then Sentia
#   ./luna.sh status [APP...]                                  show each stack's containers
#
# start pulls each app's published images from GitHub Container Registry (ghcr.io)
# and recreates containers whose image changed, so it also updates; --build builds
# them from the local source instead, and --no-pull uses the images already here.
# APP is a short name such as visia or vita (default: visia vita lumia fiora lexia vibia). Sentia
# always starts first and must be healthy: the apps join its luna_sentia_network and keep
# their data in its PostgreSQL (luna-sentia-db) and sign people in through its auth service.
set -euo pipefail

# Runs from inside Luna_Sentia or from the folder that holds Luna_Sentia and
# the Luna_<App> folders; either way ROOT is that holding folder.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $(basename "$HERE") == Luna_Sentia ]]; then ROOT="$(dirname "$HERE")"; else ROOT="$HERE"; fi
SENTIA_DIR="$ROOT/Luna_Sentia"
# Override with e.g. LUNA_APPS="visia vita" ./luna.sh start
DEFAULT_APPS=(${LUNA_APPS:-visia vita lumia fiora lexia vibia})

usage() {
    sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

log() { printf '\033[1;35m[luna]\033[0m %s\n' "$*"; }

app_dir() { echo "$ROOT/Luna_${1^}"; }

# Resolve the requested apps (or the defaults) to existing Compose directories.
resolve_apps() {
    local name dir
    local names=("$@")
    ((${#names[@]})) || names=("${DEFAULT_APPS[@]}")
    APPS=()
    for name in "${names[@]}"; do
        name="${name,,}"
        name="${name#luna_}"
        [[ $name == sentia ]] && continue
        dir="$(app_dir "$name")"
        if [[ -f $dir/docker-compose.yaml ]]; then
            APPS+=("$name")
        else
            log "Skipping $name: no docker-compose.yaml in $dir"
        fi
    done
}

compose() {
    local dir="$1"
    shift
    docker compose --project-directory "$dir" -f "$dir/docker-compose.yaml" "$@"
}

# Where images come from: pull the published ones (default), build them, or use local ones.
IMAGES=pull

# Pull or build a stack's images (or the named services'), as IMAGES says. A failed
# pull (offline, or not published yet) is ignored: up then builds the missing image.
images() {
    local dir="$1"
    shift
    case "$IMAGES" in
        pull) compose "$dir" pull --quiet --ignore-pull-failures "$@" ;;
        build) compose "$dir" build "$@" ;;
    esac
}

start_sentia() {
    log "Starting Luna Sentia (PostgreSQL, Ollama, sign-on, Adminer, luna_sentia_network)"
    # Only Sentia's own images; Ollama and Adminer update when you pull them yourself.
    images "$SENTIA_DIR" postgres auth
    compose "$SENTIA_DIR" up -d --wait postgres ollama adminer
    # Every app's role and database must exist before any app starts.
    compose "$SENTIA_DIR" run --rm postgres-init
    # Every app signs people in through Sentia's sign-on service.
    compose "$SENTIA_DIR" up -d --wait auth
    # The model pull runs in the background; apps start without waiting for it.
    compose "$SENTIA_DIR" up -d ollama-pull
}

start() {
    local watch=false names=() app
    while (($#)); do
        case "$1" in
            --watch) watch=true ;;
            --build) IMAGES=build ;;
            --no-pull | --no-build) IMAGES=local ;;
            -h | --help) usage ;;
            -*) log "Unknown option: $1"; usage 1 ;;
            *) names+=("$1") ;;
        esac
        shift
    done
    resolve_apps "${names[@]}"

    start_sentia
    for app in "${APPS[@]}"; do
        log "Starting Luna ${app^}"
        images "$(app_dir "$app")"
        compose "$(app_dir "$app")" up -d --wait
    done

    if ! $watch; then
        log "All stacks are up. Run '$0 status' to inspect them."
        return
    fi
    watch_apps
}

# Run one Compose Watch per app in the background, with prefixed output, until Ctrl+C.
watch_apps() {
    local app
    WATCHERS=()
    trap stop_watchers INT TERM
    for app in "${APPS[@]}"; do
        (compose "$(app_dir "$app")" watch --no-up --quiet 2>&1 | sed -u "s/^/[${app}] /") &
        WATCHERS+=($!)
    done
    log "Watching ${APPS[*]} for source changes. Press Ctrl+C to stop watching."
    wait
}

stop_watchers() {
    local pid
    log "Stopping watchers; containers keep running."
    for pid in "${WATCHERS[@]}"; do pkill -TERM -P "$pid" 2>/dev/null || true; done
    wait
    exit 0
}

stop() {
    local app i
    local stop_sentia=false
    (($#)) || stop_sentia=true
    for app in "$@"; do [[ ${app,,} == sentia ]] && stop_sentia=true; done
    resolve_apps "$@"
    for ((i = ${#APPS[@]} - 1; i >= 0; i--)); do
        log "Stopping Luna ${APPS[i]^}"
        compose "$(app_dir "${APPS[i]}")" down
    done
    if $stop_sentia; then
        log "Stopping Luna Sentia"
        compose "$SENTIA_DIR" down
    fi
}

status() {
    local app
    resolve_apps "$@"
    for app in sentia "${APPS[@]}"; do
        log "Luna ${app^}"
        compose "$(app_dir "$app")" ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
    done
}

case "${1:-}" in
    start | stop | status) command="$1"; shift; "$command" "$@" ;;
    -h | --help | help | "") usage ;;
    *) log "Unknown command: $1"; usage 1 ;;
esac
