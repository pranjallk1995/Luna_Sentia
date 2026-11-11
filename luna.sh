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
# Each docker-compose.yaml names the release it runs. --build tags its images :dev, so
# local code never takes a release's name; LUNA_IMAGE_TAG=dev ./luna.sh start --no-pull
# starts those builds again without rebuilding them.
# APP is a short name such as visia or vita. Without one, every app cloned beside Luna_Sentia
# (each Luna_<App> folder with a docker-compose.yaml) starts, Unia last. Sentia
# always starts first and must be healthy: the apps join its luna_sentia_network and keep
# their data in its PostgreSQL (luna-sentia-db) and sign people in through its auth service.
#
# A fresh clone needs no .env: every password, token and secret has a localhost default
# (lunasentia-dev, luna<app>-dev, ...), and without Google the owner signs in with one
# click on localhost. A stack whose address is public (https) must set its own secrets;
# start refuses it while it still uses a localhost default.
set -euo pipefail

# Runs from inside Luna_Sentia or from the folder that holds Luna_Sentia and
# the Luna_<App> folders; either way ROOT is that holding folder.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $(basename "$HERE") == Luna_Sentia ]]; then ROOT="$(dirname "$HERE")"; else ROOT="$HERE"; fi
SENTIA_DIR="$ROOT/Luna_Sentia"
# The usual start order; apps not listed here start after them, alphabetically, and
# Unia (whose home page links to the others) always starts last.
KNOWN_ORDER=(visia vita lumia fiora lexia vibia socia)

usage() {
    # The comment at the top of this file, up to its first line of code.
    awk 'NR > 1 && !/^#/ { exit } NR > 1' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

log() { printf '\033[1;35m[luna]\033[0m %s\n' "$*"; }

# An app's folder, whatever the case of its name (Luna_Visia, Luna_MyApp, ...).
app_dir() {
    local dir name
    for dir in "$ROOT"/Luna_*; do
        name="${dir##*/Luna_}"
        [[ ${name,,} == "${1,,}" ]] && { echo "$dir"; return; }
    done
    echo "$ROOT/Luna_${1^}"
}

# Every app cloned beside Luna_Sentia: each Luna_<App> folder with a docker-compose.yaml,
# in start order. LUNA_APPS="visia vita" overrides the list.
discover_apps() {
    local found=() ordered=() dir name known
    for dir in "$ROOT"/Luna_*/; do
        dir="${dir%/}"
        name="${dir##*/Luna_}"
        name="${name,,}"
        [[ $name != sentia && -f $dir/docker-compose.yaml ]] && found+=("$name")
    done
    for known in "${KNOWN_ORDER[@]}"; do
        [[ " ${found[*]} " == *" $known "* ]] && ordered+=("$known")
    done
    for name in $(printf '%s\n' "${found[@]}" | sort); do
        [[ $name == unia || " ${KNOWN_ORDER[*]} " == *" $name "* ]] || ordered+=("$name")
    done
    [[ " ${found[*]} " == *" unia "* ]] && ordered+=(unia)
    echo "${ordered[@]}"
}

# Resolve the requested apps (or the defaults) to existing Compose directories.
resolve_apps() {
    local name dir
    local names=("$@")
    ((${#names[@]})) || read -r -a names <<<"${LUNA_APPS:-$(discover_apps)}"
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

# Refuse a stack that is reachable from outside (an https address or a shared cookie
# domain) while it still uses a localhost default secret.
refuse_public_defaults() {
    local dir="$1" name="$2" resolved
    resolved="$(compose "$dir" config 2>/dev/null)" || return 0
    grep -qE '_PUBLIC_URL: https://|SENTIA_AUTH_COOKIE_DOMAIN: [^[:space:]"]+' <<<"$resolved" || return 0
    if grep -qE 'dev-token-for-localhost-only|lunasentia-dev' <<<"$resolved"; then
        log "Luna ${name^} has a public address but still uses a localhost default password or token."
        log "Set your own in $dir/.env (see .env.example) before starting it."
        return 1
    fi
}

start_sentia() {
    refuse_public_defaults "$SENTIA_DIR" sentia || exit 1
    log "Starting Luna Sentia (PostgreSQL, Ollama, ComfyUI, model monitor, sign-on, Adminer, luna_sentia_network)"
    # Only Sentia's own images; Ollama and Adminer update when you pull them yourself.
    images "$SENTIA_DIR" postgres auth monitor comfyui
    compose "$SENTIA_DIR" up -d --wait postgres ollama adminer
    # Every app's role and database must exist before any app starts.
    compose "$SENTIA_DIR" run --rm postgres-init
    # Every app signs people in through Sentia's sign-on service.
    compose "$SENTIA_DIR" up -d --wait auth
    # The apps reach Ollama through the monitor, so it must be up before any app.
    compose "$SENTIA_DIR" up -d --wait monitor
    # The image model; its first start does not hold up the apps.
    compose "$SENTIA_DIR" up -d comfyui
    # The model pulls run in the background; apps start without waiting for them.
    compose "$SENTIA_DIR" up -d ollama-pull comfyui-pull
}

start_vibia_speaker_helper() {
    local unit=luna-vibia-speakers.service
    if ! systemctl --user cat "$unit" >/dev/null 2>&1; then
        log "Vibia speaker helper is not installed; see Luna_Vibia/README.md"
        return
    fi
    log "Starting Luna Vibia speaker helper on the WSL host"
    systemctl --user start "$unit"
    systemctl --user is-active --quiet "$unit"
}

start() {
    local watch=false names=() app
    while (($#)); do
        case "$1" in
            --watch) watch=true ;;
            --build) IMAGES=build; export LUNA_IMAGE_TAG=dev ;;
            --no-pull | --no-build) IMAGES=local ;;
            -h | --help) usage ;;
            -*) log "Unknown option: $1"; usage 1 ;;
            *) names+=("$1") ;;
        esac
        shift
    done
    resolve_apps "${names[@]}"
    log "Apps to start after Sentia: ${APPS[*]:-none}"

    start_sentia
    # One app that fails to start does not hold up the others; they are named at the end.
    local failed=()
    for app in "${APPS[@]}"; do
        log "Starting Luna ${app^}"
        if [[ $app == vibia ]] && ! start_vibia_speaker_helper; then
            log "The speaker helper did not start; Vibia plays in the browser only"
        fi
        if ! { refuse_public_defaults "$(app_dir "$app")" "$app" && images "$(app_dir "$app")" \
               && compose "$(app_dir "$app")" up -d --wait; }; then
            log "Luna ${app^} did not start; see '$0 status ${app}' and its containers' logs"
            failed+=("$app")
        fi
    done

    if ((${#failed[@]})); then
        log "Started everything except: ${failed[*]}"
        exit 1
    fi
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
