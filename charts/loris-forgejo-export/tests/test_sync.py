"""Tests for files/sync.py.

The git-annex tests run the whole sync against local repositories: a site dataset, a bare "Forgejo" repository, a
fake stage-content dispatch that copies keys from the site (as the site worker does), and a fake Kubernetes API
server that plays the LORIS discovery and import Jobs.
"""

import contextlib
import copy
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))

import registry  # noqa: E402
import sync  # noqa: E402

HAS_ANNEX = shutil.which('git-annex') is not None
KEY = 'SHA256E-s3--' + 'a' * 64 + '.nii.gz'
SETTINGS = {
    'organizationPrefix': 'lfx-', 'botUser': 'lfx-loris-export', 'publicationBotSuffix': '-worker',
    'publicationTeam': 'datasets', 'exportTeam': 'loris-export', 'workerRepository': '.worker',
    'runnerLabel': 'loris-forgejo-worker', 'requestCommand': 'loris-forgejo-request',
    'variablePrefix': 'LORIS_FORGEJO_', 'dataClaim': 'loris-data', 'clonesSubPath': 'forgejo/clones',
    'importCronJobPrefix': 'loris-bids-',
}
IDENTITY = sync.dataset_identity('s', 'ds')
LABEL = 'scilus.net/bids-dataset'


def registry_data(import_enabled=True, enabled=True):
    return {'settings': copy.deepcopy(SETTINGS), 'bidsImport': {'enabled': True}, 'sites': [
        {'name': 's', 'enabled': enabled, 'source': 'pvc', 'claimName': 'loris-data', 'subPath': 'forgejo/clones/s',
         'forgejo': {'enabled': True, 'datasets': ['ds', 'other']},
         'bidsImport': {'enabled': import_enabled, 'symlinkedContent': 'identity', 'updates': 'additive'}}]}


def site(**options):
    return registry.parse(registry_data(**options)).sites[0]


# Fake Kubernetes API ----------------------------------------------------------------------------------------------

