#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../../../../" && pwd)
chart="$script_dir"
applications="$repo_root/scilusnetes/re-move/applications.yml"
applications_rel=scilusnetes/re-move/applications.yml
chart_rel=scilusnetes/re-move/central/loris
application=loris-central-portal
namespace=re-move-central
secret=loris-credentials
context=${KUBE_CONTEXT:-}
public_url=

usage() {
    printf '%s\n' \
        'Usage: bash deploy-cluster.sh <preflight|central|ingress|status> [--context NAME] [--url HTTPS_URL]' \
        'preflight validates the chart and current GitOps values without cluster access.' \
        'central syncs the enabled portal with ingress disabled, then waits for health.' \
        'ingress syncs the separately enabled ingress and probes the public URL.' \
        'status reports the Argo CD application and central Kubernetes resources, and verifies required LORIS modules.' \
        'Kubernetes changes are applied by the Argo CD Helm application, not direct helm upgrade.'
}

fail() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

action=${1:-help}
shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --context)
            [[ $# -ge 2 && -n "$2" ]] || fail '--context requires a kubeconfig context name.'
            context=$2
            shift 2
            ;;
        --url)
            [[ $# -ge 2 && -n "$2" ]] || fail '--url requires an HTTPS URL.'
            public_url=$2
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *) fail "Unknown argument: $1" ;;
    esac
done

case "$action" in
    preflight|central|ingress|status) ;;
    help|--help|-h) usage; exit 0 ;;
    *) fail "Unknown action: $action" ;;
esac

for tool in helm yq git; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required."
done
if ! printf 'check: true\n' | yq --slurp --exit-status 'length == 1 and .[0].check == true' >/dev/null 2>&1; then
    fail 'Requires jq-compatible yq (Debian/Ubuntu yq), not the Go yq CLI.'
fi

app_count=$(yq -r '[.applications[] | select(.name == "loris-central-portal")] | length' "$applications")
[[ "$app_count" == 1 ]] || fail "Expected exactly one $application entry in applications.yml."

temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
values_file="$temporary/values.yml"
yq -y '.applications[] | select(.name == "loris-central-portal") |
    .sources[] | select(.path == "scilusnetes/re-move/central/loris") | .helm.valuesObject' \
    "$applications" > "$values_file"
enabled=$(yq -r '.enabled // false' "$values_file")
ingress_enabled=$(yq -r '.ingress.enabled // false' "$values_file")
configured_url=$(yq -r '.loris.publicUrl // ""' "$values_file")

helm lint "$chart" --values "$values_file" >/dev/null
helm template "$application" "$chart" --namespace "$namespace" --values "$values_file" \
    > "$temporary/rendered.yaml"
printf 'Chart render OK; enabled=%s ingress.enabled=%s\n' "$enabled" "$ingress_enabled"
required_modules=$(yq -r --slurp '[.[] | select(.kind? == "Deployment" and .metadata.name == "loris") |
    .spec.template.spec.containers[] | select(.name == "loris") |
    .env[] | select(.name == "LORIS_REQUIRED_MODULES") | .value | fromjson[]] | unique | join(" ")' \
    "$temporary/rendered.yaml")
[[ -n "$required_modules" ]] || fail 'Rendered portal has no LORIS_REQUIRED_MODULES.'
printf 'Required LORIS modules (other modules are left to Module Manager admins): %s\n' "$required_modules"

# Verify the required modules are active and the API route the viewers need answers.
# Module names were validated by the chart, so they are safe in the SQL list. Modules
# outside the required set are admin-owned and deliberately not checked.
verify_modules() {
    local names inactive api
    names=$(printf "'%s'," $required_modules)
    inactive=$(kubectl --context "$context" --namespace "$namespace" exec loris-mariadb-0 -- sh -ec '
        export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"
        exec mariadb --user=root --batch --skip-column-names loris -e "$1"' sh \
        "SELECT GROUP_CONCAT(Name) FROM modules WHERE Active <> 'Y' AND Name IN (${names%,});") ||
        fail 'Could not read LORIS module state from loris-mariadb-0.'
    [[ "$inactive" == NULL || -z "$inactive" ]] ||
        fail "Required LORIS modules are inactive: $inactive. Restart the portal (kubectl rollout restart deployment/loris) to re-enable them."
    printf 'Required LORIS modules active: %s\n' "$required_modules"
    api=$(kubectl --context "$context" --namespace "$namespace" exec deployment/loris -c loris -- \
        curl --silent --max-time 20 --output /dev/null --write-out '%{http_code} %{content_type}' \
        http://localhost/api/v0.0.3/candidates) || fail 'Could not probe the LORIS API inside the portal pod.'
    [[ "$api" == '401 application/json'* ]] ||
        fail "Unauthenticated LORIS API probe returned '$api', expected 401 JSON; thumbnails and BrainBrowser volumes will fail."
    printf 'LORIS API route OK (unauthenticated probe: %s)\n' "$api"
}

if [[ "$action" == preflight ]]; then
    printf 'No cluster resources were changed.\n'
    exit 0
fi

case "$action" in
    central)
        [[ "$enabled" == true ]] || fail 'Set enabled: true for loris-central-portal, then commit and push the GitOps change.'
        [[ "$ingress_enabled" == false ]] || fail 'Keep ingress.enabled false for the first central deployment.'
        ;;
    ingress)
        [[ "$enabled" == true ]] || fail 'The central LORIS application must be enabled before ingress.'
        [[ "$ingress_enabled" == true ]] || fail 'Set ingress.enabled: true, then commit and push the GitOps change.'
        ;;
