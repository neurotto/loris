#!/usr/bin/env bash
set -euo pipefail

chart_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work_dir="$chart_dir/tests/.source-fixture.$$"
source_path="$work_dir/source"
mkdir -- "$work_dir"
trap 'chmod -R u+rwX -- "$work_dir" 2>/dev/null || true; rm -rf -- "$work_dir"' EXIT

uid=$(id -u)
gid=$(id -g)
if (( uid == 2147483647 )); then
    uid=2147483646
else
    uid=$((uid + 1))
fi

run_preflight() {
    bash "$chart_dir/prepare-source.sh" "$uid" "$gid" "$source_path"
}

mkdir -p -- "$source_path/nested"
printf 'fixture data\n' >"$source_path/nested/data.txt"
chmod 0750 "$source_path" "$source_path/nested"
chmod 0640 "$source_path/nested/data.txt"

output="$work_dir/output"
run_preflight >"$output"
grep -Fq "has read/traverse/list mode bits and no write mode bits" "$output"
[[ $(stat -c '%a' "$source_path") == 750 ]]
[[ $(stat -c '%a' "$source_path/nested") == 750 ]]
[[ $(stat -c '%a' "$source_path/nested/data.txt") == 640 ]]
[[ $(cat "$source_path/nested/data.txt") == 'fixture data' ]]

chmod g+w "$source_path"
if run_preflight >"$output" 2>&1; then
    printf 'Expected writable fixture to fail the preflight.\n' >&2
    exit 1
fi
grep -Fq 'has directory write mode bits' "$output"
chmod g-w "$source_path"

chmod g+w "$source_path/nested/data.txt"
if run_preflight >"$output" 2>&1; then
    printf 'Expected writable fixture file to fail the preflight.\n' >&2
    exit 1
fi
grep -Fq 'has file write mode bits' "$output"
chmod g-w "$source_path/nested/data.txt"

chmod g-rx "$source_path/nested"
if run_preflight >"$output" 2>&1; then
    printf 'Expected inaccessible fixture directory to fail the preflight.\n' >&2
    exit 1
fi
grep -Fq 'lacks directory read/traverse mode bits' "$output"

printf 'Source permission fixture tests passed.\n'
