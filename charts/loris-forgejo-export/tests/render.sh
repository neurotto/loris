#!/usr/bin/env bash
# Helm render tests for loris-forgejo-export (helm template only; needs helm and python3 with PyYAML).
set -euo pipefail

chart_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work_dir="$chart_dir/tests/.render-fixture.$$"
mkdir -- "$work_dir"
trap 'rm -rf -- "$work_dir"' EXIT

python3 -c 'import yaml' 2>/dev/null || {
    printf 'render.sh needs python3 with PyYAML.\n' >&2
    exit 1
}

render() {
    local out=$1
    shift
    helm template test "$chart_dir" --namespace loris "$@" >"$work_dir/$out"
}

expect_failure() {
    local expected=$1
    shift
    if helm template test "$chart_dir" --namespace loris "$@" >"$work_dir/output" 2>&1; then
        printf 'Expected Helm rendering to fail: %s\n' "$expected" >&2
        exit 1
    fi
    if ! grep -Fq -- "$expected" "$work_dir/output"; then
        printf 'Expected error "%s"; got:\n' "$expected" >&2
        cat "$work_dir/output" >&2
        exit 1
    fi
}

# Runs Python assertions against a rendered file. The script sees `docs` (all
# objects), `by(kind, name)`, `pod_spec(obj)`, `env(container)` and `values`.
check() {
    local file=$1 script=$2
    python3 - "$work_dir/$file" "$work_dir/base.json" <<EOF
import json, sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
values = json.load(open(sys.argv[2]))
def by(kind, name):
    found = [d for d in docs if d['kind'] == kind and d['metadata']['name'] == name]
    assert len(found) == 1, f'expected one {kind} {name}, found {len(found)}'
    return found[0]
def kinds():
    return sorted((d['kind'], d['metadata']['name']) for d in docs)
def pod_spec(obj):
    spec = obj['spec']
    if obj['kind'] == 'CronJob':
        spec = spec['jobTemplate']['spec']
    return spec['template']['spec']
def pod_annotations(obj):
    spec = obj['spec']
    if obj['kind'] == 'CronJob':
        spec = spec['jobTemplate']['spec']
    return spec['template']['metadata'].get('annotations', {})
def env(container):
    return {e['name']: e.get('value', e.get('valueFrom')) for e in container.get('env', [])}
def rules(role):
    return sorted((tuple(r.get('apiGroups', [])), tuple(r['resources']), tuple(r['verbs']),
                   tuple(r.get('resourceNames', ()))) for r in role['rules'])
$script
EOF
}

# Writes values variant <name>.json: base values mutated by a Python statement on `v`.
variant() {
    local name=$1 mutation=$2
    python3 - "$work_dir/base.json" "$work_dir/$name.json" <<EOF
import json, sys
v = json.load(open(sys.argv[1]))
$mutation
json.dump(v, open(sys.argv[2], 'w'))
EOF
}

cat >"$work_dir/base.json" <<'EOF'
{
  "forgejo": {
    "url": "http://forgejo-http.forgejo.svc.cluster.local:3000",
    "publicUrl": "https://forge.example.org",
    "publicIP": "192.0.2.80",
    "adminSecret": {"name": "forgejo-admin"}
  },
  "bidsImport": {"enabled": true},
  "sites": [
    {"name": "nfs-site", "enabled": true, "source": "nfs", "server": "192.0.2.1", "nfsPort": 20490,
     "bidsImport": {"enabled": true}},
    {"name": "alpha", "enabled": true, "source": "pvc", "claimName": "loris-data",
     "subPath": "forgejo/clones/alpha",
     "bidsImport": {"enabled": true, "symlinkedContent": "identity", "updates": "additive"},
     "forgejo": {"enabled": true, "datasets": ["Study_A", "Study.B"], "visibility": {"Study_A": "signed-in"}}},
    {"name": "beta", "source": "pvc", "claimName": "loris-data", "subPath": "forgejo/clones/beta",
     "forgejo": {"enabled": true, "datasets": ["Study_C"]}}
  ]
}
EOF
base=(-f "$work_dir/base.json")
fullname=test-loris-forgejo-export

