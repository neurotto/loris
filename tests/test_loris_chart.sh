#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
CHART="$ROOT/charts/loris"
tests_passed=0

for tool in helm yq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'ERROR: %s is required; see charts/loris/README.md (Tests).\n' "$tool" >&2
        exit 1
    fi
done
if ! printf 'check: true\n' | yq --slurp --exit-status 'length == 1 and .[0].check == true' >/dev/null 2>&1; then
    printf 'ERROR: tests require jq-compatible yq (Debian/Ubuntu package yq), not the Go yq CLI.\n' >&2
    exit 1
fi

render_chart() {
    helm template loris "$CHART" --namespace test-central -f - <<< "$1"
}

assert_resources() {
    local name="$1"
    local predicate="$2"
    shift 2
    if ! yq --slurp --exit-status "[.[] | select(type == \"object\")] | $predicate" "$@" <<< "$rendered" >/dev/null; then
        printf 'FAIL: %s\n' "$name" >&2
        exit 1
    fi
    tests_passed=$((tests_passed + 1))
    printf 'PASS: %s\n' "$name"
}

workload_specs='[.[] | select(
    (.kind == "StatefulSet" and .metadata.name == "loris-mariadb") or
    (.kind == "Deployment" and .metadata.name == "loris")
) | .spec.template.spec]'

rendered=$(render_chart '{"enabled": true}')
assert_resources 'tag-only image remains supported for the app and all init containers' '
    [.[] | select(.kind == "Deployment" and .metadata.name == "loris") |
        .spec.template.spec | (.containers + .initContainers)[] | .image] |
    length == 5 and all(.[]; . == "docker.io/scilus/loris:29.0.0-chart0.5.0")'

digest="sha256:$(printf '%064d' 1)"
values=$(printf '{"enabled":true,"image":{"digest":"%s"},"loris":{"data":{"persistence":{"enabled":true}}},
    "sites":[{"name":"nandou","siteAlias":"NAND","enabled":true,"server":"192.0.2.1","bidsImport":{"enabled":true}}],
    "bidsImport":{"enabled":true}}' "$digest")
