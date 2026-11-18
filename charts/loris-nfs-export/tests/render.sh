#!/usr/bin/env bash
set -euo pipefail

chart_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work_dir="$chart_dir/tests/.render-fixture.$$"
mkdir -- "$work_dir"
trap 'rm -rf -- "$work_dir"' EXIT

expect_failure() {
    local expected=$1
    shift
    if helm template test "$chart_dir" "$@" >"$work_dir/output" 2>&1; then
        printf 'Expected Helm rendering to fail: %s\n' "$expected" >&2
        exit 1
    fi
    if ! grep -Fq -- "$expected" "$work_dir/output"; then
        printf 'Expected error "%s"; got:\n' "$expected" >&2
        cat "$work_dir/output" >&2
        exit 1
    fi
}

base_values=(
    --set siteName=example-site
    --set-string nodeHostname=export-node
    --set-string dataPath=/srv/approved-bids
    --set-string 'allowedClientCIDRs[0]=192.0.2.10/32'
    --set-string 'allowedClientCIDRs[1]=2001:db8::/32'
    --set-string 'allowedClientCIDRs[2]=::ffff:192.0.2.10/128'
    --set nfsPort=20490
    --set exportUid=10001
    --set exportGid=2000
)

helm template test "$chart_dir" >"$work_dir/output"
if grep -Eq '^[[:space:]]*(apiVersion|kind):' "$work_dir/output"; then
    printf 'Default values must render no resources.\n' >&2
    exit 1
fi

helm template test "$chart_dir" "${base_values[@]}" >"$work_dir/output"
for expected in \
    'type: Recreate' \
    'hostPort: 20490' \
    'readOnly: true' \
    'runAsUser: 0' \
    'allowPrivilegeEscalation: false' \
    'readOnlyRootFilesystem: true' \
    'drop: [ALL]' \
    'add: [DAC_READ_SEARCH, SETUID, SETGID]' \
    "stat -c '%a %u %g' /export" \
    'value: "192.0.2.10/32, 2001:db8::/32, ::ffff:192.0.2.10/128"'; do
    grep -Fq -- "$expected" "$work_dir/output" || {
        printf 'Rendered exporter is missing expected setting: %s\n' "$expected" >&2
        exit 1
    }
done
if grep -Eq '^[[:space:]]*hostNetwork:' "$work_dir/output"; then
    printf 'Exporter must not enable hostNetwork.\n' >&2
    exit 1
fi
grep -Fq 'Squash = all_squash;' "$chart_dir/image/entrypoint.sh"
grep -Fq 'Access_Type = RO;' "$chart_dir/image/entrypoint.sh"

expect_failure 'both dataPath and allowedClientCIDRs' \
    --set-string dataPath=/srv/approved-bids
expect_failure 'lowercase DNS label' "${base_values[@]}" --set siteName=bad_name
expect_failure 'nodeHostname must be a nonempty' "${base_values[@]}" --set-string nodeHostname=
expect_failure 'absolute, non-root path' "${base_values[@]}" --set-string dataPath=relative/path
expect_failure 'absolute, non-root path' "${base_values[@]}" --set-string dataPath=/
expect_failure 'absolute, non-root path' "${base_values[@]}" --set-string dataPath=/srv/../data
expect_failure 'valid IPv4 or IPv6 CIDR' "${base_values[@]}" --set-string allowedClientCIDRs[0]=256.0.2.10/32
expect_failure 'valid IPv4 or IPv6 CIDR' "${base_values[@]}" --set-string allowedClientCIDRs[0]=2001:::1/64
expect_failure 'prefix length exceeds' "${base_values[@]}" --set-string allowedClientCIDRs[0]=2001:db8::/129
expect_failure 'nfsPort must be an integer' "${base_values[@]}" --set nfsPort=1023
expect_failure 'nfsPort must be an integer' "${base_values[@]}" --set nfsPort=20490.5
expect_failure 'exportUid must be a nonzero integer' "${base_values[@]}" --set exportUid=0
expect_failure 'exportUid must be a nonzero integer' "${base_values[@]}" --set exportUid=10001.5
expect_failure 'exportGid must be a nonzero integer' "${base_values[@]}" --set-string exportGid=
expect_failure 'exportGid must be a nonzero integer' "${base_values[@]}" --set exportGid=2147483648
expect_failure 'exportGid must be a nonzero integer' "${base_values[@]}" --set exportGid=2000.5
expect_failure 'allowedClientCIDRs must be a list' "${base_values[@]}" --set-string allowedClientCIDRs=not-a-list

printf 'NFS exporter Helm render tests passed.\n'