# Default values and sites without forgejo.enabled render nothing.
render output
if grep -Eq '^[[:space:]]*(apiVersion|kind):' "$work_dir/output"; then
    printf 'Default values must render no resources.\n' >&2
    exit 1
fi
variant nfs-only 'v["sites"] = v["sites"][:1]; v["forgejo"] = {}'
render output -f "$work_dir/nfs-only.json"
if grep -Eq '^[[:space:]]*(apiVersion|kind):' "$work_dir/output"; then
    printf 'Values without Forgejo sites must render no resources.\n' >&2
    exit 1
fi

helm lint --strict "$chart_dir" "${base[@]}" >"$work_dir/lint" || { cat "$work_dir/lint" >&2; exit 1; }

render webhook.yaml "${base[@]}"
check webhook.yaml "
F = '$fullname'
expected = sorted([
    ('ConfigMap', F + '-scripts'), ('ConfigMap', F + '-registry'), ('ConfigMap', F + '-worker-template'),
    ('Secret', F + '-webhook'),
    ('ServiceAccount', F + '-reconciler'), ('ServiceAccount', F + '-sync'), ('ServiceAccount', F + '-webhook'),
    ('Role', F + '-reconciler'), ('Role', F + '-sync'), ('Role', F + '-webhook'),
    ('RoleBinding', F + '-reconciler'), ('RoleBinding', F + '-sync'), ('RoleBinding', F + '-webhook'),
    ('CronJob', F + '-reconcile'), ('Job', F + '-reconcile-1'), ('CronJob', F + '-sync'),
    ('Deployment', F + '-provider'), ('Service', F + '-provider'), ('NetworkPolicy', F + '-provider'),
    ('Deployment', F + '-webhook'), ('Service', F + '-webhook'), ('NetworkPolicy', F + '-webhook'),
])
assert kinds() == expected, kinds()

# Every object is labelled and namespaced by the release; names fit Kubernetes limits.
for d in docs:
    labels = d['metadata']['labels']
    assert labels['app.kubernetes.io/name'] == 'loris-forgejo-export', d['metadata']
    assert labels['app.kubernetes.io/instance'] == 'test', d['metadata']
    assert 'namespace' not in d['metadata'], d['metadata']
    assert len(d['metadata']['name']) <= (52 if d['kind'] == 'CronJob' else 63), d['metadata']['name']
    assert 'nfs-site' not in json.dumps(d) or d['metadata']['name'] == F + '-registry', d['metadata']['name']

# Least-privilege RBAC: exact rules, no wildcards, no cluster-scoped objects.
assert rules(by('Role', F + '-reconciler')) == sorted([
    (('',), ('secrets',), ('create',), ()),
    (('',), ('secrets',), ('get', 'update', 'patch'),
     (F + '-bot', F + '-enrollment-alpha', F + '-enrollment-beta')),
    (('coordination.k8s.io',), ('leases',), ('create',), ()),
    (('coordination.k8s.io',), ('leases',), ('get', 'update'), (F + '-reconcile',)),
]), rules(by('Role', F + '-reconciler'))
assert rules(by('Role', F + '-sync')) == sorted([
    (('batch',), ('cronjobs',), ('get',), ('loris-bids-alpha',)),
    (('batch',), ('jobs',), ('create', 'get', 'list'), ()),
    (('',), ('pods',), ('list',), ()),
    (('',), ('pods/log',), ('get',), ()),
]), rules(by('Role', F + '-sync'))
assert rules(by('Role', F + '-webhook')) == sorted([
    (('batch',), ('cronjobs',), ('get',), (F + '-sync',)),
    (('batch',), ('jobs',), ('create',), ()),
]), rules(by('Role', F + '-webhook'))
for d in docs:
    if d['kind'] in ('Role', 'ClusterRole'):
        for rule in d['rules']:
            for field in ('apiGroups', 'resources', 'verbs', 'resourceNames'):
                assert '*' not in rule.get(field, []), (d['metadata']['name'], rule)
    assert d['kind'] not in ('ClusterRole', 'ClusterRoleBinding'), d['kind']
