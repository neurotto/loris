#!/usr/bin/env bash
set -euo pipefail
umask 077

for tool in curl yq jq; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'Missing prerequisite: %s\n' "$tool" >&2; exit 1; }
done
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/scilusnetes/loris-local"
credentials="${LORIS_TEST_CREDENTIALS:-$state_dir/credentials.env}"
port=8080
if [[ -f "$state_dir/port" ]]; then
    port=$(< "$state_dir/port")
fi
url="${LORIS_TEST_URL:-http://localhost:$port}"
url=${url%/}
admin_password=
while IFS='=' read -r key value; do
    if [[ "$key" == LORIS_FRONTEND_ADMIN_PASSWORD ]]; then
        admin_password=$value
    fi
done < "$credentials"
unset value
[[ -n "$admin_password" ]] || { printf 'No local admin password found.\n' >&2; exit 1; }

temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
printf '%s' "$admin_password" | jq -Rs '{login: "true", command: "login", username: "admin", password: .}' |
    curl -fsS -c "$temporary/cookies" -H 'Content-Type: application/json' --data-binary @- \
        -o /dev/null "$url/login/Authentication"
unset admin_password

modules=$(curl -fsS -b "$temporary/cookies" "$url/module_manager/?format=json")
for module in imaging_browser brainbrowser imaging_qc; do
    active=$(yq -er --arg name "$module" '.Data[] | select(.[0] == $name) | .[2]' <<< "$modules")
    [[ "$active" == Y || "$active" == N ]] || { printf 'Invalid state for %s.\n' "$module" >&2; exit 1; }
    status=$(printf '{"Active":"%s"}' "$active" |
        curl -sS -b "$temporary/cookies" -X PATCH --data-binary @- \
            -o "$temporary/body" -w '%{http_code}' "$url/module_manager/modules/$module")
    if [[ "$status" != 205 || -s "$temporary/body" ]]; then
        printf 'FAIL: %s returned HTTP %s with %s body bytes; expected empty HTTP 205.\n' \
            "$module" "$status" "$(wc -c < "$temporary/body")" >&2
        exit 1
    fi
    printf 'PASS: %s returns empty HTTP 205 (state %s preserved).\n' "$module" "$active"
done

after=$(curl -fsS -b "$temporary/cookies" "$url/module_manager/?format=json")
yq -e --argjson before "$modules" '.Data == $before.Data' <<< "$after" >/dev/null
status=$(curl -sS -X PATCH --data-binary '{"Active":"N"}' -o /dev/null -w '%{http_code}' \
    "$url/module_manager/modules/imaging_qc")
[[ "$status" == 403 ]] || { printf 'FAIL: anonymous PATCH returned HTTP %s; expected 403.\n' "$status" >&2; exit 1; }
printf 'PASS: unauthenticated module edits remain forbidden.\n'