class FakeCluster:
    """Jobs finish after `polls` reads; POSTed discovery Jobs follow `scenario`."""

    def __init__(self, scenario='import'):
        self.scenario = scenario
        self.jobs = {}
        self.logs = {}
        self.created = []
        self.authorization = []
        self.on_discovery = None
        self.earlier_finished_at_discovery = None

    def add_job(self, name, labels=None, state='Complete', polls=0, log=''):
        self.jobs[name] = {'metadata': {'name': name, 'labels': labels or {}}, 'state': state, 'polls': polls}
        self.logs[name] = log

    def read(self, name):
        job = self.jobs[name]
        if job['polls'] > 0:
            job['polls'] -= 1
            conditions = []
        else:
            conditions = [{'type': job['state'], 'status': 'True'}]
        return {'metadata': job['metadata'], 'status': {'conditions': conditions}}

    def discover(self, body):
        name = body['metadata']['name']
        self.created.append(body)
        self.earlier_finished_at_discovery = all(not job['polls'] for job in self.jobs.values()
                                                 if job['metadata']['labels'].get(LABEL) == IDENTITY[:32])
        if self.on_discovery:
            self.on_discovery()
        line = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))
        dispatch = line({'dataset': IDENTITY, 'attempt': 2, 'job': 'loris-bids-s-ds-imp-2'})
        other = line({'dataset': 'f' * 64, 'attempt': 1, 'job': 'loris-bids-s-other-1'})
        scenario = self.scenario
        log = {'import': f'{other}\nnot json\n{dispatch}\n', 'deferred': dispatch + '\n', 'import-failed': dispatch,
               'active': line({'discovery': 'already-active', 'site': 's'}) + '\n',
               'nothing': other + '\n', 'running': '', 'discovery-failed': dispatch}[scenario]
        self.add_job(name, state='Failed' if scenario == 'discovery-failed' else 'Complete', polls=1, log=log)
        result = line({'dataset': IDENTITY, 'attempt': 2, 'result': {'mode': 'import', 'scans': 1}})
        if scenario in ('import', 'deferred', 'import-failed'):
            self.add_job('loris-bids-s-ds-imp-2', {LABEL: IDENTITY[:32]}, polls=2,
                         state='Failed' if scenario == 'import-failed' else 'Complete',
                         log=result + '\n' if scenario == 'import' else '')
        if scenario == 'running':
            self.add_job('loris-bids-s-ds-imp-1', {LABEL: IDENTITY[:32]}, polls=10 ** 6)

    def handle(self, method, path, query, body):
        if path.startswith('/apis/batch/v1/namespaces/ns/'):
            resource = path[len('/apis/batch/v1/namespaces/ns/'):]
            if method == 'GET' and resource == 'cronjobs/loris-bids-s':
                return 200, {'spec': {'jobTemplate': {'metadata': {'labels': {'app.kubernetes.io/name': 'loris-bids'}},
                                                      'spec': {'template': {'spec': {}}}}}}
            if method == 'GET' and resource == 'jobs':
                key, _, value = query['labelSelector'][0].partition('=')
                return 200, {'items': [self.read(name) for name, job in list(self.jobs.items())
                                       if job['metadata']['labels'].get(key) == value]}
            if method == 'POST' and resource == 'jobs':
                self.discover(body)
                return 201, body
            if method == 'GET' and resource.startswith('jobs/') and resource[5:] in self.jobs:
                return 200, self.read(resource[5:])
        if path.startswith('/api/v1/namespaces/ns/pods'):
            if method == 'GET' and path.endswith('/pods'):
                job = query['labelSelector'][0].removeprefix('job-name=')
                pods = [{'metadata': {'name': job + '-pod'}}] if job in self.logs else []
                return 200, {'items': pods}
            if method == 'GET' and path.endswith('/log'):
                job = path.split('/')[-2].removesuffix('-pod')
                return 200, self.logs[job]
        return 404, {'message': 'not found'}