for component in ('reconciler', 'sync', 'webhook'):
    binding = by('RoleBinding', F + '-' + component)
    assert binding['roleRef'] == {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': F + '-' + component}
    assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': F + '-' + component, 'namespace': 'loris'}]

# Provider NetworkPolicy admits only LORIS client pods on the provider port.
policy = by('NetworkPolicy', F + '-provider')['spec']
assert policy['podSelector']['matchLabels'] == {'app.kubernetes.io/name': 'loris-forgejo-export',
    'app.kubernetes.io/instance': 'test', 'app.kubernetes.io/component': 'provider'}, policy
assert policy['policyTypes'] == ['Ingress']
assert policy['ingress'] == [{'from': [{'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'loris'}}},
                                       {'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'loris-bids'}}}],
                              'ports': [{'port': 8080, 'protocol': 'TCP'}]}], policy
assert by('NetworkPolicy', F + '-webhook')['spec']['ingress'] == [{'ports': [{'port': 8080, 'protocol': 'TCP'}]}]

# Pods: command, security contexts, /tmp, resources, env and mounts per CONTRACT.
common = {'LFX_REGISTRY', 'LFX_FORGEJO_URL', 'LFX_FORGEJO_PUBLIC_URL', 'HOME'}
components = {
    ('CronJob', F + '-reconcile'): ('reconciler', 'reconcile', common | {'LFX_NAMESPACE', 'LFX_FULLNAME',
        'LFX_RELEASE', 'LFX_ADMIN_USER', 'LFX_ADMIN_PASS', 'LFX_FORGEJO_PUBLIC_IP', 'LFX_WEBHOOK_URL', 'LFX_WEBHOOK_SECRET',
        'LFX_WORKER_TEMPLATE', 'LFX_ENROLLMENT', 'LFX_DRY_RUN', 'LFX_WAIT_SECONDS'},
        {'/opt/loris-forgejo-export', '/etc/loris-forgejo-export', '/tmp'}),
    ('Job', F + '-reconcile-1'): None,
    ('CronJob', F + '-sync'): ('sync', 'sync', common | {'LFX_SYNC_REPOSITORY', 'LFX_NAMESPACE',
        'LFX_FORGEJO_PUBLIC_IP', 'LFX_WAIT_SECONDS', 'LFX_ANNEX_URL', 'LFX_STAGE_TIMEOUT', 'LFX_IMPORT_TIMEOUT',
        'LFX_POLL_SECONDS'},
        {'/opt/loris-forgejo-export', '/etc/loris-forgejo-export', '/var/run/secrets/loris-forgejo-export/bot',
         '/clones', '/tmp'}),
    ('Deployment', F + '-provider'): ('provider', 'provider', common | {'LFX_ANNEX_URL', 'LFX_PORT',
        'LFX_CACHE_IDLE_TTL', 'LFX_CACHE_MAX_AGE', 'LFX_CACHE_MAX_BYTES', 'LFX_CACHE_SWEEP_SECONDS',
        'LFX_CACHE_OFFLINE_AFTER', 'LFX_CACHE_BATCH_WINDOW', 'LFX_CACHE_RETRY_AFTER', 'LFX_CACHE_CHUNK_TIMEOUT'},
        {'/opt/loris-forgejo-export', '/etc/loris-forgejo-export', '/var/run/secrets/loris-forgejo-export/bot',
         '/clones', '/provider-chunks', '/tmp'}),
    ('Deployment', F + '-webhook'): ('webhook', 'webhook', common | {'LFX_NAMESPACE', 'LFX_FULLNAME', 'LFX_PORT',
        'LFX_SYNC_CRONJOB'},
        {'/opt/loris-forgejo-export', '/etc/loris-forgejo-export', '/var/run/secrets/loris-forgejo-export/webhook',
         '/tmp'}),
}
components[('Job', F + '-reconcile-1')] = components[('CronJob', F + '-reconcile')]
for (kind, name), (component, script, env_names, mounts) in components.items():
    obj = by(kind, name)
    spec = pod_spec(obj)
    labels = (obj['spec']['jobTemplate']['spec'] if kind == 'CronJob' else obj['spec'])['template']['metadata']['labels']
    assert labels['app.kubernetes.io/component'] == component, (name, labels)
    assert spec['securityContext']['runAsUser'] == 33 and spec['securityContext']['runAsNonRoot'] is True, name
    assert spec['securityContext']['seccompProfile'] == {'type': 'RuntimeDefault'}, name
    assert {'name': 'tmp', 'emptyDir': {}} in spec['volumes'], name
    assert set(pod_annotations(obj)) >= {'checksum/scripts', 'checksum/registry'}, name
    [container] = spec['containers']
    assert container['image'] == 'docker.io/scilus/loris-forgejo-export:0.1.0', name
    assert container['command'] == ['python3', f'/opt/loris-forgejo-export/{script}.py'], name
    assert container['securityContext'] == {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                            'capabilities': {'drop': ['ALL']}}, name
    assert container['resources']['limits']['memory'], name
    names = [e['name'] for e in container['env']]
    assert len(names) == len(set(names)) and set(names) == env_names, (name, sorted(set(names) ^ env_names))
    assert {m['mountPath'] for m in container['volumeMounts']} == mounts, (name, container['volumeMounts'])
    e = env(container)
    assert e['LFX_FORGEJO_URL'] == values['forgejo']['url'] and e['HOME'] == '/tmp', name
    assert e['LFX_REGISTRY'] == '/etc/loris-forgejo-export/registry.json', name
    if 'LFX_NAMESPACE' in e:
        assert e['LFX_NAMESPACE'] == {'fieldRef': {'fieldPath': 'metadata.namespace'}}, name
    if component in ('sync', 'provider'):
        assert {'name': 'data', 'persistentVolumeClaim': {'claimName': 'loris-data'}} in spec['volumes'], name
        assert {'name': 'bot', 'secret': {'secretName': F + '-bot', 'optional': True, 'defaultMode': 0o440}} \
            in spec['volumes'], name
        mount = {m['mountPath']: m for m in container['volumeMounts']}
        assert mount['/clones'].get('subPath') == 'forgejo/clones' and not mount['/clones'].get('readOnly'), name
        if component == 'provider':
            assert mount['/provider-chunks'].get('subPath') == 'data/content-provider-chunks', name

reconcile = env(pod_spec(by('CronJob', F + '-reconcile'))['containers'][0])
assert reconcile['LFX_WEBHOOK_URL'] == 'http://' + F + '-webhook.loris.svc.cluster.local:8080/forgejo', reconcile
assert reconcile['LFX_WEBHOOK_SECRET'] == {'secretKeyRef': {'name': F + '-webhook', 'key': 'secret'}}
assert reconcile['LFX_ADMIN_USER'] == {'secretKeyRef': {'name': 'forgejo-admin', 'key': 'username'}}
assert reconcile['LFX_ADMIN_PASS'] == {'secretKeyRef': {'name': 'forgejo-admin', 'key': 'password'}}
assert reconcile['LFX_FULLNAME'] == F and reconcile['LFX_ENROLLMENT'] == '1' and reconcile['LFX_DRY_RUN'] == '0'
assert reconcile['LFX_FORGEJO_PUBLIC_IP'] == '192.0.2.80'
assert reconcile['LFX_WORKER_TEMPLATE'] == '/etc/loris-forgejo-export/worker-template'
assert by('CronJob', F + '-reconcile')['spec']['concurrencyPolicy'] == 'Forbid'
assert by('CronJob', F + '-reconcile')['spec']['schedule'] == '17 3 * * *'
job = by('Job', F + '-reconcile-1')
assert 'helm.sh/hook' not in job['metadata'].get('annotations', {}) and job['spec']['ttlSecondsAfterFinished'] == 86400

sync_cron = by('CronJob', F + '-sync')['spec']
assert sync_cron['concurrencyPolicy'] == 'Forbid' and sync_cron['suspend'] is False and sync_cron['schedule'] == '41 * * * *'
sync_job = sync_cron['jobTemplate']['spec']
assert (sync_job['backoffLimit'], sync_job['activeDeadlineSeconds'], sync_job['ttlSecondsAfterFinished']) == (0, 14400, 86400)
sync_pod = sync_job['template']['spec']
assert sync_pod['restartPolicy'] == 'Never' and sync_pod['serviceAccountName'] == F + '-sync'
sync_env = sync_pod['containers'][0]['env']
assert sync_env[0] == {'name': 'LFX_SYNC_REPOSITORY', 'value': ''}, sync_env[0]
assert env(sync_pod['containers'][0])['LFX_STAGE_TIMEOUT'] == '10800'

provider = by('Deployment', F + '-provider')
assert provider['spec']['strategy'] == {'type': 'Recreate'} and provider['spec']['replicas'] == 1
provider_pod = provider['spec']['template']['spec']
assert provider_pod['automountServiceAccountToken'] is False and 'serviceAccountName' not in provider_pod
container = provider_pod['containers'][0]
for probe in ('readinessProbe', 'livenessProbe'):
    assert container[probe]['httpGet'] == {'path': '/healthz', 'port': 'http'}, probe
assert container['ports'] == [{'name': 'http', 'containerPort': 8080, 'protocol': 'TCP'}]
assert env(container)['LFX_PORT'] == '8080' and env(container)['LFX_CACHE_MAX_BYTES'] == '2147483648'
service = by('Service', F + '-provider')['spec']
assert service['ports'][0]['port'] == 8080 and service['selector']['app.kubernetes.io/component'] == 'provider'

webhook = by('Deployment', F + '-webhook')['spec']['template']['spec']
assert webhook['serviceAccountName'] == F + '-webhook'
w = env(webhook['containers'][0])
assert w['LFX_SYNC_CRONJOB'] == F + '-sync' and w['LFX_PORT'] == '8080' and w['LFX_FULLNAME'] == F
assert {'name': 'webhook-secret', 'secret': {'secretName': F + '-webhook', 'items': [{'key': 'secret', 'path': 'secret'}],
        'defaultMode': 0o440}} in webhook['volumes']
for probe in ('readinessProbe', 'livenessProbe'):
    assert webhook['containers'][0][probe]['httpGet'] == {'path': '/healthz', 'port': 'http'}
import base64
secret = base64.b64decode(by('Secret', F + '-webhook')['data']['secret']).decode()
assert len(secret) == 48 and secret.isalnum(), secret

registry = json.loads(by('ConfigMap', F + '-registry')['data']['registry.json'])
assert registry['settings']['botUser'] == 'loris-export' and registry['bidsImport'] == {'enabled': True}
assert [s['name'] for s in registry['sites']] == ['nfs-site', 'alpha', 'beta']
scripts = by('ConfigMap', F + '-scripts')['data']
assert 'registry.py' in scripts and all(key.endswith('.py') and '/' not in key for key in scripts), sorted(scripts)
template = by('ConfigMap', F + '-worker-template').get('data') or {}
assert all('/' not in key for key in template), sorted(template)
projected = [v for v in pod_spec(by('Job', F + '-reconcile-1'))['volumes'] if v['name'] == 'registry'][0]['projected']
items = [i for s in projected['sources'][1:] for i in s['configMap']['items']]
assert sorted(template) == [i['key'] for i in items] and all(i['path'] == 'worker-template/' + i['key'] for i in items)
"

# The registry renders a file registry.py accepts.
python3 - "$work_dir/webhook.yaml" "$chart_dir/files" <<'EOF'
import json, sys, yaml
sys.path.insert(0, sys.argv[2])
import registry
doc = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d['metadata']['name'].endswith('-registry'))
errors = registry.validate(json.loads(doc['data']['registry.json']))
assert not errors, errors
EOF

# Image digests pin every container.
render digest.yaml "${base[@]}" --set-string image.digest=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
images=$(grep -E '^[[:space:]]*image:' "$work_dir/digest.yaml" | sed 's/^[[:space:]]*//' | sort -u)
[[ "$images" == 'image: docker.io/scilus/loris-forgejo-export@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' && $(wc -l <<<"$images") -eq 1 ]] || {
    printf 'Digest must pin every image; got:\n%s\n' "$images" >&2
    exit 1
}

# A registry change rolls the pods; unrelated values do not change the checksum.
variant changed 'v["sites"][2]["forgejo"]["datasets"].append("Study_D")'
render changed.yaml -f "$work_dir/changed.json"
render resources.yaml "${base[@]}" --set provider.resources.limits.memory=4Gi
python3 - "$work_dir" "$fullname" <<'EOF'
import sys, yaml
def annotations(path):
    docs = [d for d in yaml.safe_load_all(open(f'{sys.argv[1]}/{path}')) if d]
    return {d['metadata']['name']: d['spec']['template']['metadata']['annotations']
            for d in docs if d['kind'] == 'Deployment'}
base, changed, other = annotations('webhook.yaml'), annotations('changed.yaml'), annotations('resources.yaml')
for name in base:
    assert base[name]['checksum/registry'] != changed[name]['checksum/registry'], name
    assert base[name]['checksum/scripts'] == changed[name]['checksum/scripts'], name
    assert base == other, name
EOF

# Argo Events mode: Sensor with filters, no built-in receiver.
argo=(--set triggers.webhook.enabled=false --set triggers.argoEvents.enabled=true
      --set-string triggers.argoEvents.eventSourceName=forgejo --set-string triggers.argoEvents.eventName=push
      --set-string triggers.argoEvents.webhookUrl=http://forgejo-eventsource-svc.argo-events:12000/push)
render argo.yaml "${base[@]}" "${argo[@]}"
check argo.yaml "
import re
F = '$fullname'
names = kinds()
for kind in ('Deployment', 'Service', 'NetworkPolicy', 'Secret', 'ServiceAccount', 'RoleBinding'):
    assert (kind, F + '-webhook') not in names, kind
assert ('Role', F + '-webhook') in names
sa = by('ServiceAccount', F + '-sensor')
assert sa['metadata']['namespace'] == 'argo-events'
binding = by('RoleBinding', F + '-sensor')
assert 'namespace' not in binding['metadata']
assert binding['roleRef'] == {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': F + '-webhook'}
assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': F + '-sensor', 'namespace': 'argo-events'}]
sensor = by('Sensor', F + '-sync')
assert sensor['apiVersion'] == 'argoproj.io/v1alpha1' and sensor['metadata']['namespace'] == 'argo-events'
assert sensor['spec']['template']['serviceAccountName'] == F + '-sensor'
[dep] = sensor['spec']['dependencies']
assert (dep['eventSourceName'], dep['eventName']) == ('forgejo', 'push')
assert dep['filters']['dataLogicalOperator'] == 'and'
filters = {(f['path'], f.get('comparator', '=')): f['value'] for f in dep['filters']['data']}
assert filters[('header.X-Forgejo-Event.0', '=')] == ['^push\$']
assert filters[('body.ref', '=')] == ['^refs/heads/.*\$']
assert filters[('body.ref', '!=')] == ['^refs/heads/git-annex\$']
[regex] = filters[('body.repository.full_name', '=')]
for repo, ok in (('alpha/Study_A', True), ('alpha/Study.B', True), ('beta/Study_C', True), ('alpha/StudyxB', False),
                 ('alpha/.worker', False), ('nfs-site/Study_A', False), ('beta/Study_C2', False)):
    assert bool(re.match(regex, repo)) == ok, (regex, repo)
[trigger] = sensor['spec']['triggers']
k8s = trigger['template']['k8s']
assert k8s['operation'] == 'create'
job = k8s['source']['resource']
assert job['kind'] == 'Job' and job['metadata']['namespace'] == 'loris'
assert job['metadata']['generateName'] == F + '-push-'
assert job['spec'] == by('CronJob', F + '-sync')['spec']['jobTemplate']['spec'], 'Sensor Job must use syncPodSpec'
assert job['spec']['template']['spec']['containers'][0]['env'][0]['name'] == 'LFX_SYNC_REPOSITORY'
assert k8s['parameters'] == [{'src': {'dependencyName': 'push', 'dataKey': 'body.repository.full_name'},
                              'dest': 'spec.template.spec.containers.0.env.0.value'}]
reconcile = env(pod_spec(by('CronJob', F + '-reconcile'))['containers'][0])
assert reconcile['LFX_WEBHOOK_URL'] == 'http://forgejo-eventsource-svc.argo-events:12000/push'
assert 'LFX_WEBHOOK_SECRET' not in reconcile
"

# Scheduled sync only: no webhook objects, and the reconciler registers no hook.
render scheduled.yaml "${base[@]}" --set triggers.webhook.enabled=false
check scheduled.yaml "
F = '$fullname'
assert not [d for d in docs if 'webhook' in d['metadata']['name'] or d['kind'] == 'Sensor'], kinds()
for kind, name in (('CronJob', F + '-reconcile'), ('Job', F + '-reconcile-1')):
    e = env(pod_spec(by(kind, name))['containers'][0])
    assert e['LFX_WEBHOOK_URL'] == '' and 'LFX_WEBHOOK_SECRET' not in e, e
"

# Existing webhook Secret, no enrollment, no importing site, worker template override.
variant options '
v["bidsImport"]["enabled"] = False
v["reconciler"] = {"enrollment": {"enabled": False}}
v["triggers"] = {"webhook": {"existingSecret": "my-hook"}}
v["forgejo"]["worker"] = {"templateFiles": {".forgejo/workflows/probe.yml": "runs-on: ${{ vars.X }} @RUNNER_LABEL@\n",
                                            "README.md": "x"}}
'
render options.yaml -f "$work_dir/options.json"
check options.yaml "
F = '$fullname'
assert ('Secret', F + '-webhook') not in kinds() and ('Secret', 'my-hook') not in kinds()
assert (('',), ('secrets',), ('get', 'update', 'patch'), (F + '-bot',)) in rules(by('Role', F + '-reconciler')), \
    rules(by('Role', F + '-reconciler'))
assert not [r for r in by('Role', F + '-sync')['rules'] if 'cronjobs' in r['resources']]
pod = pod_spec(by('CronJob', F + '-reconcile'))
e = env(pod['containers'][0])
assert e['LFX_ENROLLMENT'] == '0'
assert e['LFX_WEBHOOK_SECRET'] == {'secretKeyRef': {'name': 'my-hook', 'key': 'secret'}}
webhook = by('Deployment', F + '-webhook')['spec']['template']['spec']
assert [v for v in webhook['volumes'] if v['name'] == 'webhook-secret'][0]['secret']['secretName'] == 'my-hook'
data = by('ConfigMap', F + '-worker-template')['data']
assert data == {'.forgejo__workflows__probe.yml': 'runs-on: \${{ vars.X }} @RUNNER_LABEL@\n', 'README.md': 'x'}, data
[volume] = [v for v in pod['volumes'] if v['name'] == 'registry']
assert volume['projected']['sources'] == [
    {'configMap': {'name': F + '-registry'}},
    {'configMap': {'name': F + '-worker-template', 'items': [
        {'key': '.forgejo__workflows__probe.yml', 'path': 'worker-template/.forgejo__workflows__probe.yml'},
        {'key': 'README.md', 'path': 'worker-template/README.md'}]}}], volume
"

# Validation failures (CONTRACT "Validation rules" and site layout).
fail_variant() {
    local expected=$1 mutation=$2
    variant failure "$mutation"
    expect_failure "$expected" -f "$work_dir/failure.json"
}
fail_variant 'Forgejo sites require source pvc' 'v["sites"][1]["source"] = "nfs"'
fail_variant 'must not set server or nfsPort' 'v["sites"][1]["server"] = "192.0.2.1"'
fail_variant 'claimName must be "loris-data"' 'v["sites"][1]["claimName"] = "other"'
fail_variant 'subPath must be "forgejo/clones/alpha"' 'v["sites"][1]["subPath"] = "alpha"'
fail_variant 'subPath must be "forgejo/clones/beta"' 'del v["sites"][2]["subPath"]'
fail_variant 'not a valid repository/folder name' 'v["sites"][1]["forgejo"]["datasets"].append(".Worker")'
fail_variant 'not a valid repository/folder name' 'v["sites"][1]["forgejo"]["datasets"].append("Study.git")'
fail_variant 'not a valid repository/folder name' 'v["sites"][1]["forgejo"]["datasets"].append("-bad")'
fail_variant 'not a valid repository/folder name' 'v["sites"][1]["forgejo"]["datasets"].append("a/b")'
fail_variant 'not a valid repository/folder name' 'v["sites"][1]["forgejo"]["datasets"].append("x" * 101)'
fail_variant 'is duplicated' 'v["sites"][1]["forgejo"]["datasets"].append("study_a")'
fail_variant 'is duplicated' 'v["sites"].append(dict(v["sites"][2]))'
fail_variant 'lowercase DNS label' 'v["sites"][2]["name"] = "Beta"; v["sites"][2]["subPath"] = "forgejo/clones/Beta"'
fail_variant 'lowercase DNS label' 'n = "s" * 35; v["sites"][2]["name"] = n; v["sites"][2]["subPath"] = "forgejo/clones/" + n'
fail_variant 'collides with forgejo.botUser' 'v["forgejo"]["botUser"] = "Alpha"'
fail_variant 'collides with site "alpha"' 'v["sites"][2]["name"] = "alpha-worker"; v["sites"][2]["subPath"] = "forgejo/clones/alpha-worker"'
fail_variant 'publicationTeam and forgejo.exportTeam must differ' 'v["forgejo"]["exportTeam"] = "Datasets"'
fail_variant 'sites without datasets must remain disabled' 'v["sites"][1]["forgejo"]["datasets"] = []'
fail_variant 'symlinkedContent identity' 'v["sites"][1]["bidsImport"]["updates"] = "replace"'
fail_variant 'forgejo.publicUrl must be the public https URL' 'v["forgejo"]["publicUrl"] = "http://forge.example.org"'
fail_variant 'forgejo.publicUrl must be the public https URL' 'del v["forgejo"]["publicUrl"]'
fail_variant 'forgejo.publicUrl must be the public https URL' 'v["forgejo"]["publicUrl"] = "ftp://forge.example.org"; v["forgejo"]["allowInsecurePublicUrl"] = True'
fail_variant 'forgejo.url must be the in-cluster' 'del v["forgejo"]["url"]'
fail_variant 'forgejo.adminSecret.name is required' 'del v["forgejo"]["adminSecret"]'
fail_variant 'forgejo.publicIP must be an IP address' 'v["forgejo"]["publicIP"] = "forge.example.org"'
fail_variant 'forgejo.annexUrl must look like' 'v["forgejo"]["annexUrl"] = "http://forgejo/git-annex-p2phttp"'
fail_variant 'not a valid repository file path' 'v["forgejo"]["worker"] = {"templateFiles": {"bad path.yml": "x"}}'
expect_failure 'mutually exclusive' "${base[@]}" "${argo[@]}" --set triggers.webhook.enabled=true
for missing in eventSourceName eventName webhookUrl; do
    expect_failure 'triggers.argoEvents requires eventSourceName, eventName and an http(s) webhookUrl' \
        "${base[@]}" "${argo[@]}" --set-string "triggers.argoEvents.$missing="
done

printf 'Forgejo export Helm render tests passed.\n'
