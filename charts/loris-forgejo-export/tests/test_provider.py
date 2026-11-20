import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))

import provider  # noqa: E402
from test_registry import data, site  # noqa: E402


def annex_missing() -> str | None:
    try:
        result = subprocess.run(['git', 'annex', 'version'], capture_output=True)
    except OSError:
        return 'git is not installed'
    return None if result.returncode == 0 else 'git-annex is not installed'


NO_ANNEX = annex_missing()
needs_annex = unittest.skipIf(NO_ANNEX, NO_ANNEX or '')
KEY = 'SHA256E-s100--' + 'b' * 64 + '.nii.gz'
IMAGE = 'sub-01/anat/sub-01_T1w.nii.gz'
EEG = 'sub-01/eeg/sub-01_task-rest_eeg'
DIGEST = hashlib.sha256(f'{EEG}.vhdr'.encode()).hexdigest()
CHUNK_PATH = f'content-provider-chunks/s/ds/{DIGEST}.chunks'
FAKE_CHUNKER = r'''
import json, pathlib, sys
recording, destination = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[3])
if (recording.parent / 'fail').exists():
    sys.exit('Traceback\nValueError: broken recording')
header = recording.read_text()
companions = [line.split('=', 1)[1] for line in header.splitlines() if line.startswith(('DataFile', 'MarkerFile'))]
out = destination / (recording.stem + '.chunks')
out.mkdir(parents=True)
(out / 'index.json').write_text(json.dumps({'read': [(recording.parent / name).read_text() for name in companions]}))
(out / 'raw').mkdir()
(out / 'raw' / '0.buf').write_bytes(b'x' * 10)
'''


def git(cwd, *args, stdin=None):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, input=stdin)
    assert result.returncode == 0, f'git {" ".join(args)}: {result.stderr}'
    return result.stdout.strip()


def wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('timed out')
        time.sleep(0.05)