class FakeKubernetesServer:
    def __init__(self, cluster):
        class Handler(BaseHTTPRequestHandler):
            def do(handler, method):
                url = urllib.parse.urlsplit(handler.path)
                length = int(handler.headers.get('Content-Length') or 0)
                body = json.loads(handler.rfile.read(length)) if length else None
                cluster.authorization.append(handler.headers.get('Authorization'))
                status, payload = cluster.handle(method, url.path, urllib.parse.parse_qs(url.query), body)
                data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                handler.send_response(status)
                handler.send_header('Content-Length', str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)

            def do_GET(handler):
                handler.do('GET')

            def do_POST(handler):
                handler.do('POST')

            def log_message(handler, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ClusterTest(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        self.server = FakeKubernetesServer(self.cluster)
        self.addCleanup(self.server.close)
        self.importer = sync.Importer(sync.Kubernetes(self.server.url, 'ns'), timeout=30, poll=0)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def events(self):
        return [json.loads(line)['event'] for line in self.output.getvalue().splitlines()]


class ImporterTest(ClusterTest):
    def run_importer(self, scenario):
        self.cluster.scenario = scenario
        return self.importer.run(site(), 'ds')

    def test_acknowledged_by_import_result_line(self):
        self.assertTrue(self.run_importer('import'))
        job = self.cluster.created[0]
        name = job['metadata']['name']
        self.assertRegex(name, r'^loris-bids-s-lfx-[0-9a-f]{8}$')
        self.assertEqual(job['metadata']['annotations']['loris-forgejo-export/triggered-by'], 'sync')
        self.assertEqual(job['metadata']['labels'], {'app.kubernetes.io/name': 'loris-bids'})
        self.assertEqual(job['spec'], {'template': {'spec': {}}})
        self.assertIn('import-acknowledged', self.events())

    def test_deferred_import_is_pending(self):
        self.assertFalse(self.run_importer('deferred'))
        self.assertIn('import-deferred', self.events())

    def test_already_active_discovery_is_pending(self):
        self.assertFalse(self.run_importer('active'))
        self.assertIn('discovery-already-active', self.events())

    def test_no_dispatch_and_nothing_unfinished_is_acknowledged(self):
        self.assertTrue(self.run_importer('nothing'))
        self.assertIn('import-not-needed', self.events())

    def test_no_dispatch_but_unfinished_import_is_pending(self):
        self.assertFalse(self.run_importer('running'))

    def test_earlier_unfinished_import_is_waited_for(self):
        self.cluster.add_job('loris-bids-s-ds-imp-1', {LABEL: IDENTITY[:32]}, state='Failed', polls=3)
        self.cluster.add_job('unrelated', {LABEL: 'x' * 32}, polls=10 ** 6)
        self.assertTrue(self.run_importer('nothing'))
        self.assertTrue(self.cluster.earlier_finished_at_discovery)
        events = self.events()
        self.assertLess(events.index('import-waiting'), events.index('discovery-started'))

    def test_discovery_failure_is_an_error(self):
        with self.assertRaisesRegex(sync.SyncError, 'discovery Job .* failed'):
            self.run_importer('discovery-failed')

    def test_failed_import_is_an_error(self):
        with self.assertRaisesRegex(sync.SyncError, 'import Job loris-bids-s-ds-imp-2 failed'):
            self.run_importer('import-failed')

    def test_discovery_job_name_is_bounded(self):
        long = site()
        settings = dict(SETTINGS, importCronJobPrefix='a' * 50)
        long = registry.Site(**{**long.__dict__, 'settings': registry.Settings(**settings)})
        name = sync.Importer.discovery_name(long)
        self.assertLessEqual(len(name), sync.JOB_NAME_MAX)
        self.assertRegex(name, r'^a+-lfx-[0-9a-f]{8}$')

    def test_service_account_token_is_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('sa-token\n')
            sync.Kubernetes(self.server.url, 'ns', token).request('GET', 'cronjobs/loris-bids-s')
        self.assertEqual(self.cluster.authorization[-1], 'Bearer sa-token')


# Helpers and main ------------------------------------------------------------------------------------------------

class HelperTest(unittest.TestCase):
    def test_annex_key(self):
        cases = [(f'../../.git/annex/objects/Xx/Yy/{KEY}/{KEY}'.encode(), KEY), (f'/annex/objects/{KEY}\n'.encode(), KEY),
                 (b'../README', None), (b'/annex/objects/not a key', None), (b'\xff\xfe', None)]
        for target, key in cases:
            self.assertEqual(sync.annex_key(target), key)

    def test_metadata_is_kept(self):
        for path, metadata in [('sub-01/anat/sub-01_T1w.json', True), ('participants.tsv', True), ('README', True),
                               ('sub-01/eeg/x_eeg.vhdr', True), ('sub-01/anat/sub-01_T1w.nii.gz', False),
                               ('sub-01/eeg/x_eeg.eeg', False)]:
            self.assertIs(sync.is_metadata(path), metadata, path)

    def test_history_transition_requires_complete_exact_commit_inputs(self):
        for values in ({'LFX_HISTORY_REPOSITORY': 'lfx-s/ds'},
                       {'LFX_HISTORY_OLD': '0' * 40, 'LFX_HISTORY_NEW': '1' * 40},
                       {'LFX_HISTORY_REPOSITORY': 'lfx-s/ds', 'LFX_HISTORY_OLD': 'short', 'LFX_HISTORY_NEW': '1' * 40}):
            with mock.patch.dict(os.environ, {'LFX_FORGEJO_URL': '/forge', **values}):
                with self.assertRaisesRegex(sync.SyncError, 'history transition requires'):
                    sync.configuration()

    def test_configuration_derives_annex_url(self):
        with mock.patch.dict(os.environ, {'LFX_FORGEJO_URL': 'http://127.0.0.1:3000/', 'LFX_ANNEX_URL': ''}):
            config = sync.configuration()
        self.assertEqual(config['annex_url'], 'annex+http://127.0.0.1:3000/git-annex-p2phttp')
        self.assertEqual(config['annex_ips'], '127.0.0.1')

    def test_targets(self):
        reg = registry.parse(registry_data())
        self.assertEqual([(s.name, d) for s, d in sync.targets(reg, None)], [('s', 'ds'), ('s', 'other')])
        self.assertEqual([(s.name, d) for s, d in sync.targets(reg, 'lfx-s/ds')], [('s', 'ds')])
        self.assertIsNone(sync.targets(reg, 'lfx-s/.worker'))
        self.assertEqual(sync.targets(registry.parse(registry_data(enabled=False)), None), [])

    def test_credential_helper_answers_from_environment_only(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'username').write_text('bot\n')
            (Path(directory) / 'token').write_text('s3cret\n')
            with mock.patch.dict(os.environ, {'LFX_BOT_DIR': directory}):
                env = sync.git_auth_env()
            output = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=forge.invalid\n\n',
                                    env=env, capture_output=True, text=True, check=True).stdout
        self.assertIn('username=bot\n', output)
        self.assertIn('password=s3cret\n', output)

    def test_ledger_missing_or_uninitialized_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.sqlite'
            self.assertEqual(sync.ledger_keys(str(path), 's', 'ds'), set())
            sqlite3.connect(path).close()
            self.assertEqual(sync.ledger_keys(str(path), 's', 'ds'), set())


class MainTest(unittest.TestCase):
    def main(self, environment):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'registry.json'
            path.write_text(json.dumps(registry_data()))
            (Path(directory) / 'bot').mkdir()
            env = {'LFX_REGISTRY': str(path), 'LFX_BOT_DIR': str(Path(directory) / 'bot'),
                   'LFX_FORGEJO_URL': 'http://127.0.0.1:9', 'LFX_SYNC_REPOSITORY': ''}
            env.update(environment)
            output = io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(output), \
                    mock.patch.object(sync, 'CLONES', Path(directory) / 'clones'):
                code = sync.main(['sync.py'])
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_missing_bot_credentials_exit_1(self):
        code, lines = self.main({'LFX_SYNC_REPOSITORY': 'lfx-s/ds'})
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]['error'], sync.CREDENTIALS_MISSING)

    def test_unmanaged_repository_exit_1(self):
        for repository in ('lfx-s/.worker', 'lfx-x/ds', 'other/thing'):
            code, lines = self.main({'LFX_SYNC_REPOSITORY': repository})
            self.assertEqual(code, 1)
            self.assertEqual(lines[-1]['error'], 'not a managed dataset repository')