esac

[[ -n "$context" ]] || fail 'Set KUBE_CONTEXT or pass --context with the intended cluster context.'
for tool in kubectl argocd jq; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required for $action."
done
kubectl config get-contexts -o name | grep -Fxq -- "$context" || fail "Kube context not found: $context"
kubectl --context "$context" cluster-info >/dev/null 2>&1 || fail "Cannot reach Kubernetes context: $context"

if [[ "$action" == central || "$action" == ingress ]]; then
    changed_sources=$(git -C "$repo_root" status --porcelain --untracked-files=all -- \
        "$applications_rel" "$chart_rel")
    [[ -z "$changed_sources" ]] || fail 'GitOps application/chart sources have uncommitted or untracked files; commit and push them before syncing.'
    kubectl --context "$context" --namespace "$namespace" get secret "$secret" -o name >/dev/null 2>&1 ||
        fail "Secret $namespace/$secret is missing; provision it with auth.sh before deployment. Secret data is never displayed."
fi

if [[ "$action" == status ]]; then
    argocd app get "$application" -o json > "$temporary/application.json" ||
        fail "Argo CD application $application is unavailable."
    jq -e --arg namespace "$namespace" \
        '.spec.destination.namespace == $namespace and any(.spec.sources[]?; .path == "scilusnetes/re-move/central/loris")' \
        "$temporary/application.json" >/dev/null || fail "Argo CD application does not target the expected LORIS chart/namespace."
    kubectl --context "$context" --namespace "$namespace" get deployment loris --ignore-not-found
    kubectl --context "$context" --namespace "$namespace" get statefulset loris-mariadb --ignore-not-found
    kubectl --context "$context" --namespace "$namespace" get service loris loris-mariadb --ignore-not-found
    kubectl --context "$context" --namespace "$namespace" get cronjobs,jobs -l app.kubernetes.io/name=loris-bids
    kubectl --context "$context" --namespace "$namespace" get ingress --ignore-not-found
    if [[ "$enabled" == true ]]; then
        verify_modules
    fi
    exit 0
fi

if [[ "$action" == central || "$action" == ingress ]]; then
    argocd app get "$application" -o json > "$temporary/application.json" ||
        fail "Argo CD application $application is unavailable; ensure the GitOps change is pushed and the ApplicationSet has refreshed."
    jq -e --arg namespace "$namespace" \
        '.spec.destination.namespace == $namespace and any(.spec.sources[]?; .path == "scilusnetes/re-move/central/loris")' \
        "$temporary/application.json" >/dev/null || fail "Argo CD application does not target the expected LORIS chart/namespace."

    argocd app sync "$application"
    argocd app wait "$application" --sync --health --timeout 900
    kubectl --context "$context" --namespace "$namespace" rollout status statefulset/loris-mariadb --timeout=300s
    kubectl --context "$context" --namespace "$namespace" rollout status deployment/loris --timeout=600s
    verify_modules
fi

if [[ "$action" == ingress ]]; then
    command -v curl >/dev/null 2>&1 || fail 'curl is required to verify ingress.'
    public_url=${public_url:-$configured_url}
    [[ "$public_url" == https://* ]] || fail 'Ingress verification requires an HTTPS URL (use --url to override the chart value).'
    public_url=${public_url%/}
    headers="$temporary/headers"
    if ! http_status=$(curl --silent --show-error --connect-timeout 5 --max-time 20 \
        --dump-header "$headers" --output /dev/null --write-out '%{http_code}' "$public_url/"); then
        fail "Ingress request failed for $public_url/"
    fi
    [[ "$http_status" =~ ^[23][0-9][0-9]$ ]] || fail "Ingress returned HTTP $http_status for $public_url/"
    location=$(awk 'BEGIN { IGNORECASE=1 } /^Location:/ { sub(/^[^:]*:[[:space:]]*/, ""); sub(/[\r\n]+$/, ""); value=$0 } END { print value }' "$headers")
    printf 'Ingress reachable: HTTP %s %s/\n' "$http_status" "$public_url"
    [[ -z "$location" ]] || printf 'Redirect location: %s\n' "$location"
    printf 'Confirm login, API docs, and schema loading in a browser before accepting sub-path routing.\n'
fi