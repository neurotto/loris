import copy
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, select


FILES = Path(__file__).resolve().parents[1] / 'charts/loris/files'
spec = importlib.util.spec_from_file_location('bids_runtime', FILES / 'bids_runtime.py')
runtime = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runtime
spec.loader.exec_module(runtime)


def configuration():
    return {
        'namespace': 'test', 'cronJob': 'loris-bids-discover-nandou',
        'bidsImport': {
            'enabled': True, 'defaults': {
                'project': 'Project', 'projectAlias': 'PROJ', 'recruitmentTarget': None,
                'defaultVisit': '000', 'datasetType': 'raw', 'createCandidates': True,
                'createSessions': True, 'mode': 'import',
            },
        },
        'site': {'name': 'nandou', 'enabled': True, 'siteAlias': 'NAND',
                 'bidsImport': {'enabled': True}},
    }


@contextmanager
def unlocked(*args):
    yield True


class FakeApi:
    def __init__(self, config):
        self.objects = {}
        self.creates = []
        self.put('cronjobs', {
            'apiVersion': 'batch/v1', 'kind': 'CronJob',
            'metadata': {'name': config['cronJob'], 'uid': 'cron-uid',
                         'annotations': {'argocd.argoproj.io/tracking-id': 'not-inherited'}},
            'spec': {'jobTemplate': {'spec': {
                'ttlSecondsAfterFinished': 600,
                'template': {
                    'metadata': {'annotations': {'argocd.argoproj.io/tracking-id': 'not-inherited'}},
                    'spec': {'serviceAccountName': 'discovery', 'automountServiceAccountToken': True,
                             'initContainers': [{'name': 'schema', 'env': [{'name': 'ROOT_CREDENTIAL'}]}],
                             'volumes': [{'name': 'profile', 'configMap': {'name': 'profile'}}],
                             'containers': [{'name': 'discover', 'image': 'pinned@sha256:first'}]},
                },
            }}},
        })
        self.put('configmaps', {'apiVersion': 'v1', 'kind': 'ConfigMap',
                               'metadata': {'name': 'profile', 'uid': 'profile-uid'},
                               'data': {'runtime.json': json.dumps(config), 'script.py': 'original'}})

    def put(self, kind, obj):
        self.objects[(kind, obj['metadata']['name'])] = copy.deepcopy(obj)

    def get(self, kind, name):
        obj = self.objects.get((kind, name))
        return copy.deepcopy(obj) if obj else None

    def request(self, kind, name='', method='GET', body=None):
        if method == 'POST':
            name = body['metadata']['name']
            if (kind, name) in self.objects:
                raise runtime.ApiError(409, 'Conflict')
            body = copy.deepcopy(body)
            body['metadata']['uid'] = name + '-uid'
            self.put(kind, body)
            self.creates.append((kind, name))
        elif method == 'PATCH':
            current = self.get(kind, name)
            for key, value in body.items():
                current.setdefault(key, {}).update(copy.deepcopy(value))
            self.put(kind, current)
        return self.get(kind, name)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = self.root / 'assembly_bids/nandou'
        self.source = self.site / 'dataset'
        self.source.mkdir(parents=True)
        (self.source / 'dataset_description.json').write_text('{"Name":"Dataset","BIDSVersion":"1.8.0"}')
        (self.source / 'participants.tsv').write_text('participant_id\nsub-example\n')
        (self.source / 'sub-example/anat').mkdir(parents=True)
        (self.source / 'sub-example/anat/sub-example_T1w.nii.gz').write_bytes(b'fixture')
        self.config = configuration()
        self.api = FakeApi(self.config)
        self.engine = create_engine('sqlite://')
        runtime.METADATA.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.root_patch = patch.object(runtime, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def enroll(self):
        settings = runtime.resolve_settings(self.config, 'dataset', 'Dataset')
        return runtime.enroll(self.engine, 'nandou', 'dataset', settings, 'Dataset'), settings

    def discovery(self):
        with patch.object(runtime, 'initialize'), patch.object(runtime, 'database_lock', unlocked):
            runtime.discover(self.engine, self.config, self.api)

    def test_effective_defaults_preserve_false_and_null(self):
        self.config['bidsImport']['defaults']['recruitmentTarget'] = 12
        self.config['site']['bidsImport'].update({
            'project': 'Site project', 'datasets': {
                'dataset': {'createCandidates': False, 'recruitmentTarget': None, 'cohort': 'Override'},
            },
        })
        settings = runtime.resolve_settings(self.config, 'dataset', 'From description')
        self.assertEqual(settings['project'], 'Site project')
        self.assertEqual(settings['cohort'], 'Override')
        self.assertFalse(settings['createCandidates'])
        self.assertIsNone(settings['recruitmentTarget'])
        self.assertEqual(settings['siteAlias'], 'NAND')

    def test_immediate_discovery_does_not_recurse_or_use_override_as_allowlist(self):
        nested = self.site / 'container/nested'
        nested.mkdir(parents=True)
        (nested / 'dataset_description.json').write_text('{"Name":"Nested"}')
        (self.site / 'derivatives').mkdir()
        self.assertEqual(list(runtime.discover_datasets(self.site, {})), [self.source])
        self.assertEqual(list(runtime.discover_datasets(self.site, {'dataset': {'enabled': False}})), [])
        (self.site / 'dataset_description.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Export root'):
            list(runtime.discover_datasets(self.site, {}))

    def test_invalid_metadata_and_symlink_source_are_rejected(self):
        (self.source / 'dataset_description.json').write_text('{"Name": ""}')
        with self.assertRaisesRegex(ValueError, 'Name'):
            runtime.read_name(self.source)
        (self.site / 'escape').symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Symlinked'):
            self.discovery()

    def test_names_are_bounded_and_collision_resistant(self):
        one = runtime.run_name('a' * 63, 'Name with Unicode \u00e9' * 20, 'a' * 64, 123)
        two = runtime.run_name('a' * 63, 'Name with Unicode e' * 20, 'b' * 64, 123)
        self.assertLessEqual(len(one), 63)
        self.assertRegex(one, r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
        self.assertNotEqual(one, two)

    def test_success_survives_job_expiry_and_image_updates(self):
        self.discovery()
        identity, settings = self.enroll()
        row = runtime.get_dataset(self.engine, identity)
        self.assertEqual(row['attempt'], 1)
        runtime.record(self.engine, identity, 1, 'succeeded')
        del self.api.objects[('jobs', row['job_name'])]
        cron = self.api.objects[('cronjobs', self.config['cronJob'])]
        cron['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['image'] = 'new-image'
        self.discovery()
        self.assertEqual(len([kind for kind, _ in self.api.creates if kind == 'jobs']), 1)
        self.assertIsNone(runtime.reserve(self.engine, identity, settings))

    def test_preflight_does_not_suppress_import_and_failed_attempt_retries(self):
        self.config['bidsImport']['defaults']['mode'] = 'preflight'
        self.discovery()
        identity, _ = self.enroll()
        runtime.record(self.engine, identity, 1, 'succeeded')
        self.config['bidsImport']['defaults']['mode'] = 'import'
        self.discovery()
        self.assertEqual(runtime.get_dataset(self.engine, identity)['attempt'], 2)
        runtime.record(self.engine, identity, 2, 'failed', 'partial acquisition')
        self.discovery()
        row = runtime.get_dataset(self.engine, identity)
        self.assertEqual(row['attempt'], 3)
        self.assertEqual(row['status'], 'reserved')
        with self.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(runtime.ATTEMPTS)).all()), 3)

    def test_overlapping_discovery_and_unknown_create_result_do_not_duplicate(self):
        self.discovery()
        self.discovery()
        identity, _ = self.enroll()
        self.assertEqual(runtime.get_dataset(self.engine, identity)['attempt'], 1)
        self.assertEqual(len([kind for kind, _ in self.api.creates if kind == 'jobs']), 1)

    def test_retry_reconciles_a_terminated_worker_not_a_running_one(self):
        self.discovery()
        identity, _ = self.enroll()
        runtime.record(self.engine, identity, 1, 'running')
        self.discovery()
        row = runtime.get_dataset(self.engine, identity)
        self.assertEqual(row['attempt'], 1)
        self.api.objects[('jobs', row['job_name'])]['status'] = {
            'conditions': [{'type': 'Failed', 'status': 'True'}],
        }
        self.discovery()
        self.assertEqual(runtime.get_dataset(self.engine, identity)['attempt'], 2)

    def test_missing_dataset_does_not_leave_a_terminal_worker_permanently_running(self):
        self.discovery()
        identity, _ = self.enroll()
        row = runtime.get_dataset(self.engine, identity)
        runtime.record(self.engine, identity, 1, 'running')
        self.source.rename(self.root / 'removed')
        self.api.objects[('jobs', row['job_name'])]['status'] = {
            'conditions': [{'type': 'Failed', 'status': 'True'}],
        }
        self.discovery()
        row = runtime.get_dataset(self.engine, identity)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['attempt'], 1)

    def test_worker_token_and_argo_tracking_are_not_inherited(self):
        self.discovery()
        identity, _ = self.enroll()
        row = runtime.get_dataset(self.engine, identity)
        job = self.api.get('jobs', row['job_name'])
        pod = job['spec']['template']
        self.assertNotIn('serviceAccountName', pod['spec'])
        self.assertNotIn('initContainers', pod['spec'])
        self.assertFalse(pod['spec']['automountServiceAccountToken'])
        self.assertNotIn('annotations', pod['metadata'])
        self.assertNotIn('annotations', job['metadata'])
        self.assertEqual(job['spec']['backoffLimit'], 0)
        cm = self.api.get('configmaps', row['job_name'])
        self.assertTrue(cm['immutable'])
        self.assertEqual(cm['metadata']['ownerReferences'][0]['uid'], job['metadata']['uid'])

    def test_reserved_attempt_uses_frozen_spec_after_updates(self):
        self.discovery()
        identity, _ = self.enroll()
        row = runtime.get_dataset(self.engine, identity)
        old_job = self.api.get('jobs', row['job_name'])
        del self.api.objects[('jobs', row['job_name'])]
        self.api.objects[('cronjobs', self.config['cronJob'])]['spec']['jobTemplate']['spec'][
            'template']['spec']['containers'][0]['image'] = 'new-image'
        self.api.objects[('configmaps', 'profile')]['data']['script.py'] = 'changed'
        self.discovery()
        self.assertEqual(self.api.get('jobs', row['job_name'])['spec'], old_job['spec'])

    def test_changed_data_or_identity_requires_review(self):
        identity, settings = self.enroll()
        settings['cohort'] = 'Changed'
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            runtime.enroll(self.engine, 'nandou', 'dataset', settings, 'Dataset')
        (self.source / 'sub-example/anat/sub-example_T1w.nii.gz').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'content changed'):
            self.enroll()
        self.assertEqual(runtime.get_dataset(self.engine, identity)['attempt'], 0)

    def annex(self, relative, key, content=None):
        path = self.source / relative
        objects = self.source / '.git/annex/objects/Xx/Yy' / key
        objects.mkdir(parents=True, exist_ok=True)
        if content is not None:
            (objects / key).write_bytes(content)
        path.unlink()
        path.symlink_to(os.path.relpath(objects / key, path.parent))
        return objects / key

    def test_annex_fingerprint_ignores_content_presence_and_git_metadata(self):
        stored = self.annex('sub-example/anat/sub-example_T1w.nii.gz', 'MD5E-s7--x.nii.gz', b'fixture')
        present = runtime.source_fingerprint(self.source)
        stored.unlink()
        (self.source / '.git/HEAD').write_text('ref: refs/heads/main\n')
        self.assertEqual(runtime.source_fingerprint(self.source), present)
        self.annex('sub-example/anat/sub-example_T1w.nii.gz', 'MD5E-s7--y.nii.gz')
        self.assertNotEqual(runtime.source_fingerprint(self.source), present)

    def test_clone_sources_reimport_updates_but_not_during_an_attempt(self):
        self.config['site']['source'] = 'clone'
        self.annex('sub-example/anat/sub-example_T1w.nii.gz', 'MD5E-s7--x.nii.gz')
        self.discovery()
        identity, _ = self.enroll()
        runtime.record(self.engine, identity, 1, 'succeeded')
        self.annex('sub-example/anat/sub-example_T1w.nii.gz', 'MD5E-s7--y.nii.gz')
        self.discovery()
        row = runtime.get_dataset(self.engine, identity)
        self.assertEqual((row['attempt'], row['status']), (2, 'reserved'))
        self.annex('sub-example/anat/sub-example_T1w.nii.gz', 'MD5E-s7--z.nii.gz')
        with self.assertRaisesRegex(ValueError, 'active attempt'):
            runtime.enroll(self.engine, 'nandou', 'dataset', runtime.resolve_settings(self.config, 'dataset', 'Dataset'),
                           'Dataset', allow_update=True)

    def test_renamed_folder_cannot_bypass_import_history(self):
        self.enroll()
        self.source.rename(self.site / 'renamed')
        settings = runtime.resolve_settings(self.config, 'renamed', 'Dataset')
        with self.assertRaisesRegex(ValueError, 'another folder'):
            runtime.enroll(self.engine, 'nandou', 'renamed', settings, 'Dataset')

    def test_one_bad_dataset_does_not_block_an_independent_dataset(self):
        invalid = self.site / 'invalid'
        invalid.mkdir()
        (invalid / 'dataset_description.json').write_text('{invalid JSON')
        with self.assertRaisesRegex(ValueError, 'Discovery failures'):
            self.discovery()
        self.assertEqual(len([kind for kind, _ in self.api.creates if kind == 'jobs']), 1)

    def test_disabled_runtime_cannot_be_triggered(self):
        self.config['site']['bidsImport']['enabled'] = False
        with self.assertRaisesRegex(ValueError, 'disabled'):
            self.discovery()


