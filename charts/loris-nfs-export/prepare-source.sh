#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf '%s\n' \
        'Usage: sudo bash prepare-source.sh [--apply] [--uid UID] [PATH]' \
        'Makes a site BIDS copy read-only and grants the LORIS exporter UID read/traverse access.' \
        'PATH defaults to /braindata/processing/re-move-site/Penthera_3T; UID defaults to 10001.' \
        'Without --apply, prints the planned changes and current access check only.' \
        'Ownership is unchanged. Requires setfacl/getfacl (acl package) and setpriv (util-linux).'
}

apply=false
uid=10001
path=/braindata/processing/re-move-site/Penthera_3T
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) apply=true; shift ;;
        --uid) don.\n' >&2; exit 1; }
path=$(realpath -e -- "$path")
[[ -d "$path" ]] || { printf 'ERROR: %s is not a directory.\n' "$path" >&2; exit 1; }
[[ "$(dirname -- "$path")" != / ]] || { printf 'ERROR: refusing to modify a top-level directory.\n' >&2; exit 1; }
for tool in setfacl getfacl setpriv find; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'ERROR: %s is required.\n' "$tool" >&2; exit 1; }
done

as_uid() {
    # cd / avoids failures from the caller's working directory being inaccessible to the exporter UID.
    setpriv --reuid="$uid" --regid="$uid" --clear-groups -- sh -c 'cd / && shift && exec "$@"' sh _ "$@"
}

check_access() {
    # Runs as the exporter UID with no supplementary groups, like the container.
    if as_uid sh -c 'test -r "$1" && test -x "$1" && find "$1" -mindepth 1 -maxdepth 1 -print -quit >/dev/null' sh "$path"; then
        printf 'UID %s: can read, traverse, and list %s\n' "$uid" "$path"
        return 0
    fi
    printf 'UID %s: cannot read/traverse/list %s\n' "$uid" "$path"
    return 1
}

check_write_denied() {
    local probe="$path/.loris-write-probe.$$"
    as_uid true || { printf 'Cannot switch to UID %s; write check is inconclusive.\n' "$uid"; return 1; }
    if as_uid sh -c ': > "$1"' sh "$probe" 2>/dev/null; then
        rm -f -- "$probe"
        printf 'UID %s: write was ALLOWED (unexpected)\n' "$uid"
        return 1
    fi
    printf 'UID %s: write denied, as expected\n' "$uid"
}

ancestors=()
dir=$(dirname -- "$path")
while [[ "$dir" != / ]]; do
    ancestors=("$dir" "${ancestors[@]}")
    dir=$(dirname -- "$dir")
done

# The group-class bits act as the mask once a named entry exists, so a missing x there blocks traversal.
needs_mask_x() {
    local class
    class=$(getfacl -cp -- "$1" 2>/dev/null | awk -F: '$1 == "mask" {m = $3} $1 == "group" && $2 == "" {g = $3} END {print (m != "" ? m : g)}')
    [[ "$class" != *x* ]]
}

if [[ "$apply" != true ]]; then
    [[ $EUID -eq 0 ]] || { printf 'ERROR: run as root (sudo); the access check impersonates UID %s.\n' "$uid" >&2; exit 1; }
    printf 'DRY RUN for %s (UID %s). Planned changes with --apply:\n' "$path" "$uid"
    printf '  1. Remove write permission for owner and others on the whole tree (chmod -R u-w,o-w).\n'
    printf '  2. Ensure group-class read/traverse (chmod -R g+rX); existing ACL entries and their write rights are kept.\n'
    printf '  3. Grant user:%s r-X recursively without changing ACL masks.\n' "$uid"
    printf '  4. Grant user:%s --x (traverse only) on ancestors:\n' "$uid"
    printf '       %s\n' "${ancestors[@]}"
    for dir in "${ancestors[@]}"; do
        if needs_mask_x "$dir"; then
            printf '     %s has no x in its ACL mask/group class; x will be added (group %s gains traverse-only, not listing).\n' \
                "$dir" "$(stat -c %G -- "$dir")"
        fi
    done
    printf '\nCurrent state:\n'
    check_access || true
    exit 0
fi

[[ $EUID -eq 0 ]] || { printf 'ERROR: --apply must run as root (sudo).\n' >&2; exit 1; }
printf 'Making %s read-only for owner and others...\n' "$path"
# With ACLs, g+rX only adds bits to the mask, so named entries keep their rights and groups keep read.
chmod -R u-w,o-w,g+rX -- "$path"
setfacl -R -n -m "u:${uid}:rX" -- "$path"
for dir in "${ancestors[@]}"; do
    add_x=false
    needs_mask_x "$dir" && add_x=true
    # -n keeps the existing mask so other named ACL users don't gain effective rights.
    setfacl -n -m "u:${uid}:x" -- "$dir"
    if [[ "$add_x" == true ]]; then
        chmod g+x -- "$dir"
        printf 'Added x to the ACL mask of %s (group %s gains traverse-only).\n' "$dir" "$(stat -c %G -- "$dir")"
    fi
done

printf '\nVerification:\n'
status=0
check_access || status=1
check_write_denied || status=1
getfacl -p -- "$path" 2>/dev/null | grep -E "^(user:|group:|mask::|other::)"
exit "$status"
