#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s EXPORT_UID EXPORT_GID ABSOLUTE_SOURCE_PATH\n' "${0##*/}" >&2
    printf '%s\n' \
        'Read-only preflight for Ganesha mode-bit access; it never changes the source.' \
        'Run as root if needed to enumerate the complete tree.' >&2
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[[ $# -eq 3 ]] || { usage; exit 2; }

uid_text=$1
gid_text=$2
source_path=$3
[[ $uid_text =~ ^[0-9]+$ ]] || fail 'EXPORT_UID must be a nonzero integer.'
[[ $gid_text =~ ^[0-9]+$ ]] || fail 'EXPORT_GID must be a nonzero integer.'
[[ ${#uid_text} -le 10 && ${#gid_text} -le 10 ]] || fail 'UID and GID must not exceed 2147483647.'
uid=$((10#$uid_text))
gid=$((10#$gid_text))
(( uid > 0 && uid <= 2147483647 )) || fail 'EXPORT_UID must be from 1 to 2147483647.'
(( gid > 0 && gid <= 2147483647 )) || fail 'EXPORT_GID must be from 1 to 2147483647.'
[[ $source_path == /* ]] || fail 'source path must be absolute.'
[[ -d $source_path ]] || fail 'source path must exist and be a directory.'
source_path=$(realpath -e -- "$source_path") || fail 'could not resolve source path.'
[[ $source_path != / ]] || fail 'refusing to check the filesystem root.'

mode_bits() {
    local path=$1 mode owner group class
    read -r mode owner group < <(stat -c '%a %u %g' -- "$path") ||
        fail "could not read mode and ownership for $path"
    [[ $mode =~ ^[0-7]{3,4}$ && $owner =~ ^[0-9]+$ && $group =~ ^[0-9]+$ ]] ||
        fail "unsupported mode or ownership metadata for $path"
    mode=${mode: -3}
    if [[ $uid == "$owner" ]]; then
        class=${mode:0:1}
    elif [[ $gid == "$group" ]]; then
        class=${mode:1:1}
    else
        class=${mode:2:1}
    fi
    printf '%d\n' "$((8#$class))"
}

check_tree() {
    local path bits
    if ! find "$source_path" -print0 | while IFS= read -r -d '' path; do
        if [[ -L $path ]]; then
            printf 'Symlink requires manual permission review: %s\n' "$path" >&2
            exit 1
        elif [[ -d $path ]]; then
            bits=$(mode_bits "$path") || exit 1
            if (( (bits & 5) != 5 )); then
                printf 'UID:GID %s:%s lacks directory read/traverse mode bits on %s\n' \
                    "$uid" "$gid" "$path" >&2
                exit 1
            fi
            if (( (bits & 2) != 0 )); then
                printf 'UID:GID %s:%s has directory write mode bits on %s\n' \
                    "$uid" "$gid" "$path" >&2
                exit 1
            fi
        elif [[ -f $path ]]; then
            bits=$(mode_bits "$path") || exit 1
            if (( (bits & 4) != 4 )); then
                printf 'UID:GID %s:%s lacks file read mode bits on %s\n' \
                    "$uid" "$gid" "$path" >&2
                exit 1
            fi
            if (( (bits & 2) != 0 )); then
                printf 'UID:GID %s:%s has file write mode bits on %s\n' \
                    "$uid" "$gid" "$path" >&2
                exit 1
            fi
        fi
    done; then
        return 1
    fi
    return 0
}

if ! check_tree; then
    fail "UID:GID $uid:$gid does not meet the source-tree read-only mode-bit requirements."
fi

printf 'UID:GID %s:%s has read/traverse/list mode bits and no write mode bits throughout the source tree.\n' \
    "$uid" "$gid"
