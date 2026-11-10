import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_loris_bids_runtime import runtime


previews = runtime.helper('bids_previews')


class PreviewCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        clone = self.root / 'assembly_bids/site/dataset'
        self.target = clone / '.git/annex/objects/Xx/Yy/SHA256E-s4--a.nii.gz/SHA256E-s4--a.nii.gz'
        self.target.parent.mkdir(parents=True)
        self.path = self.root / 'assembly_bids/view/sub-example_T1w.nii.gz'
        self.path.parent.mkdir()
        self.path.symlink_to(self.target)

    def test_cache_waits_for_content_then_verifies_registered_path(self):
        states = iter(('fetching', 'present'))
        def request(req, timeout):
            self.assertEqual(json.loads(req.data), {
                'site': 'site', 'dataset': 'dataset', 'key': self.target.name})
            state = next(states)
            if state == 'present':
                self.target.write_bytes(b'data')
            return io.BytesIO(json.dumps({'state': state}).encode())
        with patch.object(previews.urllib.request, 'urlopen', side_effect=request), patch.object(
                previews.time, 'sleep'):
            previews.cache_content(self.path, self.root, 'site', 'dataset', 'http://cache')

    def test_cache_failure_is_not_reported_as_success(self):
        for state in ('unavailable', 'present'):
            with self.subTest(state=state), patch.object(
                    previews.urllib.request, 'urlopen',
                    return_value=io.BytesIO(json.dumps({'state': state}).encode())):
                with self.assertRaises(ValueError):
                    previews.cache_content(self.path, self.root, 'site', 'dataset', 'http://cache')

    def test_other_dataset_path_is_refused_before_network_request(self):
        with patch.object(previews.urllib.request, 'urlopen') as request:
            with self.assertRaisesRegex(ValueError, 'approved dataset annex'):
                previews.cache_content(self.path, self.root, 'site', 'another', 'http://cache')
            request.assert_not_called()

    def test_idle_import_allows_repair_after_a_later_import_failure(self):
        for status in ('succeeded', 'failed', 'updated'):
            with self.subTest(status=status):
                previews.require_idle_import({'mode': 'import', 'status': status})
        for row in (None, {'mode': 'setup', 'status': 'succeeded'},
                    {'mode': 'import', 'status': 'new'},
                    {'mode': 'import', 'status': 'reserved'},
                    {'mode': 'import', 'status': 'running'}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                previews.require_idle_import(row)


class PreviewLauncherTests(unittest.TestCase):
    def run_launcher(self, apply=False, **overrides):
        arguments = {
            'context': 'test', 'namespace': 'central', 'site': 'site',
            'dataset': 'dataset', 'commit': 'a' * 40, 'expected-count': '164',
            'job': 'preview-audit',
        }
        arguments.update(overrides)
        tool = Path(__file__).resolve().parents[1] / 'tools/repair-bids-previews.py'
        argv = [str(tool)]
        for key, value in arguments.items():
            argv.extend(['--' + key, value])
        if apply:
            argv.append('--apply')
        with patch.object(sys, 'argv', argv):
            runpy.run_path(str(tool), run_name='__main__')

    def test_job_retains_installed_profile_but_removes_privileged_initialization(self):
        cron = {'spec': {'suspend': True, 'jobTemplate': {'spec': {'template': {'spec': {
            'volumes': [{'name': 'profile', 'configMap': {'name': 'installed'}}],
            'initContainers': [{'name': 'privileged-ledger-initializer'}],
            'serviceAccountName': 'importer',
            'containers': [{'name': 'bids-import', 'image': 'loris@sha256:pinned',
                            'env': [{'name': 'LORIS_DB_PASSWORD',
                                     'valueFrom': {'secretKeyRef': {
                                         'name': 'application', 'key': 'password'}}}]}],
        }}}}}}
        profile = {'data': {
            'runtime.json': json.dumps({'site': {'name': 'site', 'source': 'clone'}}),
            'config.py': 'installed config', 'bids_runtime.py': 'installed runtime',
        }}
        for apply in (False, True):
            created = []
            def kubectl(command, **kwargs):
                if 'get' in command:
                    resource = cron if 'cronjob' in command else profile
                    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(resource))
                created.append(json.loads(kwargs['input']))
                return subprocess.CompletedProcess(command, 0)
            with self.subTest(apply=apply), patch.object(subprocess, 'run', side_effect=kubectl):
                self.run_launcher(apply=apply)
            self.assertEqual(len(created), 2)
            self.assertEqual(created[0]['data']['bids_runtime.py'], 'installed runtime')
            self.assertIn('bids_previews.py', created[0]['data'])
            spec = created[1]['spec']
            pod = spec['template']['spec']
            self.assertEqual(spec['backoffLimit'], 0)
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertNotIn('initContainers', pod)
            self.assertNotIn('serviceAccountName', pod)
            container = pod['containers'][0]
            self.assertEqual(container['image'], 'loris@sha256:pinned')
            self.assertEqual('--apply' in container['args'], apply)
            self.assertEqual(pod['volumes'][0]['configMap']['name'], 'preview-audit-profile')
            self.assertEqual(spec['template']['metadata']['labels'], {
                'app.kubernetes.io/name': 'loris-bids',
                'app.kubernetes.io/component': 'bids-preview-repair',
            })

    def test_invalid_approval_arguments_fail_before_cluster_access(self):
        for overrides in ({'job': 'invalid-'}, {'commit': 'short'}, {'expected-count': '0'}):
            with self.subTest(overrides=overrides), patch.object(subprocess, 'run') as command:
                with self.assertRaises(SystemExit):
                    self.run_launcher(**overrides)
                command.assert_not_called()