class DatabaseLockTests(unittest.TestCase):
    def test_project_namespace_is_shared_by_acquisition_and_release(self):
        for name in ('schema', 'import', 'discovery-nandou'):
            with self.subTest(name=name):
                engine = MagicMock()
                engine.dialect.name = 'mysql'
                connection = engine.connect.return_value.__enter__.return_value
                connection.scalar.return_value = 1
                with runtime.database_lock(engine, name, timeout=30) as acquired:
                    self.assertTrue(acquired)
                calls = connection.scalar.call_args_list
                self.assertEqual(len(calls), 2)
                self.assertEqual(str(calls[0].args[0]), 'SELECT GET_LOCK(:name, :timeout)')
                self.assertEqual(calls[0].args[1], {'name': 're-move-loris-' + name, 'timeout': 30})
                self.assertEqual(str(calls[1].args[0]), 'SELECT RELEASE_LOCK(:name)')
                self.assertEqual(calls[1].args[1], {'name': 're-move-loris-' + name})


class MariaDBTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('BIDS_TEST_DATABASE_URL'), 'isolated MariaDB URL not provided')
    def test_schema_and_exclusive_connection_locks(self):
        engine = create_engine(os.environ['BIDS_TEST_DATABASE_URL'])
        self.addCleanup(engine.dispose)
        admin = create_engine(os.environ['BIDS_TEST_ADMIN_DATABASE_URL'])
        self.addCleanup(admin.dispose)
        runtime.initialize(admin)
        runtime.initialize(engine)
        with runtime.database_lock(engine, 'integration-test') as first:
            self.assertTrue(first)
            with runtime.database_lock(engine, 'integration-test') as second:
                self.assertFalse(second)
        with runtime.database_lock(engine, 'integration-test') as released:
            self.assertTrue(released)
        runtime.initialize(engine)


if __name__ == '__main__':
    unittest.main()
