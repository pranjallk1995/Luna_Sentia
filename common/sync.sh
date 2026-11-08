#!/usr/bin/env bash
# Keep the files every Luna app shares identical. The copies in this folder are the
# originals: change them here, run this, and commit each app.
#
#   common/sync.sh                 copy them into every Luna_<App> beside Luna_Sentia
#   common/sync.sh --check DIR     report where one app's copies differ (used by CI)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# original -> where an app keeps it. An app without that path does not use the file.
FILES=(
    "luna_mcp.py:mcp/app/transport/luna_mcp.py"
    "tenancy.py:mcp/app/core/tenancy.py"
    "sso.py:ui/sso.py"
)

# Luna Unia's sign-in differs on purpose: it has no backend to pass the person to.
skipped() { [[ $(basename "$1") == Luna_Unia && $2 == sso.py ]] || [[ -n ${GITHUB_REPOSITORY:-} && $GITHUB_REPOSITORY == */Luna_Unia && $2 == sso.py ]]; }

check() {
    local app="$1" entry original target different=0
    for entry in "${FILES[@]}"; do
        original="${entry%%:*}" target="$app/${entry#*:}"
        [[ -f $target ]] && ! skipped "$app" "$original" || continue
        if ! cmp -s "$HERE/$original" "$target"; then
            echo "::warning file=${entry#*:}::Differs from Luna_Sentia/common/$original; run common/sync.sh"
            different=1
        fi
    done
    return "$different"
}

sync_all() {
    local app entry original target
    for app in "$(dirname "$(dirname "$HERE")")"/Luna_*/; do
        app="${app%/}"
        for entry in "${FILES[@]}"; do
            original="${entry%%:*}" target="$app/${entry#*:}"
            [[ -f $target ]] && ! skipped "$app" "$original" || continue
            cmp -s "$HERE/$original" "$target" || { cp "$HERE/$original" "$target"; echo "updated $target"; }
        done
    done
}

if [[ ${1:-} == --check ]]; then check "${2:?--check needs an app folder}"; else sync_all; fi