# End to end with git-annex ----------------------------------------------------------------------------------------

def git(cwd, *args):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f'git {" ".join(args)}: {result.stderr}'
    return result.stdout.strip()


class FakeForgejo:
    """Stage-content dispatch: copy keys from the site dataset to the bare Forgejo repository."""

    def __init__(self, site_path, on_stage=None):
        self.site, self.staged, self.on_stage = site_path, [], on_stage

    def annex_uuid(self, org, dataset):
        return git(Path(git(self.site, 'remote', 'get-url', 'forge')), 'config', '--get', 'annex.uuid')

    def stage(self, managed, dataset, commit, keys, timeout, poll):
        assert managed.org == 'lfx-s' and dataset == 'ds' and len(keys) <= sync.BATCH
        git(self.site, 'cat-file', '-e', commit + '^{commit}')
        if self.on_stage:
            self.on_stage()
        self.staged.append(sorted(keys))
        subprocess.run(['git', 'annex', 'copy', '--to', 'forge', '--batch-keys'], cwd=self.site, check=True,
                       input='\n'.join(keys) + '\n', capture_output=True, text=True)


def write_subject(path, subject):
    anat = path / subject / 'anat'
    anat.mkdir(parents=True)
    (anat / f'{subject}_T1w.nii.gz').write_bytes(os.urandom(128))
    (anat / f'{subject}_T1w.json').write_text('{"EchoTime": 0.003}')


