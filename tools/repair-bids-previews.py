#!/usr/bin/env python3
"""Create a bounded preview-only audit/repair Job from an installed LORIS profile."""

import argparse
import copy
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--expected-count', required=True, type=int)
    parser.add_argument('--job', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?', args.job):
        parser.error('job must be a DNS label of at most 50 characters')
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        parser.error('commit must be a full approved commit')
    if args.expected_count <= 0:
        parser.error('expected-count must be positive')
    kubectl = ['kubectl', '--context', args.context, '-n', args.namespace]
    def get(kind, name):
        return json.loads(subprocess.run(kubectl + ['get', kind, name, '-o', 'json'],
                                         check=True, capture_output=True, text=True).stdout)
    cron = get('cronjob', 'loris-bids-' + args.site)
    spec = copy.deepcopy(cron['spec']['jobTemplate']['spec'])
    pod = spec['template']['spec']
    if not cron['spec'].get('suspend'):
        parser.error('suspend site discovery before preview maintenance')
    profile_volume = next(volume for volume in pod['volumes'] if volume['name'] == 'profile')
    profile = get('configmap', profile_volume['configMap']['name'])
    config = json.loads(profile['data']['runtime.json'])
    if config['site']['name'] != args.site or config['site'].get('source') != 'clone':
        parser.error('preview fetch repair requires the matching clone-sourced site')
    files = Path(__file__).resolve().parents[1] / 'charts/loris/files'
    data = {name: profile['data'][name] for name in ('runtime.json', 'config.py', 'bids_runtime.py')}
    data['bids_previews.py'] = (files / 'bids_previews.py').read_text()
    profile_name = args.job + '-profile'
    profile_volume['configMap']['name'] = profile_name
    pod.pop('initContainers', None)
    pod.pop('serviceAccountName', None)
    pod['automountServiceAccountToken'] = False
    pod['restartPolicy'] = 'Never'
    container = next(c for c in pod['containers'] if c['name'] == 'bids-import')
    container['command'] = ['python3', '-u', '/import-profile/bids_previews.py']
    container.setdefault('env', []).append({'name': 'MPLCONFIGDIR', 'value': '/tmp/matplotlib'})
    container['args'] = [
        '--config', '/import-profile/runtime.json', '--dataset', args.dataset,
        '--commit', args.commit, '--expected-count', str(args.expected_count),
        '--cache-url', 'http://re-move-cache.' + args.namespace + '.svc.cluster.local:8080',
    ] + (['--apply'] if args.apply else [])
    spec['template']['metadata'] = {'labels': {
        'app.kubernetes.io/name': 'loris-bids',
        'app.kubernetes.io/component': 'bids-preview-repair',
    }}
    spec.update(backoffLimit=0, activeDeadlineSeconds=86400, ttlSecondsAfterFinished=604800)
    resources = [
        {'apiVersion': 'v1', 'kind': 'ConfigMap',
         'metadata': {'name': profile_name, 'namespace': args.namespace}, 'data': data},
        {'apiVersion': 'batch/v1', 'kind': 'Job',
         'metadata': {'name': args.job, 'namespace': args.namespace}, 'spec': spec},
    ]
    for resource in resources:
        subprocess.run(kubectl + ['create', '-f', '-'], input=json.dumps(resource), text=True, check=True)
    print('Job created. Inspect its logs and require preview-complete after an applied repair.')


if __name__ == '__main__':
    main()