rendered=$(render_chart "$values")
assert_resources 'digest pins the app, bootstrap, discovery and ledger initializer consistently' '
    [.[] | select((.kind == "Deployment" and .metadata.name == "loris") or .kind == "CronJob") |
        (if .kind == "CronJob" then .spec.jobTemplate.spec else .spec end) |
        .template.spec | (.containers + (.initContainers // []))[] | .image] |
    length == 7 and all(.[]; . == $image)' --arg image "docker.io/scilus/loris:29.0.0-chart0.5.0@$digest"

if render_chart '{"enabled":true,"image":{"digest":"sha256:invalid"}}' >/dev/null 2>&1; then
    printf 'FAIL: invalid image digest must not render\n' >&2
    exit 1
fi
tests_passed=$((tests_passed + 1))
printf 'PASS: invalid image digest is rejected\n'

rendered=$(render_chart '{"enabled": true}')
assert_resources 'default scheduling has no cluster placement' "$workload_specs |
    length == 2 and all(.[]; (has(\"nodeSelector\") or has(\"affinity\") or has(\"tolerations\")) | not)"

values='{
    "enabled": true,
    "affinity": {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
        {"weight": 50, "preference": {"matchExpressions": [
            {"key": "example.org/pool", "operator": "In", "values": ["services"]}
        ]}}
    ]}}
}'
rendered=$(render_chart "$values")
assert_resources 'affinity is passed to all workloads' "$workload_specs |
    length == 2 and all(.[]; .affinity == \$expected.affinity)" --argjson expected "$values"

# Mirrors the scheduling and ingress values of the scilusnetes loris-central-portal application.
values='{
    "enabled": true,
    "nodeSelector": {"kubernetes.io/hostname": "dinf-artamie2"},
    "tolerations": [{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}],
    "ingress": {"enabled": true, "definitions": [{"name": "loris", "className": "nginx", "hosts": [
        {"host": "loris.scil.usherbrooke.ca", "paths": [{"path": "/", "pathType": "Prefix"}]}
    ]}]}
}'
rendered=$(render_chart "$values")
assert_resources 'application settings configure scheduling and dedicated-host ingress' "
    ($workload_specs | length == 2 and all(.[];
        .nodeSelector == \$expected.nodeSelector and .tolerations == \$expected.tolerations)) and
    ([.[] | select(.kind == \"Ingress\" and .metadata.name == \"loris\")] |
        length == 1 and all(.[];
            (.metadata.annotations | has(\"nginx.ingress.kubernetes.io/rewrite-target\") | not) and
            .spec.rules[0].host == \"loris.scil.usherbrooke.ca\" and
            (.spec.tls // []) == (\$expected.ingress.definitions[0].tls // []) and
            .spec.rules[0].http.paths[0].path == \"/\" and
            .spec.rules[0].http.paths[0].pathType == \"Prefix\"))" --argjson expected "$values"

database_spec='[.[] | select(.kind == "StatefulSet" and .metadata.name == "loris-mariadb") | .spec]'
rendered=$(render_chart '{"enabled": true}')
assert_resources 'full dev portal uses pod-local emptyDir initialization with no PVCs' '
    ([.[] | select(.kind == "PersistentVolumeClaim" or .kind == "Job")] | length == 0) and
    ([.[] | select(.kind == "Deployment" and .metadata.name == "loris") | .spec.template.spec] |
        length == 1 and all(.[];
            ([.volumes[] | select(.emptyDir != null)] | length == 2) and
            ([.initContainers[].name] == ["prepare-loris-state", "install-loris", "ensure-loris-jwt-key",
                "activate-loris-modules"]) and
            all(.initContainers[:2][]; [.volumeMounts[].name] == ["project-state", "loris-data"]) and
            all(.initContainers[2:][]; has("volumeMounts") | not) and
            ([.containers[0].volumeMounts[].name] == ["project-state", "loris-data"])))'

rendered=$(render_chart '{"enabled": true, "loris": {
    "project": {"persistence": {"enabled": true}},
    "data": {"persistence": {"enabled": true}}
}}')
assert_resources 'generated portal PVCs use cluster default class and share consumer wave' '
    ([.[] | select(.kind == "PersistentVolumeClaim")] |
        length == 2 and all(.[];
            (.spec | has("storageClassName") | not) and
            .metadata.annotations["argocd.argoproj.io/sync-wave"] == "2")) and
    ([.[] | select(.kind == "Deployment" and .metadata.name == "loris") | .spec.template.spec.volumes] |
        length == 1 and all(.[];
            .[0].persistentVolumeClaim.claimName == "loris-project" and
            .[1].persistentVolumeClaim.claimName == "loris-data"))'

rendered=$(render_chart '{"enabled": true, "loris": {
    "project": {"persistence": {"enabled": true, "existingClaim": "my-project"}},
    "data": {"persistence": {"enabled": true, "existingClaim": "my-data"}}
}}')
assert_resources 'portal existing claims do not generate replacement PVCs' '
    ([.[] | select(.kind == "PersistentVolumeClaim")] | length == 0) and
    ([.[] | select(.kind == "Deployment" and .metadata.name == "loris") | .spec.template.spec.volumes] |
        length == 1 and all(.[];
            .[0].persistentVolumeClaim.claimName == "my-project" and
            .[1].persistentVolumeClaim.claimName == "my-data"))'

rendered=$(render_chart '{"enabled": true, "loris": {
    "project": {"emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}},
    "data": {"persistence": {"enabled": true, "size": "2Gi", "storageClassName": "test-storage"}}
}}')
assert_resources 'project and data storage choices are independent' '
    ([.[] | select(.kind == "Deployment" and .metadata.name == "loris") | .spec.template.spec.volumes] |
        length == 1 and all(.[];
            .[0].emptyDir == {"medium": "Memory", "sizeLimit": "256Mi"} and
            .[1].persistentVolumeClaim.claimName == "loris-data")) and
    ([.[] | select(.kind == "PersistentVolumeClaim")] |
        length == 1 and all(.[];
            .metadata.name == "loris-data" and
            .spec.storageClassName == "test-storage" and
            .spec.resources.requests.storage == "2Gi"))'

rendered=$(render_chart '{"enabled": true}')
assert_resources 'database defaults to memory emptyDir' "$database_spec |
    length == 1 and all(.[];
        (has(\"volumeClaimTemplates\") | not) and
        ([.template.spec.volumes[] | select(.name == \"database\")] |
            length == 1 and all(.[]; .emptyDir == {\"medium\": \"Memory\", \"sizeLimit\": \"5Gi\"})))"

rendered=$(render_chart '{"enabled": true, "database": {"persistence": {"enabled": true}}}')
assert_resources 'database supports generated claims' "$database_spec |
    length == 1 and all(.[];
        (.template.spec | has(\"volumes\") | not) and
        .volumeClaimTemplates[0].metadata.name == \"database\" and
        .volumeClaimTemplates[0].spec.resources.requests.storage == \"5Gi\")"

rendered=$(render_chart '{"enabled": true, "database": {"persistence": {
    "enabled": true, "existingClaim": "precreated-database"
}}}')
assert_resources 'database supports existing claims' "$database_spec |
    length == 1 and all(.[];
        (has(\"volumeClaimTemplates\") | not) and
        ([.template.spec.volumes[] | select(.name == \"database\")] |
            length == 1 and all(.[]; .persistentVolumeClaim.claimName == \"precreated-database\")))"

rendered=$(render_chart '{"enabled": true, "ingress": {"enabled": true}}')
assert_resources 'root ingress defaults do not rewrite' '
    [.[] | select(.kind == "Ingress" and .metadata.name == "loris")] |
    length == 1 and all(.[];
        (.metadata | has("annotations") | not) and
        (.spec | has("tls") or has("ingressClassName") | not) and
        .spec.rules[0].http.paths[0].path == "/" and
        .spec.rules[0].http.paths[0].pathType == "Prefix")'

values='{"enabled": true, "ingress": {"enabled": true, "definitions": [
    {
        "name": "public", "className": "nginx",
        "annotations": {"cert-manager.io/cluster-issuer": "test-issuer"},
        "tls": [{"secretName": "test-tls", "hosts": ["loris.example.org"]}],
        "hosts": [{"host": "loris.example.org", "paths": [{"path": "/", "pathType": "Prefix"}]}]
    },
    {
        "name": "internal", "className": "internal",
        "hosts": [{"host": "loris.internal", "paths": [{"path": "/"}, {"path": "/api"}]}]
    }
]}}'
rendered=$(render_chart "$values")
assert_resources 'multiple ingresses have independent configuration' '
    ([.[] | select(.kind == "Ingress" and .metadata.name == "public")] |
        length == 1 and all(.[];
            .spec.tls == $expected.ingress.definitions[0].tls and
            .metadata.annotations == $expected.ingress.definitions[0].annotations)) and
    ([.[] | select(.kind == "Ingress" and .metadata.name == "internal")] |
        length == 1 and all(.[];
            .spec.ingressClassName == "internal" and
            (.metadata | has("annotations") | not) and
            (.spec | has("tls") | not) and
            (.spec.rules[0].http.paths | length == 2)))' --argjson expected "$values"

rendered=$(render_chart '{"enabled": true, "sites": [
    {"enabled": true, "name": "test", "server": "192.0.2.1", "nfsPort": 20490}
]}')
assert_resources 'site claim uses release namespace' '
    [.[] | select(.kind == "PersistentVolume" and .metadata.name == "loris-bids-test")] |
    length == 1 and all(.[];
        .spec.claimRef.namespace == "test-central" and
        (.spec.mountOptions | contains(["port=20490"])))'

values='{"enabled":true,"loris":{"data":{"persistence":{"enabled":true}}},
 "bidsImport":{"enabled":true,"suspend":true,"defaults":{"project":"Shared","projectAlias":"SHAR","recruitmentTarget":12}},
 "sites":[
  {"name":"nandou","siteAlias":"NAND","enabled":true,"server":"192.0.2.1","bidsImport":{"enabled":true,"suspend":false,
    "datasets":{"Dataset":{"recruitmentTarget":null,"createCandidates":false}}}},
  {"name":"second","siteAlias":"SCND","enabled":true,"server":"192.0.2.2","bidsImport":{"enabled":true,"schedule":"15 3 * * *"}},
  {"name":"mounted","enabled":true,"server":"192.0.2.3"},
  {"name":"disabled","enabled":false,"server":"192.0.2.4"}
 ]}'
rendered=$(render_chart "$values")
assert_resources 'each import-enabled site gets an independently scheduled discoverer, not immutable Jobs' '
    ([.[] | select(.kind == "Job")] | length == 0) and
    ([.[] | select(.kind == "CronJob")] | length == 2 and all(.[];
        .spec.concurrencyPolicy == "Forbid" and
        .spec.timeZone == "Etc/UTC" and
        .spec.jobTemplate.spec.backoffLimit == 0 and
        .spec.jobTemplate.spec.ttlSecondsAfterFinished == 604800 and
        .spec.jobTemplate.spec.template.spec.containers[0].volumeMounts[2].readOnly == true)) and
    ([.[] | select(.kind == "CronJob" and .metadata.name == "loris-bids-nandou")][0].spec |
        .schedule == "0 2 * * *" and .suspend == false) and
    ([.[] | select(.kind == "CronJob" and .metadata.name == "loris-bids-second")][0].spec |
        .schedule == "15 3 * * *" and .suspend == true)'

assert_resources 'runtime profiles preserve dataset overrides and exclude clone-specific helpers' '
    [.[] | select(.kind == "ConfigMap" and .metadata.name == "loris-bids-nandou-profile")][0].data |
    has("bids_runtime.py") and has("bids_ephys.py") and has("bids_previews.py") and
    has("bids-view.py") and has("bids-setup.py") and has("bids_migrate.py") and
    (has("bids_clone_migrate.py") | not) and
    (.["runtime.json"] | fromjson |
        .site.name == "nandou" and .site.siteAlias == "NAND" and
        .bidsImport.defaults.project == "Shared" and
        .site.bidsImport.datasets.Dataset.recruitmentTarget == null and
        .site.bidsImport.datasets.Dataset.createCandidates == false)'

assert_resources 'discovery has scoped API access, Secret references and narrow database access' '
    ([.[] | select(.kind == "ClusterRole" or .kind == "ClusterRoleBinding")] | length == 0) and
    ([.[] | select(.kind == "Role")] | length == 2 and all(.[];
        all(.rules[]; (.resources | index("secrets")) == null and (.verbs | index("*")) == null))) and
    ([.[] | select(.kind == "CronJob")] | all(.[];
        .spec.jobTemplate.spec.template.spec.automountServiceAccountToken == true and
        .spec.jobTemplate.spec.template.metadata.labels["app.kubernetes.io/name"] == "loris-bids" and
        ([.spec.jobTemplate.spec.template.spec.containers[0].env[] |
          select(.name == "LORIS_FRONTEND_ADMIN_PASSWORD")][0].valueFrom.secretKeyRef.key == "LORIS_FRONTEND_ADMIN_PASSWORD"))) and
    ([.[] | select(.kind == "NetworkPolicy")][0].spec.ingress[0].from |
        any(.[]; .podSelector.matchLabels["app.kubernetes.io/name"] == "loris-bids"))'

rendered=$(render_chart "$(printf '%s' "$values" | yq '.bidsImport.enabled = false')")
assert_resources 'global disabled gate leaves site mounts but creates no import resources' '
    ([.[] | select(.kind == "CronJob" or .kind == "Job" or .kind == "Role")] | length == 0) and
    ([.[] | select(.kind == "PersistentVolume")] | length == 3)'

if render_chart '{"enabled": true, "bidsImport": {"enabled": true},
    "sites": [{"name":"nandou","siteAlias":"NAND","enabled":true,"server":"192.0.2.1","bidsImport":{"enabled":true}}]}' >/dev/null 2>&1; then
    printf 'FAIL: import without shared persistent data must not render\n' >&2
    exit 1
fi
tests_passed=$((tests_passed + 1))
printf 'PASS: BIDS import requires shared persistent data\n'

for invalid in \
 '{"enabled":true,"bidsImport":{"site":"nandou"}}' \
 '{"enabled":true,"bidsImport":{"runId":"old"}}' \
 '{"enabled":true,"bidsImport":{"schedule":"invalid"}}' \
 '{"enabled":true,"bidsImport":{"enabled":"false"}}' \
 '{"enabled":true,"bidsImport":{"defaults":{"recruitmentTarget":0}}}' \
 '{"enabled":true,"sites":[{"name":"../escape"}]}' \
 '{"enabled":true,"sites":[{"name":"same"},{"name":"same"}]}' \
 '{"enabled":true,"sites":[{"name":"nandou","enabled":true,"bidsImport":{"enabled":true}}]}' \
 '{"enabled":true,"sites":[{"name":"nandou","bidsImport":{"recruitmentTarget":0}}]}' \
 '{"enabled":true,"sites":[{"name":"nandou","bidsImport":{"datasets":{"../escape":{}}}}]}' \
 '{"enabled":true,"sites":[{"name":"nandou","bidsImport":{"centre":"old"}}]}' ; do
    if render_chart "$invalid" >/dev/null 2>&1; then
        printf 'FAIL: invalid import configuration must not render: %s\n' "$invalid" >&2
        exit 1
    fi
    tests_passed=$((tests_passed + 1))
done
printf 'PASS: legacy keys and invalid site/dataset settings are rejected\n'

site_values='{"enabled":true,"loris":{"data":{"persistence":{"enabled":true}}},"bidsImport":{"enabled":true},
    "sites":[{"name":"simsite","siteAlias":"SIM","enabled":true,"server":"nfs.example.internal",
              "bidsImport":{"enabled":true,"project":"P","projectAlias":"P"}}]}'
rendered=$(render_chart "$site_values")
assert_resources 'NFS site mounts remain read-only in portal and import Pods' '
    ([.[] | select(.kind == "PersistentVolume" or .kind == "PersistentVolumeClaim") |
        .metadata.name] | index("loris-bids-simsite") != null) and
    ([.[] | select(.kind == "Deployment" and .metadata.name == "loris") |
        .spec.template.spec] | length == 1 and all(.[];
        .containers[0].volumeMounts | any(.mountPath == "/data/loris/data/assembly_bids/simsite" and
            .readOnly == true))) and
    ([.[] | select(.kind == "CronJob") | .spec.jobTemplate.spec.template.spec] |
        length == 1 and all(.[];
        .containers[0].volumeMounts | any(.mountPath == "/data/loris/data/assembly_bids/simsite" and
            .readOnly == true)))'

pvc_values='{"enabled":true,"loris":{"data":{"persistence":{"enabled":true}}},"bidsImport":{"enabled":true},
    "sites":[{"name":"simsite","siteAlias":"SIM","enabled":true,"source":"pvc","claimName":"loris-data",
              "subPath":"copies/simsite","bidsImport":{"enabled":true,"project":"P","projectAlias":"P",
              "symlinkedContent":"identity","updates":"additive"}}]}'
rendered=$(render_chart "$pvc_values")
assert_resources 'PVC site sources mount an existing claim read-only without creating NFS volumes' '
    ([.[] | select(.kind == "PersistentVolume" or .kind == "PersistentVolumeClaim") |
        select(.metadata.name == "loris-bids-simsite")] | length == 0) and
    ([(.[] | select(.kind == "Deployment" and .metadata.name == "loris") | .spec.template.spec),
      (.[] | select(.kind == "CronJob") | .spec.jobTemplate.spec.template.spec)] |
        length == 2 and all(.[];
        (.containers[0].volumeMounts | map(select(.mountPath == "/data/loris/data/assembly_bids/simsite")) |
            length == 1 and .[0].readOnly == true and .[0].subPath == "copies/simsite") as $mounted |
        (.volumes | any(.persistentVolumeClaim.claimName == "loris-data")) and $mounted))'

assert_resources 'site import options reach the runtime profile' '
    [.[] | select(.kind == "ConfigMap" and .metadata.name == "loris-bids-simsite-profile")][0].data["runtime.json"] |
    fromjson | .site.bidsImport.symlinkedContent == "identity" and .site.bidsImport.updates == "additive"'

fingerprint=$(printf '%064d' 0 | tr 0 a)
accepted=${pvc_values/\"updates\":\"additive\"/\"updates\":\"additive\",\"datasets\":\{\"dataset\":\{\"acceptFingerprint\":\"$fingerprint\"\}\}}
rendered=$(render_chart "$accepted")
assert_resources 'accepted fingerprints reach the runtime profile without entering settings validation' '
    [.[] | select(.kind == "ConfigMap" and .metadata.name == "loris-bids-simsite-profile")][0].data["runtime.json"] |
    fromjson | .site.bidsImport.datasets.dataset.acceptFingerprint == "'"$fingerprint"'"'

for invalid in "${pvc_values/\"identity\"/\"annex\"}" "${pvc_values/\"additive\"/\"replace\"}" \
        "${accepted/$fingerprint/ABC}" "${accepted/\"additive\"/\"reject\"}"; do
    if render_chart "$invalid" >/dev/null 2>&1; then
        printf 'FAIL: invalid site import option must not render: %s\n' "$invalid" >&2
        exit 1
    fi
    tests_passed=$((tests_passed + 1))
done

for invalid in \
 '{"enabled":true,"sites":[{"name":"simsite","enabled":true,"source":"rsync"}]}' \
 '{"enabled":true,"sites":[{"name":"simsite","enabled":true,"source":"pvc"}]}' \
 '{"enabled":true,"sites":[{"name":"simsite","enabled":true,"source":"pvc","claimName":"Bad_Claim"}]}' \
 '{"enabled":true,"sites":[{"name":"simsite","enabled":true,"source":"pvc","claimName":"c","subPath":"/abs"}]}' \
 '{"enabled":true,"sites":[{"name":"simsite","enabled":true,"source":"pvc","claimName":"c","subPath":"a/../b"}]}' \
 '{"enabled":true,"sites":[{"name":"simsite","enabled":true,"source":"pvc","claimName":"c","server":"192.0.2.1"}]}' \
 '{"enabled":true,"sites":[{"name":"simsite","enabled":true,"server":"192.0.2.1","claimName":"c"}]}' \
 '{"enabled":true,"sites":[{"name":"nandou","enabled":true}]}' \
 '{"enabled":true,"sites":[{"name":"simsite","enabled":true,"source":"clone"}]}' \
 '{"enabled":true,"siteClones":{"subPath":"legacy-clones"}}' \
 '{"enabled":true,"siteClones":{"cacheURL":"http://legacy-cache.example:8080"}}' ; do
    if render_chart "$invalid" >/dev/null 2>&1; then
        printf 'FAIL: invalid site source must not render: %s\n' "$invalid" >&2
        exit 1
    fi
    tests_passed=$((tests_passed + 1))
done
printf 'PASS: unsupported or inconsistent site sources and legacy clone settings are rejected\n'

loris_env='[.[] | select(.kind == "Deployment" and .metadata.name == "loris") | .spec.template.spec.containers[] |
    select(.name == "loris") | .env | map({(.name): .value}) | add]'
assert_resources 'without a provider the portal does not ask for remote content' "$loris_env |
    length == 1 and (.[0] | has(\"LORIS_CONTENT_PROVIDER_URL\") | not) and
    (.[0] | has(\"RE_MOVE_CACHE_URL\") | not) and (.[0] | has(\"RE_MOVE_CLONE_ROOT\") | not)"

provider_values=$(printf '%s' "$site_values" |
    yq -c '.contentProvider.url = "http://content-provider.example:8080"')
rendered=$(render_chart "$provider_values")
assert_resources 'provider URL is wired only to the portal and importer with generic paths' '
    ([.[] | select(.kind == "Deployment" and .metadata.name == "loris") |
        .spec.template.spec] | length == 1 and all(.[];
        (.containers[] | select(.name == "loris") | .env |
            map({(.name): .value}) | add) as $env |
        $env.LORIS_CONTENT_PROVIDER_URL == "http://content-provider.example:8080" and
        $env.LORIS_CONTENT_ROOT == "/data/loris/data/assembly_bids" and
        ($env | has("RE_MOVE_CACHE_URL") | not) and ($env | has("RE_MOVE_CLONE_ROOT") | not) and
        (.initContainers[0].command[2] | contains("/data-state/data/content-provider-chunks")))) and
    ([.[] | select(.kind == "CronJob") | .spec.jobTemplate.spec.template.spec] |
        length == 1 and all(.[];
        (.containers[] | select(.name == "bids-import") | .env |
            map({(.name): .value}) | add) as $env |
        $env.LORIS_CONTENT_PROVIDER_URL == "http://content-provider.example:8080" and
        $env.LORIS_CONTENT_ROOT == "/data/loris/data/assembly_bids" and
        ($env | has("RE_MOVE_CACHE_URL") | not) and ($env | has("RE_MOVE_CLONE_ROOT") | not)))'
for invalid in '"ftp://provider"' '"http://provider/v1"' '"http://provider:80 x"'; do
    if render_chart "{\"enabled\": true, \"contentProvider\": {\"url\": $invalid}}" >/dev/null 2>&1; then
        printf 'FAIL: invalid contentProvider.url %s must not render\n' "$invalid" >&2
        exit 1
    fi
done
tests_passed=$((tests_passed + 1))
printf 'PASS: provider URL validation and generic-only service wiring\n'

rendered=$(render_chart '{"enabled": true}')
assert_resources 'required LORIS modules are enforced by an unprivileged init container and guarded in the portal' '
    [.[] | select(.kind == "Deployment" and .metadata.name == "loris") | .spec.template.spec] |
        length == 1 and all(.[];
            .initContainers[-1].name == "activate-loris-modules" and
            (.initContainers[-1].env[] | select(.name == "LORIS_REQUIRED_MODULES") | .value | fromjson) ==
                ["api", "imaging_browser", "brainbrowser", "electrophysiology_browser"] and
            (.containers[] | select(.name == "loris") | .env[] | select(.name == "LORIS_REQUIRED_MODULES") |
                .value | fromjson) == ["api", "imaging_browser", "brainbrowser", "electrophysiology_browser"] and
            (.initContainers[-1].env[] | select(.name == "LORIS_DB_PASSWORD") | .valueFrom.secretKeyRef.key) ==
                "LORIS_DB_PASSWORD" and
            ([.initContainers[-1].env[].name] | index("MARIADB_ROOT_PASSWORD") == null) and
            .initContainers[-1].securityContext.runAsUser == 33 and
            .initContainers[-1].securityContext.allowPrivilegeEscalation == false)'

assert_resources 'non-compliant installer JWT keys are replaced by an unprivileged init container' '
    [.[] | select(.kind == "Deployment" and .metadata.name == "loris") | .spec.template.spec.initContainers[] |
        select(.name == "ensure-loris-jwt-key")] |
        length == 1 and all(.[];
            (.command[2] | contains("JWTKey") and contains("random_int")) and
            ([.env[].name] | index("MARIADB_ROOT_PASSWORD") == null) and
            .securityContext.runAsUser == 33 and
            .securityContext.allowPrivilegeEscalation == false)'

rendered=$(render_chart '{"enabled": true, "loris": {"additionalRequiredModules": ["dashboard", "api"]}}')
assert_resources 'additional required modules extend, but never replace, the built-in set' '
    [.[] | select(.kind == "Deployment" and .metadata.name == "loris") | .spec.template.spec |
        (.initContainers[-1].env[], (.containers[] | select(.name == "loris") | .env[])) |
        select(.name == "LORIS_REQUIRED_MODULES") | .value | fromjson] |
        length == 2 and all(.[]; . == ["api", "imaging_browser", "brainbrowser", "electrophysiology_browser", "dashboard"])'

for invalid in '"api"' '["api", "Bad;name"]' '[1]'; do
    if render_chart "{\"enabled\": true, \"loris\": {\"additionalRequiredModules\": $invalid}}" >/dev/null 2>&1; then
        printf 'FAIL: invalid loris.additionalRequiredModules %s must not render\n' "$invalid" >&2
        exit 1
    fi
done
tests_passed=$((tests_passed + 1))
printf 'PASS: invalid loris.additionalRequiredModules values are rejected\n'

rendered=$(render_chart '{"enabled":true,"loris":{"data":{"persistence":{"enabled":true}}},
    "bidsImport":{"enabled":true},"sites":[{"name":"nandou","siteAlias":"NAND","enabled":true,
    "server":"nfs.example.invalid","bidsImport":{"enabled":true,
    "datasets":{"sigmo":{"subjectPrefix":"nandousigmo"}}}}]}')
assert_resources 'dataset subject prefix reaches the frozen import profile' '
    [.[] | select(.kind == "ConfigMap" and .data["runtime.json"] != null) |
        .data["runtime.json"] | fromjson |
        .site.bidsImport.datasets.sigmo.subjectPrefix] == ["nandousigmo"]'

for invalid in '"001"' '"../escape"' 'false' '123'; do
    if render_chart "{\"enabled\":true,\"bidsImport\":{\"defaults\":{\"subjectPrefix\":$invalid}}}" >/dev/null 2>&1; then
        printf 'FAIL: invalid subjectPrefix %s must not render\n' "$invalid" >&2
        exit 1
    fi
done
tests_passed=$((tests_passed + 1))
printf 'PASS: invalid subject prefixes are rejected\n'

printf 'All %s LORIS chart tests passed.\n' "$tests_passed"