def share(path):
    """What site publication does: git only, after merging Forgejo's git-annex branch."""
    git(path, 'fetch', '-q', 'forge', '+refs/heads/git-annex:refs/remotes/forge/git-annex')
    git(path, 'annex', 'merge', '-q')
    git(path, 'push', '-q', 'forge', 'main', 'git-annex')


@unittest.skipUnless(HAS_ANNEX, 'git-annex is not installed')
class EndToEndTest(ClusterTest):
    def setUp(self):
        super().setUp()
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bot = self.tmp / 'bot'
        bot.mkdir()
        (bot / 'username').write_text('lfx-loris-export')
        (bot / 'token').write_text('bot-token-value')
        self.enterContext(mock.patch.dict(os.environ, {
            'HOME': str(self.tmp), 'GIT_CONFIG_NOSYSTEM': '1', 'LFX_BOT_DIR': str(bot),
            'GIT_CONFIG_PARAMETERS': (os.environ.get('GIT_CONFIG_PARAMETERS', '')
                                      + " 'safe.bareRepository'='all'").strip()}))
        git(self.tmp, 'config', '--global', 'user.name', 't')
        git(self.tmp, 'config', '--global', 'user.email', 't@t.invalid')
        git(self.tmp, 'config', '--global', 'init.defaultBranch', 'main')
        self.site_path = self.tmp / 'site'
        self.site_path.mkdir()
        git(self.site_path, 'init', '-q')
        git(self.site_path, 'annex', 'init', '-q', 'site storage')
        write_subject(self.site_path, 'sub-01')
        (self.site_path / 'dataset_description.json').write_text('{"Name": "ds", "BIDSVersion": "1.9.0"}')
        (self.site_path / 'derivatives').mkdir()
        (self.site_path / 'derivatives' / 'big.nii.gz').write_bytes(os.urandom(64))
        git(self.site_path, 'annex', 'add', '-q', '.')
        git(self.site_path, 'commit', '-qm', 'initial')
        self.forge = self.tmp / 'forge' / 'lfx-s' / 'ds.git'
        self.forge.parent.mkdir(parents=True)
        git(self.tmp, 'clone', '-q', '--bare', str(self.site_path), str(self.forge))
        git(self.forge, 'annex', 'init', '-q', 'forgejo')
        git(self.site_path, 'remote', 'add', 'forge', str(self.forge))
        share(self.site_path)
        self.config = {'forgejo_url': str(self.tmp / 'forge'), 'annex_url': '', 'annex_ips': '',
                       'clones': str(self.tmp / 'c'), 'ledger': str(self.tmp / 'c' / '.cache-ledger.sqlite'),
                       'stage_timeout': 10, 'import_timeout': 30, 'poll': 0, 'history_transition': None}
        self.clone = self.tmp / 'c' / 's' / 'ds'
        self.imported_view = []
        self.cluster.on_discovery = self.record_view

    def record_view(self):
        # LORIS reads the work tree: every image to import must be published with its content.
        self.imported_view.append(sorted(str(p.relative_to(self.clone)) for p in self.clone.rglob('*.nii.gz')
                                         if p.exists() and '.git' not in p.parts))

    def job(self, import_enabled=True, on_stage=None):
        forgejo = FakeForgejo(self.site_path, on_stage)
        return sync.DatasetSync(site(import_enabled=import_enabled), 'ds', self.config, forgejo, self.importer), forgejo

    def present(self):
        return sorted(git(self.clone, 'annex', 'find', '--in', 'here').splitlines())

    def forge_objects(self):
        return sorted(p.name for p in (self.forge / 'annex' / 'objects').rglob('*') if p.is_file())

    def imported(self):
        return subprocess.run(['git', 'rev-parse', '--verify', '-q', sync.IMPORTED_REF], cwd=self.clone,
                              capture_output=True, text=True).stdout.strip()

    def test_first_sync_imports_then_keeps_only_metadata_and_resync_does_not_refetch(self):
        job, forgejo = self.job(on_stage=lambda: self.assertFalse(self.clone.exists()))
        result = job.run()
        self.assertEqual((result['status'], result['keys']), ('imported', 3))
        self.assertEqual([len(keys) for keys in forgejo.staged], [3])  # derivatives are never staged
        self.assertEqual(self.imported_view, [['sub-01/anat/sub-01_T1w.nii.gz']])
        self.assertEqual(self.imported(), git(self.site_path, 'rev-parse', 'HEAD'))
        self.assertEqual(git(self.clone, 'config', 'annex.private'), 'true')
        self.assertEqual(self.present(), ['dataset_description.json', 'sub-01/anat/sub-01_T1w.json'])
        self.assertEqual(self.forge_objects(), [])
        self.assertFalse((self.clone.parent / '.ds.cloning').exists())
        self.assertNotIn('bot-token-value', (self.clone / '.git' / 'config').read_text())
        self.assertNotIn('bot-token-value', self.output.getvalue())

        job, forgejo = self.job()
        self.assertEqual(job.run()['status'], 'up-to-date')
        self.assertEqual(forgejo.staged, [])
        self.assertEqual(len(self.cluster.created), 1)
        self.assertEqual(self.present(), ['dataset_description.json', 'sub-01/anat/sub-01_T1w.json'])

    def test_update_hydrates_before_checkout_and_does_not_refetch_dropped_content(self):
        self.job()[0].run()
        old = git(self.clone, 'rev-parse', 'HEAD')
        write_subject(self.site_path, 'sub-02')
        git(self.site_path, 'annex', 'add', '-q', 'sub-02')
        git(self.site_path, 'commit', '-qm', 'add sub-02')
        share(self.site_path)
        heads = []
        job, forgejo = self.job(on_stage=lambda: heads.append(git(self.clone, 'rev-parse', 'HEAD')))
        original_get = job.get

        def get(keys):
            heads.append(git(self.clone, 'rev-parse', 'HEAD'))
            self.assertFalse((self.clone / 'sub-02').exists())
            original_get(keys)
        job.get = get
        self.assertEqual(job.run()['keys'], 2)
        self.assertEqual(heads, [old, old])
        self.assertEqual([len(keys) for keys in forgejo.staged], [1])  # sub-01's image is not fetched again
        self.assertEqual(self.imported_view[-1], ['sub-02/anat/sub-02_T1w.nii.gz'])
        self.assertEqual(self.forge_objects(), [])
        self.assertEqual(self.present(), ['dataset_description.json', 'sub-01/anat/sub-01_T1w.json',
                                          'sub-02/anat/sub-02_T1w.json'])

    def test_failed_drop_after_acknowledgement_is_retried(self):
        job, _ = self.job()
        original_drop = job.drop

        def failing_drop(keys, local):
            original_drop(keys, local=False)
            raise sync.SyncError('transient drop failure')
        job.drop = failing_drop
        with self.assertRaises(sync.SyncError):
            job.run()
        self.assertNotEqual(self.imported(), '')
        self.assertIn('sub-01/anat/sub-01_T1w.nii.gz', self.present())

        job, _ = self.job()
        self.assertEqual(job.run()['status'], 'up-to-date')
        self.assertEqual(self.present(), ['dataset_description.json', 'sub-01/anat/sub-01_T1w.json'])

    def test_deferred_import_is_pending_and_keeps_local_content(self):
        self.cluster.scenario = 'deferred'
        job, _ = self.job()
        self.assertEqual(job.run()['status'], 'import-pending')
        self.assertIn('import-pending', self.events())
        self.assertEqual(self.imported(), '')
        self.assertIn('sub-01/anat/sub-01_T1w.nii.gz', self.present())
        self.assertEqual(self.forge_objects(), [])  # transit copies are dropped anyway

        self.cluster.scenario = 'import'
        job, forgejo = self.job()
        self.assertEqual(job.run()['status'], 'imported')
        self.assertEqual(forgejo.staged, [])
        self.assertEqual(self.present(), ['dataset_description.json', 'sub-01/anat/sub-01_T1w.json'])

    def test_already_active_discovery_is_pending(self):
        self.cluster.scenario = 'active'
        self.assertEqual(self.job()[0].run()['status'], 'import-pending')
        self.assertEqual(self.imported(), '')
        self.assertIn('sub-01/anat/sub-01_T1w.nii.gz', self.present())

    def test_discovery_failure_fails_and_keeps_local_content(self):
        self.cluster.scenario = 'discovery-failed'
        with self.assertRaisesRegex(sync.SyncError, 'discovery'):
            self.job()[0].run()
        self.assertEqual(self.imported(), '')
        self.assertIn('sub-01/anat/sub-01_T1w.nii.gz', self.present())
        self.assertEqual(self.forge_objects(), [])

    def test_ledger_rows_and_content_without_site_copy_are_kept(self):
        image = 'sub-01/anat/sub-01_T1w.nii.gz'
        key = git(self.site_path, 'annex', 'lookupkey', image)
        self.clone.parent.mkdir(parents=True)
        with sqlite3.connect(self.config['ledger']) as connection:
            connection.execute('CREATE TABLE content (site TEXT, dataset TEXT, key TEXT, size INTEGER, '
                               'fetched REAL, accessed REAL, PRIMARY KEY (site, dataset, key))')
            connection.execute('INSERT INTO content VALUES (?, ?, ?, 1, 0, 0)', ('s', 'ds', key))
        job, _ = self.job()
        job.run()
        self.assertIn(image, self.present())
        orphan = self.clone / 'orphan.nii.gz'
        orphan.write_bytes(os.urandom(32))
        git(self.clone, 'annex', 'add', '-q', 'orphan.nii.gz')
        job.drop({git(self.clone, 'annex', 'lookupkey', 'orphan.nii.gz'): 'orphan.nii.gz'}, local=True)
        self.assertIn('orphan.nii.gz', self.present())

    def rewrite(self):
        git(self.site_path, 'checkout', '-q', '--orphan', 'rewrite')
        git(self.site_path, 'commit', '-qm', 'rewritten')
        git(self.site_path, 'push', '-q', '--force', 'forge', 'rewrite:main')
        return git(self.site_path, 'rev-parse', 'HEAD')

    def test_rewritten_history_is_refused(self):
        self.job()[0].run()
        self.rewrite()
        with self.assertRaisesRegex(sync.SyncError, 'history was rewritten'):
            self.job()[0].run()

    def test_approved_history_transition(self):
        self.job()[0].run()
        old = self.imported()
        new = self.rewrite()
        self.config['history_transition'] = {'repository': 'lfx-s/ds', 'old': old, 'new': new}
        self.assertEqual(self.job()[0].run()['status'], 'imported')
        self.assertEqual(self.imported(), new)
        self.assertEqual(git(self.clone, 'rev-parse', 'HEAD'), new)
        self.assertEqual(git(self.clone, 'rev-parse', sync.BACKUP_REFS + old), old)

    def test_sites_without_import_only_fetch(self):
        job, forgejo = self.job(import_enabled=False)
        self.assertEqual(job.run()['status'], 'fetched')
        self.assertEqual((forgejo.staged, self.cluster.created), ([], []))
        self.assertTrue((self.clone / 'dataset_description.json').is_symlink())


if __name__ == '__main__':
    unittest.main()