def serve(test, handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f'http://127.0.0.1:{server.server_address[1]}'


def post(url, payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method='POST', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


class FakeForgejo(BaseHTTPRequestHandler):
    """stage-content dispatch: the 'site runner' copies keys from the site dataset to the bare Forgejo repository."""

    world = None

    def reply(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        world = self.world
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'null')
        assert self.headers['Authorization'] == 'Basic ' + b64encode(b'export-bot:bot-token').decode()
        prefix = '/api/v1/repos/lfx-s/.worker/actions'
        if self.path == prefix + '/workflows/stage-content.yml/dispatches':
            assert body['ref'] == 'main' and body['return_run_info'] is True
            assert body['inputs']['dataset'] == 'ds'
            git(world['site'], 'cat-file', '-e', body['inputs']['commit'] + '^{commit}')
            world['staged'].append(sorted(body['inputs']['keys'].split()))
            return self.reply(200, {'id': len(world['staged'])})
        if self.path.endswith('/cancel'):
            return self.reply(200, {})
        self.reply(404, {})

    def do_GET(self):
        world = self.world
        run = int(self.path.rsplit('/', 1)[-1])
        if world['hold'].is_set():
            return self.reply(200, {'status': 'waiting'})
        if run not in world['done']:
            keys = world['staged'][run - 1]
            subprocess.run(['git', 'annex', 'copy', '--to', 'forge', '--batch-keys'], cwd=world['site'],
                           input='\n'.join(keys) + '\n', capture_output=True, text=True, check=True)
            share(world['site'])
            world['done'].add(run)
        self.reply(200, {'status': 'success'})

    def log_message(self, *args):
        pass


def share(site_path):
    """What site publication does: push git and the location log, never content."""
    git(site_path, 'fetch', '-q', 'forge', '+refs/heads/git-annex:refs/remotes/forge/git-annex')
    git(site_path, 'annex', 'merge', '-q')
    git(site_path, 'push', '-q', 'forge', 'main', 'git-annex')


def present(clone):
    return sorted(git(clone, 'annex', 'find', '--in', 'here').splitlines())


def forge_objects(forge):
    return sorted(p.name for p in (forge / 'annex' / 'objects').rglob('*') if p.is_file())


def remove_tree(path: Path):
    # git-annex makes object directories read-only.
    for root, dirs, _ in os.walk(path):
        for name in dirs:
            os.chmod(os.path.join(root, name), 0o755)
    shutil.rmtree(path, ignore_errors=True)


class ProviderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(remove_tree, self.tmp)
        self.logs = []
        log = mock.patch.object(provider, 'log', lambda **fields: self.logs.append(fields))
        log.start()
        self.addCleanup(log.stop)
        params = os.environ.get('GIT_CONFIG_PARAMETERS', '')
        env = mock.patch.dict(os.environ, {
            'HOME': str(self.tmp), 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_PARAMETERS': f"{params} 'safe.bareRepository'='all'".strip()})
        env.start()
        self.addCleanup(env.stop)
        (self.tmp / 'registry.json').write_text(json.dumps(data(site(
            's', siteAlias='S', subPath='forgejo/clones/s', forgejo={'enabled': True, 'datasets': ['ds', 'other']}))))
        self.bot = self.tmp / 'bot'
        self.bot.mkdir()
        (self.bot / 'username').write_text('export-bot\n')
        (self.bot / 'token').write_text('bot-token\n')
        (self.tmp / 'chunker.py').write_text(FAKE_CHUNKER)
        (self.tmp / 'clones').mkdir()
        (self.tmp / 'provider-chunks').mkdir()
        self.world = {'staged': [], 'done': set(), 'hold': threading.Event()}
        FakeForgejo.world = self.world
        self.forgejo_url = serve(self, FakeForgejo)

    def make_provider(self, **overrides):
        config = provider.configuration({'LFX_FORGEJO_URL': self.forgejo_url})
        config.update(annex_url='', bot_dir=str(self.bot), clones=str(self.tmp / 'clones'),
                      provider_chunks=str(self.tmp / 'provider-chunks'),
                      ledger=str(self.tmp / 'clones' / '.cache-ledger.sqlite'), batch_window=0.05, poll=0,
                      stage_timeout=30, chunk_timeout=30,
                      chunkers={'.vhdr': [sys.executable, str(self.tmp / 'chunker.py')]})
        config.update(overrides)
        store = provider.Provider(config, provider.Forgejo(self.forgejo_url, str(self.bot)),
                                  str(self.tmp / 'registry.json'))
        self.addCleanup(store.ledger.connection.close)

        def idle():
            deadline = time.monotonic() + 30
            while (store.pending or store.inflight or store.pinned) and time.monotonic() < deadline:
                time.sleep(0.05)
        self.addCleanup(idle)
        provider.Handler.provider = store
        self.url = serve(self, provider.Handler)
        return store

    def build_world(self):
        """A site dataset, a bare 'Forgejo' repository and the provider's dataset copy holding metadata only."""
        git(self.tmp, 'config', '--global', 'user.name', 't')
        git(self.tmp, 'config', '--global', 'user.email', 't@t.invalid')
        git(self.tmp, 'config', '--global', 'init.defaultBranch', 'main')
        site_path = self.tmp / 'site'
        (site_path / 'sub-01' / 'anat').mkdir(parents=True)
        (site_path / 'sub-01' / 'eeg').mkdir()
        git(site_path, 'init', '-q')
        git(site_path, 'annex', 'init', '-q', 'site storage')
        (site_path / IMAGE).write_bytes(os.urandom(128))
        (site_path / 'sub-01/anat/sub-01_T1w.json').write_text('{"EchoTime": 0.003}')
        (site_path / f'{EEG}.vhdr').write_text(
            '[Common Infos]\nDataFile=sub-01_task-rest_eeg.eeg\nMarkerFile=sub-01_task-rest_eeg.vmrk\n')
        (site_path / f'{EEG}.vmrk').write_text('markers')
        (site_path / f'{EEG}.eeg').write_text('signal')
        git(site_path, 'annex', 'add', '-q', IMAGE, f'{EEG}.eeg')
        git(site_path, 'add', '.')
        git(site_path, 'commit', '-qm', 'initial')
        forge = self.tmp / 'forge' / 'lfx-s' / 'ds.git'
        forge.parent.mkdir(parents=True)
        git(self.tmp, 'clone', '-q', '--bare', str(site_path), str(forge))
        git(forge, 'annex', 'init', '-q', 'forgejo')
        git(site_path, 'remote', 'add', 'forge', str(forge))
        share(site_path)
        clone = self.tmp / 'clones' / 's' / 'ds'
        git(self.tmp, 'clone', '-q', str(forge), str(clone))
        git(clone, 'config', 'annex.private', 'true')
        git(clone, 'annex', 'init', '-q', 'provider copy')
        git(clone, 'config', 'remote.origin.annex-uuid', git(forge, 'config', 'annex.uuid'))
        self.world.update(site=site_path, forge=forge, clone=clone)
        return clone

    def key(self, path):
        return git(self.world['clone'], 'annex', 'lookupkey', path)


class LedgerTest(ProviderCase):
    def test_ddl_is_the_contract(self):
        ledger = provider.Ledger(str(self.tmp / 'clones' / '.cache-ledger.sqlite'))
        self.addCleanup(ledger.connection.close)
        connection = sqlite3.connect(self.tmp / 'clones' / '.cache-ledger.sqlite')
        self.addCleanup(connection.close)
        tables = dict(connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"))
        normalize = lambda text: ' '.join(text.replace('IF NOT EXISTS ', '').split()).rstrip(';')  # noqa: E731
        self.assertEqual({name: normalize(sql) for name, sql in tables.items()},
                         {statement.split()[5]: normalize(statement)
                          for statement in provider.LEDGER_DDL.strip().split(';\n') if statement.strip()})
        self.assertEqual(set(tables), {'content', 'provider_chunks'})
        self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_expiry_across_content_and_chunks(self):
        ledger = provider.Ledger(str(self.tmp / 'ledger.sqlite'))
        self.addCleanup(ledger.connection.close)
        key = lambda n, size: f'SHA256E-s{size}--{n:064d}.nii.gz'  # noqa: E731
        ledger.record('content', 's', 'ds', [(key(1, 10), 10)], now=0)        # too old
        ledger.record('content', 's', 'ds', [(key(2, 10), 10)], now=900)      # idle
        ledger.record('content', 's', 'ds', [(key(3, 60), 60)], now=950)     # least recently used above the cap
        ledger.record('provider_chunks', 's', 'ds', [('a' * 64, 50)], now=990)
        self.assertTrue(ledger.touch('content', 's', 'ds', key(1, 10), 999))
        self.assertFalse(ledger.touch('provider_chunks', 's', 'ds', 'c' * 64, 999))
        expired = [(row[2], row[-1]) for row in ledger.expired(1000, idle=50, max_age=500, max_bytes=100)]
        self.assertEqual(expired, [(key(2, 10), 'content'), (key(1, 10), 'content'), (key(3, 60), 'content')])
        ledger.remove('content', 's', 'ds', [row[0] for row in expired])
        self.assertEqual(ledger.rows(), [])
        self.assertEqual([row[2] for row in ledger.rows('provider_chunks')], ['a' * 64])

    def test_key_size(self):
        self.assertEqual(provider.key_size(KEY), 100)
        self.assertEqual(provider.key_size('MD5E-s12-m1700000000--' + 'c' * 32), 12)
        self.assertEqual(provider.key_size('URL--https&c%%x'), 0)


class HelpersTest(unittest.TestCase):
    def test_brainvision_companions(self):
        cases = [
            ('[Common Infos]\nDataFile=r.eeg\nMarkerFile=r.vmrk\n[Binary Infos]\nDataFile=x\n', None),
            ('[Common Infos]\nDataFile=../r.eeg\nMarkerFile=r.vmrk\n', 'without valid'),
            ('[Common Infos]\nMarkerFile=r.vmrk\n[Other]\n', None),
            ('[Common Infos]\nCodepage=UTF-8\n', 'without valid'),
            ('[Common Infos]\nDataFile=gone.eeg\n', 'gone.eeg is missing'),
        ]
        for header, error in cases:
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'eeg').mkdir()
                (root / 'eeg' / 'r.vhdr').write_text(header)
                (root / 'eeg' / 'r.eeg').write_bytes(b'')
                (root / 'eeg' / 'r.vmrk').write_text('')
                if error:
                    with self.assertRaisesRegex(provider.ProviderError, error):
                        provider.companions(root, 'eeg/r.vhdr')
                else:
                    expected = ['eeg/r.eeg', 'eeg/r.vmrk'] if 'DataFile=r.eeg' in header else ['eeg/r.vmrk']
                    self.assertEqual(provider.companions(root, 'eeg/r.vhdr'), expected)

    def test_eeglab_and_edf_companions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'a.set').write_bytes(b'')
            self.assertEqual(provider.companions(root, 'a.set'), [])
            (root / 'a.fdt').write_bytes(b'')
            self.assertEqual(provider.companions(root, 'a.set'), ['a.fdt'])
            self.assertEqual(provider.companions(root, 'a.edf'), [])

    def test_chunker_by_extension(self):
        self.assertEqual(provider.CHUNKERS['.edf'], ['edf-to-chunks'])
        self.assertEqual(provider.CHUNKERS['.set'], ['eeglab-to-chunks'])
        for suffix in ('.vhdr', '.bdf'):
            self.assertEqual(Path(provider.CHUNKERS[suffix][-1]).name, 'ephys_chunks.py')
            self.assertTrue(Path(provider.CHUNKERS[suffix][-1]).is_file())

    def test_configuration_reads_lfx_environment(self):
        config = provider.configuration({'LFX_FORGEJO_URL': 'http://forgejo:3000/', 'LFX_CACHE_RETRY_AFTER': '7',
                                         'LFX_BOT_DIR': '/bot', 'LFX_CACHE_MAX_BYTES': '10'})
        self.assertEqual(config['annex_url'], 'annex+http://forgejo:3000/git-annex-p2phttp')
        self.assertEqual((config['retry_after'], config['bot_dir'], config['max_bytes']), (7, '/bot', 10))
        self.assertEqual(config['ledger'], '/clones/.cache-ledger.sqlite')
        self.assertEqual(provider.configuration({'LFX_ANNEX_URL': 'annex+http://x/p'})['annex_url'],
                         'annex+http://x/p')
        self.assertEqual(provider.configuration({})['bot_dir'], '/var/run/secrets/loris-forgejo-export/bot')

    def test_git_environment_carries_credentials_helper_not_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            config = provider.configuration({'LFX_FORGEJO_URL': 'http://127.0.0.1:3000'})
            config['clones'] = directory
            names = provider.registry.parse(data(site())).sites[0]
            env = provider.Dataset(names, 'Study_A', config).environment()
        pairs = {env[f'GIT_CONFIG_KEY_{i}']: env[f'GIT_CONFIG_VALUE_{i}']
                 for i in range(int(env['GIT_CONFIG_COUNT']))}
        self.assertIn('$LFX_BOT_DIR/token', pairs['credential.helper'])
        self.assertEqual(pairs['remote.origin.annexUrl'], 'annex+http://127.0.0.1:3000/git-annex-p2phttp')
        self.assertEqual(pairs['annex.security.allowed-ip-addresses'], '127.0.0.1')
        self.assertEqual(env['LFX_BOT_DIR'], '/var/run/secrets/loris-forgejo-export/bot')


class RequestValidationTest(ProviderCase):
    def test_bad_requests(self):
        self.make_provider()
        cases = [
            ('/content', b'x' * 5000),
            ('/content', b'not json'),
            ('/content', b'[]'),
            ('/content', {'site': 's', 'dataset': 'ds', 'key': KEY}),
            ('/content', {'path': 's/ds'}),
            ('/content', {'path': 's/ds/../secret'}),
            ('/content', {'path': 's/ds/.git/config'}),
            ('/content', {'path': '/etc/passwd'}),
            ('/content', {'path': 'x/ds/sub-01/a.nii.gz'}),
            ('/content', {'path': 's/unknown/sub-01/a.nii.gz'}),
            ('/chunks', {'path': f's/ds/{EEG}.vhdr'}),
            ('/chunks', {'path': f's/ds/{EEG}.vhdr', 'chunkPath': f'content-provider-chunks/x/ds/{DIGEST}.chunks'}),
            ('/chunks', {'path': f's/ds/{EEG}.vhdr', 'chunkPath': 'content-provider-chunks/s/ds/' + 'a' * 64
                         + '.chunks'}),
            ('/chunks', {'path': f's/ds/{EEG}.vhdr', 'chunkPath': f's/ds/{DIGEST}.chunks'}),
            ('/chunks', {'path': f's/ds/{EEG}.eeg', 'chunkPath': 'content-provider-chunks/s/ds/'
                         + hashlib.sha256(f'{EEG}.eeg'.encode()).hexdigest() + '.chunks'}),
        ]
        for endpoint, payload in cases:
            with self.subTest(endpoint=endpoint, payload=payload):
                self.assertEqual(post(self.url + endpoint, payload)[0], 400)
        self.assertEqual(post(self.url + '/other', {'path': 's/ds/x'})[0], 404)

    def test_unsynchronized_dataset_is_unavailable(self):
        self.make_provider()
        self.assertEqual(post(self.url + '/content', {'path': f's/ds/{IMAGE}'}),
                         (404, {'state': 'unavailable', 'message': 'the dataset has not been synchronized yet'}))
        self.assertEqual(post(self.url + '/chunks', {'path': f's/ds/{EEG}.vhdr', 'chunkPath': CHUNK_PATH})[0], 404)
        with urllib.request.urlopen(self.url + '/healthz', timeout=5) as response:
            self.assertEqual(response.status, 200)


@needs_annex
class ContentTest(ProviderCase):
    def test_fetch_on_demand_then_sweep(self):
        clone = self.build_world()
        key = self.key(IMAGE)
        store = self.make_provider(retry_after=600)
        self.assertNotIn(IMAGE, present(clone))
        self.assertEqual(post(self.url + '/content', {'path': f's/ds/sub-01/anat/sub-01_T1w.json'}),
                         (200, {'state': 'ready'}))

        code, reply = post(self.url + '/content', {'path': f's/ds/{IMAGE}'})
        self.assertEqual((code, reply), (202, {'state': 'pending', 'retryAfter': 60,
                                               'message': 'Fetching the file from site S; this can take a minute.'}))
        self.assertNotIn(key, json.dumps(reply))
        self.assertEqual(post(self.url + '/content', {'path': f's/ds/{IMAGE}'})[0], 202)    # deduplicated
        wait_for(lambda: post(self.url + '/content', {'path': f's/ds/{IMAGE}'})[0] == 200
                 and not store.inflight and not store.pending)
        self.assertEqual(self.world['staged'], [[key]])
        self.assertIn(IMAGE, present(clone))
        self.assertEqual(forge_objects(self.world['forge']), [])                          # transit copy dropped
        self.assertEqual([row[2] for row in store.ledger.rows()], [key])
        config = (clone / '.git' / 'config').read_text()
        self.assertNotIn('credential', config)
        self.assertNotIn('bot-token', config)

        store.sweep()                                                                       # fresh: kept
        self.assertIn(IMAGE, present(clone))
        store.config['idle_ttl'] = 0
        with open(clone.parent / '.ds.lock', 'w') as lock:                                 # a sync is running
            fcntl.flock(lock, fcntl.LOCK_EX)
            store.sweep()
        self.assertIn(IMAGE, present(clone))
        self.assertIn({'event': 'sweep-deferred', 'org': 'lfx-s', 'dataset': 'ds', 'reason': 'dataset lock busy'},
                      self.logs)
        store.sweep()
        self.assertNotIn(IMAGE, present(clone))
        self.assertEqual(store.ledger.rows(), [])

    def test_fetch_waits_for_the_dataset_lock_and_records_before_get(self):
        clone = self.build_world()
        key = self.key(IMAGE)
        store = self.make_provider()
        with open(clone.parent / '.ds.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(post(self.url + '/content', {'path': f's/ds/{IMAGE}'})[0], 202)
            wait_for(lambda: self.world['done'])
            time.sleep(0.5)
            self.assertNotIn(IMAGE, present(clone))
            self.assertEqual(store.ledger.rows(), [])
        wait_for(lambda: post(self.url + '/content', {'path': f's/ds/{IMAGE}'})[0] == 200)
        self.assertEqual([row[2] for row in store.ledger.rows()], [key])

    def test_offline_site_is_reported_then_recovers(self):
        self.build_world()
        store = self.make_provider(offline_after=0)
        self.world['hold'].set()
        self.assertEqual(post(self.url + '/content', {'path': f's/ds/{IMAGE}'})[0], 202)
        wait_for(lambda: post(self.url + '/content', {'path': f's/ds/{IMAGE}'})[0] == 404)
        code, reply = post(self.url + '/content', {'path': f's/ds/{IMAGE}'})
        self.assertEqual(reply['state'], 'unavailable')
        self.assertIn('site S is offline', reply['message'])
        self.world['hold'].clear()
        wait_for(lambda: post(self.url + '/content', {'path': f's/ds/{IMAGE}'})[0] == 200 and not store.inflight)
        self.assertEqual(len(self.world['staged']), 1)

    def test_key_without_site_copy_is_unavailable(self):
        self.build_world()
        git(self.world['site'], 'annex', 'drop', '-q', '--force', IMAGE)
        share(self.world['site'])
        git(self.world['clone'], 'fetch', '-q', 'origin')
        git(self.world['clone'], 'annex', 'merge', '-q')
        self.make_provider()
        wait_for(lambda: post(self.url + '/content', {'path': f's/ds/{IMAGE}'}) ==
                 (404, {'state': 'unavailable', 'message': 'site S has no copy of this file'}))
        self.assertEqual(self.world['staged'], [])

    def test_missing_credentials(self):
        self.build_world()
        (self.bot / 'token').write_text('')
        self.make_provider()
        self.assertEqual(post(self.url + '/content', {'path': f's/ds/{IMAGE}'}),
                         (404, {'state': 'unavailable', 'message': provider.CREDENTIALS_MESSAGE}))
        self.assertEqual(post(self.url + '/chunks', {'path': f's/ds/{EEG}.vhdr', 'chunkPath': CHUNK_PATH}),
                         (404, {'state': 'unavailable', 'message': provider.CREDENTIALS_MESSAGE}))
        self.assertEqual(post(self.url + '/content', {'path': 's/ds/sub-01/anat/sub-01_T1w.json'})[0], 200)
        with urllib.request.urlopen(self.url + '/healthz', timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.world['staged'], [])

    def test_unknown_file_is_unavailable(self):
        self.build_world()
        self.make_provider()
        self.assertEqual(post(self.url + '/content', {'path': 's/ds/sub-02/anat/x.nii.gz'})[0], 404)


@needs_annex
class ChunksTest(ProviderCase):
    def chunks(self):
        return post(self.url + '/chunks', {'path': f's/ds/{EEG}.vhdr', 'chunkPath': CHUNK_PATH})

    def test_chunks_on_demand_then_sweep(self):
        clone = self.build_world()
        eeg_key = self.key(f'{EEG}.eeg')
        store = self.make_provider()
        self.assertEqual(self.chunks(), (202, {
            'state': 'pending', 'retryAfter': 5,
            'message': 'Preparing the EEG signal from site S; this can take a few minutes.'}))
        wait_for(lambda: self.chunks()[0] == 200 and not store.pinned)
        output = self.tmp / 'provider-chunks' / 's' / 'ds' / f'{DIGEST}.chunks'
        self.assertEqual(json.loads((output / 'index.json').read_text()), {'read': ['signal', 'markers']})
        self.assertEqual(sorted(p.name for p in output.parent.iterdir()), [f'{DIGEST}.chunks'])
        self.assertEqual([(row[2], row[3]) for row in store.ledger.rows('provider_chunks')],
                         [(DIGEST, provider.tree_size(output))])
        self.assertEqual(self.world['staged'], [[eeg_key]])
        rows = store.ledger.rows()
        self.assertEqual([row[2] for row in rows], [eeg_key])
        self.assertGreater(rows[0][5], rows[0][4])                       # accessed refreshed by the conversion

        store.config['idle_ttl'] = 0
        store.sweep()
        self.assertFalse(output.exists())
        self.assertEqual(store.ledger.rows('provider_chunks'), [])
        self.assertNotIn(f'{EEG}.eeg', present(clone))
        self.assertTrue((clone / f'{EEG}.vhdr').is_file())                # metadata untouched

    def test_existing_chunks_are_recorded(self):
        self.build_world()
        store = self.make_provider()
        output = self.tmp / 'provider-chunks' / 's' / 'ds' / f'{DIGEST}.chunks'
        output.mkdir(parents=True)
        (output / 'index.json').write_text('{}')
        self.assertEqual(self.chunks(), (200, {'state': 'ready'}))
        self.assertEqual([(row[2], row[3]) for row in store.ledger.rows('provider_chunks')], [(DIGEST, 2)])

    def test_sweep_respects_the_conversion_pin(self):
        clone = self.build_world()
        eeg_key = self.key(f'{EEG}.eeg')
        store = self.make_provider(idle_ttl=0, chunk_timeout=3600)
        site_entry = store.datasets()[('s', 'ds')]
        self.assertEqual(store.ensure(site_entry, 'ds', [eeg_key]), [eeg_key])
        store.pin('s', 'ds', [eeg_key], 1)
        store.sweep()
        self.assertIn(f'{EEG}.eeg', present(clone))                       # active conversion, fresh row
        store.config['chunk_timeout'] = 0
        time.sleep(0.01)
        store.sweep()
        self.assertNotIn(f'{EEG}.eeg', present(clone))                    # chunkTimeout expired
        store.pin('s', 'ds', [eeg_key], -1)

    def test_chunker_failure_is_reported(self):
        clone = self.build_world()
        store = self.make_provider()
        (clone / 'sub-01' / 'eeg' / 'fail').write_text('')
        self.chunks()
        wait_for(lambda: self.chunks()[0] == 404)
        self.assertEqual(self.chunks()[1], {
            'state': 'unavailable',
            'message': 'could not prepare the EEG signal from site S: chunking failed: ValueError: broken recording'})
        self.assertEqual(list((self.tmp / 'provider-chunks' / 's' / 'ds').iterdir()), [])
        self.assertFalse(store.pinned)


if __name__ == '__main__':
    unittest.main()
