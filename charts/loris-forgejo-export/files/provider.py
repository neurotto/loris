#!/usr/bin/env python3
"""loris-forgejo-export content provider: on-demand site content for LORIS's provider-neutral contract.

Sync (``sync.py``) drops imaging content from the dataset copies once LORIS has imported it. When a LORIS user opens
a file whose content is absent, LORIS asks this service (``POST /content {"path": "<site>/<dataset>/<file>"}``). The
provider resolves the git-annex key from the dataset copy itself, never from the request, and answers:

- 200 ``ready``: the content is in the copy; the access is recorded in the ledger.
- 202 ``pending``: the key was queued. Requests that arrive within ``LFX_CACHE_BATCH_WINDOW`` are grouped per
  dataset. The site worker uploads them to Forgejo (``stage-content.yml``), the provider gets them into the copy and
  drops the Forgejo transit copy.
- 404 ``unavailable``: the site runner did not pick the run up within ``LFX_CACHE_OFFLINE_AFTER`` seconds (site
  offline), the site has no copy, the fetch failed, or the export bot is not provisioned. The fetch keeps going in
  the background; an unavailable answer is kept for 60 s, then a new request starts a new fetch.

Every fetched key is recorded in the shared SQLite ledger. A sweeper drops keys idle longer than
``LFX_CACHE_IDLE_TTL``, older than ``LFX_CACHE_MAX_AGE``, then the least recently used ones above
``LFX_CACHE_MAX_BYTES``. It drops only keys whose site copy is recorded and skips datasets whose lock is held.

EEG signals (``POST /chunks {"path", "chunkPath"}``): the first request fetches the recording and its companion files
(BrainVision data and marker files, EEGLAB .fdt), runs the chunker selected by extension, and writes
``/provider-chunks/<site>/<dataset>/<sha256>.chunks``. Chunk directories share the ledger, byte cap and expiry.

See CONTRACT.md for paths, environment and ledger rules.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import registry  # noqa: E402

BATCH = 1000
"""Keys per stage-content run (the workflow's ``keys`` input limit)."""
MAX_BODY = 4096
CLONES = '/clones'
PROVIDER_CHUNKS = '/provider-chunks'
BOT_DIR = '/var/run/secrets/loris-forgejo-export/bot'
CREDENTIALS_MESSAGE = 'bot credentials not provisioned yet; run the reconciler'
LEDGER_DDL = '''
CREATE TABLE IF NOT EXISTS content (site TEXT, dataset TEXT, key TEXT, size INTEGER,
    fetched REAL, accessed REAL, PRIMARY KEY (site, dataset, key));
CREATE TABLE IF NOT EXISTS provider_chunks (site TEXT, dataset TEXT, key TEXT, size INTEGER,
    fetched REAL, accessed REAL, PRIMARY KEY (site, dataset, key));
'''
TABLES = ('content', 'provider_chunks')
SPECIAL_UUIDS = {'00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002'}
KEY_RE = re.compile(r'^[A-Z0-9]+(-[a-zA-Z][0-9]+)*--[A-Za-z0-9._,+=-]+$')
SIZE_RE = re.compile(r'-s(\d+)(?:-|--)')
NAME_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$')
PATH_RE = re.compile(r'^(?:[A-Za-z0-9_][A-Za-z0-9_.+-]{0,254}/){0,8}[A-Za-z0-9_][A-Za-z0-9_.+-]{0,254}$')
CHUNK_PATH_RE = re.compile(
    r'^content-provider-chunks/([A-Za-z0-9_][A-Za-z0-9_.-]{0,99})/'
    r'([A-Za-z0-9_][A-Za-z0-9_.-]{0,99})/([a-f0-9]{64})\.chunks$')
METADATA_SUFFIXES = ('.json', '.jsonld', '.tsv', '.tsv.gz', '.csv', '.xlsx', '.bval', '.bvec', '.vhdr', '.vmrk',
                     '.txt', '.md', '.rst', '.cff', '.bidsignore')
METADATA_NAMES = {'README', 'CHANGES', 'LICENSE'}
PRESENT, FETCHING, UNAVAILABLE = 'present', 'fetching', 'unavailable'
CHUNKERS = {
    '.edf': ['edf-to-chunks'],
    '.set': ['eeglab-to-chunks'],
    '.vhdr': [sys.executable, os.path.join(_HERE, 'ephys_chunks.py')],
    '.bdf': [sys.executable, os.path.join(_HERE, 'ephys_chunks.py')],
}
"""Chunker command per recording extension; each takes ``FILE --destination DIR`` and writes ``DIR/<stem>.chunks``."""


class ProviderError(RuntimeError):
    """Content could not be provided; the message says why."""


class NotProvisioned(ProviderError):
    def __init__(self):
        super().__init__(CREDENTIALS_MESSAGE)


class BadRequest(ValueError):
    """The request is malformed or names a path outside a managed dataset (HTTP 400)."""


def log(**fields):
    """Print one JSON log line."""
    print(json.dumps(fields, sort_keys=True), flush=True)


def is_metadata(path: str) -> bool:
    name = path.rsplit('/', 1)[-1]
    return name in METADATA_NAMES or name.endswith(METADATA_SUFFIXES)


def key_size(key: str) -> int:
    match = SIZE_RE.search(key)
    return int(match.group(1)) if match else 0


def tree_size(path: Path) -> int:
    return sum(entry.stat().st_size for entry in path.rglob('*') if entry.is_file())


def read_credentials(bot_dir: str) -> tuple[str, str]:
    """Return the export bot's (username, token), read from the mounted Secret; raise NotProvisioned if absent."""
    values = []
    for name in ('username', 'token'):
        try:
            values.append(Path(bot_dir, name).read_text(encoding='utf-8').strip())
        except OSError:
            values.append('')
    if not all(values):
        raise NotProvisioned()
    return values[0], values[1]


def companions(clone: Path, path: str) -> list[str]:
    """Dataset paths a recording needs next to it: BrainVision data and marker files, the EEGLAB .fdt.

    The BrainVision header must be present; the files it names must sit in the recording's directory.
    """
    if path.endswith('.vhdr'):
        names, section = [], None
        for line in (clone / path).read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if line.startswith('['):
                section = line
            elif section == '[Common Infos]' and '=' in line and \
                    line.split('=', 1)[0].strip() in ('DataFile', 'MarkerFile'):
                names.append(line.split('=', 1)[1].strip())
        if not names or not all(NAME_RE.match(name) for name in names):
            raise ProviderError(f'{path}: BrainVision header without valid DataFile/MarkerFile')
        found = [str(Path(path).parent / name) for name in names]
    elif path.endswith('.set'):
        found = [name for name in [str(Path(path).with_suffix('.fdt'))] if os.path.lexists(clone / name)]
    else:
        found = []
    missing = [name for name in found if not os.path.lexists(clone / name)]
    if missing:
        raise ProviderError(f'{path}: companion file {missing[0]} is missing from the dataset')
    return found


class Forgejo:
    """Forgejo API client authenticated as the export bot; credentials are read from files on every call."""

    def __init__(self, url: str, bot_dir: str):
        self.url = url.rstrip('/')
        self.bot_dir = bot_dir

    def provisioned(self) -> bool:
        try:
            read_credentials(self.bot_dir)
        except NotProvisioned:
            return False
        return True

    def request(self, method: str, path: str, body=None):
        user, token = read_credentials(self.bot_dir)
        request = urllib.request.Request(
            self.url + '/api/v1' + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Basic ' + b64encode(f'{user}:{token}'.encode()).decode(),
                     'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise ProviderError(f'Forgejo {method} {path}: HTTP {error.code}') from error
        except (urllib.error.URLError, OSError) as error:
            raise ProviderError(f'Forgejo {method} {path}: {error}') from error
        return json.loads(payload) if payload else None

    def stage(self, org: str, worker_repository: str, dataset: str, commit: str, keys: list[str],
              timeout: float, poll: float, on_wait=None):
        """Dispatch stage-content on the site runner for keys and wait until it succeeds.

        on_wait(status, seconds) is called after each poll of an unfinished run.
        """
        actions = f'/repos/{urllib.parse.quote(org)}/{urllib.parse.quote(worker_repository)}/actions'
        run = self.request('POST', actions + '/workflows/stage-content.yml/dispatches', {
            'ref': 'main', 'return_run_info': True,
            'inputs': {'dataset': dataset, 'commit': commit, 'keys': ' '.join(keys)}})
        run_id = (run or {}).get('id')
        if not isinstance(run_id, int):
            raise ProviderError(f'stage-content dispatch on {org} returned no run id')
        log(event='stage-dispatched', org=org, dataset=dataset, run=run_id, keys=len(keys))
        started = time.monotonic()
        while True:
            status = (self.request('GET', f'{actions}/runs/{run_id}') or {}).get('status')
            if status == 'success':
                return
            if status in ('failure', 'cancelled', 'skipped'):
                raise ProviderError(f'stage-content run {run_id} of {org} ended with {status}')
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                try:
                    self.request('POST', f'{actions}/runs/{run_id}/cancel')
                except ProviderError as error:
                    log(event='stage-cancel-failed', org=org, run=run_id, error=str(error))
                raise ProviderError(f'stage-content run {run_id} of {org} still {status} after {timeout:.0f}s')
            if on_wait:
                on_wait(status, elapsed)
            time.sleep(poll)


class Ledger:
    """Content keys and chunk directories the provider holds, shared read-only with sync.py."""

    def __init__(self, path: str):
        self.lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.connection.execute('PRAGMA busy_timeout = 30000')
        self.connection.execute('PRAGMA journal_mode = WAL')
        self.connection.executescript(LEDGER_DDL)

    def record(self, table: str, site: str, dataset: str, rows: list[tuple[str, int]], now: float):
        assert table in TABLES
        with self.lock:
            self.connection.executemany(
                f'INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET size = excluded.size, '
                'fetched = excluded.fetched, accessed = excluded.accessed',
                [(site, dataset, key, size, now, now) for key, size in rows])

    def touch(self, table: str, site: str, dataset: str, key: str, now: float) -> bool:
        assert table in TABLES
        with self.lock:
            return self.connection.execute(
                f'UPDATE {table} SET accessed = ? WHERE site = ? AND dataset = ? AND key = ?',
                (now, site, dataset, key)).rowcount > 0

    def remove(self, table: str, site: str, dataset: str, keys: list[str]):
        assert table in TABLES
        with self.lock:
            self.connection.executemany(f'DELETE FROM {table} WHERE site = ? AND dataset = ? AND key = ?',
                                        [(site, dataset, key) for key in keys])

    def rows(self, table: str = 'content') -> list[tuple]:
        assert table in TABLES
        with self.lock:
            return self.connection.execute(
                f'SELECT site, dataset, key, size, fetched, accessed FROM {table} ORDER BY accessed').fetchall()

    def expired(self, now: float, idle: float, max_age: float, max_bytes: int) -> list[tuple]:
        """Rows to drop: idle or too old, then the least recently accessed until the rest fits in max_bytes.

        Content and chunk directories share the limits; each row ends with its table name.
        """
        rows = sorted([row + (table,) for table in TABLES for row in self.rows(table)], key=lambda row: row[5])
        expired = [row for row in rows if now - row[5] > idle or now - row[4] > max_age]
        kept = [row for row in rows if row not in expired]
        total = sum(row[3] for row in kept)
        for row in kept:
            if total <= max_bytes:
                break
            expired.append(row)
            total -= row[3]
        return expired


class Dataset:
    """git and git-annex operations on one dataset copy, authenticated as the export bot through the environment."""

    def __init__(self, site, dataset: str, config: dict):
        self.site = site
        self.dataset = dataset
        self.config = config
        self.clone = Path(config['clones']) / site.name / dataset

    def synchronized(self) -> bool:
        return (self.clone / '.git').is_dir()

    def environment(self) -> dict:
        """Per-command git configuration (GIT_CONFIG_COUNT); nothing is written to .git/config."""
        settings = [
            ('credential.helper', ''),
            ('credential.helper', '!f() { test "$1" = get || exit 0; '
                                  'echo "username=$(cat "$LFX_BOT_DIR/username")"; '
                                  'echo "password=$(cat "$LFX_BOT_DIR/token")"; }; f'),
            ('user.name', 'LORIS Forgejo export provider'),
            ('user.email', 'provider@loris-forgejo-export.invalid'),
        ]
        if self.config.get('annex_url'):
            settings.append(('remote.origin.annexUrl', self.config['annex_url']))
        ips = annex_ips(self.config)
        if ips:
            settings.append(('annex.security.allowed-ip-addresses', ips))
        env = dict(os.environ, LFX_BOT_DIR=self.config['bot_dir'], GIT_TERMINAL_PROMPT='0')
        count = int(env.get('GIT_CONFIG_COUNT') or 0)
        for index, (key, value) in enumerate(settings, start=count):
            env[f'GIT_CONFIG_KEY_{index}'] = key
            env[f'GIT_CONFIG_VALUE_{index}'] = value
        env['GIT_CONFIG_COUNT'] = str(count + len(settings))
        return env

    def git(self, *args, stdin: str | None = None, check: bool = True) -> str:
        result = subprocess.run(['git', *args], cwd=self.clone, env=self.environment(), capture_output=True,
                                input=stdin.encode() if stdin is not None else None)
        if check and result.returncode != 0:
            raise ProviderError(f'git {" ".join(args[:2])} failed: '
                                f'{result.stderr.decode(errors="replace").strip()[-300:]}')
        return result.stdout.decode(errors='replace')

    @contextmanager
    def lock(self, blocking: bool = True):
        """Hold the per-dataset lock (/clones/<site>/.<dataset>.lock); yield False if busy and not blocking."""
        with open(self.clone.parent / f'.{self.dataset}.lock', 'a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def lookupkey(self, path: str) -> str | None:
        key = self.git('annex', 'lookupkey', '--', path, check=False).strip()
        return key if KEY_RE.fullmatch(key) else None

    def present(self, key: str) -> bool:
        location = self.git('annex', 'contentlocation', key, check=False).strip()
        return bool(location) and (self.clone / location).is_file()

    def batch(self, args: list[str], keys: list[str]) -> list[dict]:
        """Run a git-annex ``--batch-keys --json`` command over keys and return one record per key."""
        if not keys:
            return []
        output = self.git('annex', *args, '--batch-keys', '--json', '--json-error-messages',
                          stdin=''.join(key + '\n' for key in keys), check=False)
        return [json.loads(line) for line in output.splitlines() if line.startswith('{')]

    def locations(self, keys: list[str]) -> dict[str, dict]:
        """Return {key: {'here', 'origin', 'site'}} presence flags from the location log."""
        origin = self.git('config', '--get', 'remote.origin.annex-uuid', check=False).strip()
        here = self.git('config', '--get', 'annex.uuid').strip()
        result = {key: {'here': False, 'origin': False, 'site': False} for key in keys}
        for record in self.batch(['whereis'], keys):
            uuids = {entry['uuid'] for entry in record.get('whereis', []) + record.get('untrusted', [])}
            if record.get('key') in result:
                result[record['key']] = {'here': here in uuids, 'origin': bool(origin) and origin in uuids,
                                         'site': bool(uuids - {here, origin} - SPECIAL_UUIDS)}
        return result

    def get(self, keys: list[str]) -> list[str]:
        """Get keys from origin; return the ones that failed."""
        got = {record['key'] for record in self.batch(['get', '--from', 'origin'], keys) if record.get('success')}
        return [key for key in keys if key not in got]

    def drop_transit(self, keys: list[str]):
        """Drop Forgejo transit copies of keys whose site copy is recorded, then push the location log."""
        locations = self.locations(keys)
        transit = [key for key in keys if locations[key]['origin'] and locations[key]['site']]
        dropped = [r['key'] for r in self.batch(['drop', '--from', 'origin', '--force'], transit) if r.get('success')]
        if dropped:
            self.git('annex', 'sync', '-q', '--no-content', '--only-annex', '--no-pull', 'origin', check=False)
        if len(dropped) != len(transit):
            log(event='transit-drop-incomplete', org=self.site.org, dataset=self.dataset,
                dropped=len(dropped), wanted=len(transit))


_ANNEX_IPS: dict[str, str] = {}


def annex_ips(config: dict) -> str:
    """Addresses of the in-cluster Forgejo host, allowed for git-annex HTTP (private service IPs)."""
    host = urllib.parse.urlsplit(config.get('forgejo_url') or '').hostname
    if not host or not urllib.parse.urlsplit(config['forgejo_url']).scheme.startswith('http'):
        return ''
    if host not in _ANNEX_IPS:
        try:
            _ANNEX_IPS[host] = ' '.join(sorted({info[4][0] for info in
                                                socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}))
        except OSError:
            return ''
    return _ANNEX_IPS[host]


class Provider:
    def __init__(self, config: dict, forgejo: Forgejo, registry_path: str):
        self.config = config
        self.forgejo = forgejo
        self.registry_path = registry_path
        self.ledger = Ledger(config['ledger'])
        self.lock = threading.Lock()
        self.status: dict[tuple, dict] = {}
        self.pending: dict[tuple, set] = {}
        self.inflight: set[tuple] = set()
        self.chunker = threading.Semaphore(1)
        self.pinned: dict[tuple, int] = {}
        """(site, dataset, key) of content a chunk conversion is reading; see sweep()."""
        self._sites = (None, {})

    # Registry --------------------------------------------------------------------------------------------------

    def datasets(self) -> dict:
        """{(site, dataset): Site} of enabled managed sites, reloaded when the registry file changes."""
        mtime = os.stat(self.registry_path).st_mtime
        if self._sites[0] != mtime:
            loaded = registry.load(self.registry_path)
            self._sites = (mtime, {(site.name, dataset): site for site in loaded.sites if site.enabled
                                   for dataset in site.datasets})
        return self._sites[1]

    def resolve(self, path) -> tuple:
        """Split ``<site>/<dataset>/<relative>`` and return (Site, dataset, relative); raise BadRequest."""
        parts = path.split('/', 2) if isinstance(path, str) else []
        if (len(parts) != 3 or not NAME_RE.fullmatch(parts[0]) or not NAME_RE.fullmatch(parts[1])
                or not PATH_RE.fullmatch(parts[2]) or '.git' in parts[2].split('/')):
            raise BadRequest('invalid path')
        site = self.datasets().get((parts[0], parts[1]))
        if site is None:
            raise BadRequest(f'{parts[0]}/{parts[1]} is not a managed dataset')
        return site, parts[1], parts[2]

    @staticmethod
    def alias(site) -> str:
        return site.alias or site.name

    # Content ---------------------------------------------------------------------------------------------------

    def content(self, path) -> dict:
        site, dataset, relative = self.resolve(path)
        copy = Dataset(site, dataset, self.config)
        if not copy.synchronized():
            return {'state': UNAVAILABLE, 'message': 'the dataset has not been synchronized yet'}
        key = copy.lookupkey(relative)
        if key is None:
            if (copy.clone / relative).is_file():
                return {'state': PRESENT}
            return {'state': UNAVAILABLE, 'message': 'this file is not available from this provider'}
        return self.request(site, dataset, key)

    def request(self, site, dataset: str, key: str) -> dict:
        if Dataset(site, dataset, self.config).present(key):
            self.ledger.touch('content', site.name, dataset, key, time.time())
            return {'state': PRESENT}
        ident = (site.name, dataset, key)
        with self.lock:
            status = self.status.get(ident)
            if status and (status['state'] == FETCHING or ident in self.inflight
                           or time.monotonic() - status['since'] < 60):
                return self.reply(status)
        if not self.forgejo.provisioned():
            return {'state': UNAVAILABLE, 'message': CREDENTIALS_MESSAGE}
        with self.lock:
            status = self.status.get(ident)
            if status and (status['state'] == FETCHING or ident in self.inflight):
                return self.reply(status)
            self.status[ident] = status = {
                'state': FETCHING, 'since': time.monotonic(),
                'message': f'Fetching the file from site {self.alias(site)}; this can take a minute.'}
            queue = self.pending.setdefault((site.name, dataset), set())
            start = not queue
            queue.add(key)
        if start:
            threading.Thread(target=self.worker, args=(site, dataset), daemon=True).start()
        return self.reply(status)

    def reply(self, status: dict) -> dict:
        retry = self.config['retry_after'] if status['state'] == FETCHING else 60
        return {'state': status['state'], 'message': status['message'], 'retryAfter': retry}

    def mark(self, site: str, dataset: str, keys, state: str, message: str = ''):
        with self.lock:
            for key in keys:
                if state == PRESENT:
                    self.status.pop((site, dataset, key), None)
                else:
                    self.status[(site, dataset, key)] = {'state': state, 'message': message,
                                                         'since': time.monotonic()}

    def worker(self, site, dataset: str):
        """Fetch the dataset's queued keys in batches until the queue is empty."""
        while True:
            time.sleep(self.config['batch_window'])
            with self.lock:
                keys = sorted(self.pending.get((site.name, dataset), ()))[:BATCH]
                if not keys:
                    self.pending.pop((site.name, dataset), None)
                    return
                self.pending[(site.name, dataset)].difference_update(keys)
                self.inflight.update((site.name, dataset, key) for key in keys)
            try:
                self.fetch(site, dataset, keys)
            except NotProvisioned:
                log(event='fetch-failed', org=site.org, dataset=dataset, keys=len(keys), error=CREDENTIALS_MESSAGE)
                self.mark(site.name, dataset, keys, UNAVAILABLE, CREDENTIALS_MESSAGE)
            except (ProviderError, OSError, subprocess.SubprocessError) as error:
                log(event='fetch-failed', org=site.org, dataset=dataset, keys=len(keys), error=str(error))
                self.mark(site.name, dataset, keys, UNAVAILABLE,
                          f'could not fetch the file from site {self.alias(site)}; see the provider logs')
            finally:
                with self.lock:
                    self.inflight.difference_update((site.name, dataset, key) for key in keys)

    def fetch(self, site, dataset: str, keys: list[str]):
        """Stage keys the site must upload, then get them into the copy and drop the Forgejo transit copies."""
        copy = Dataset(site, dataset, self.config)
        locations = copy.locations(keys)
        missing = [key for key in keys if not locations[key]['site'] and not locations[key]['here']]
        self.mark(site.name, dataset, missing, UNAVAILABLE, f'site {self.alias(site)} has no copy of this file')
        wanted = [key for key in keys if key not in missing and not locations[key]['here']]
        to_stage = [key for key in wanted if not locations[key]['origin']]
        log(event='fetch', org=site.org, dataset=dataset, keys=len(keys), stage=len(to_stage), get=len(wanted))
        if to_stage:
            def waiting(status, seconds):
                if status in ('waiting', 'blocked') and seconds > self.config['offline_after']:
                    self.mark(site.name, dataset, to_stage, UNAVAILABLE,
                              f'site {self.alias(site)} is offline (its worker did not start the transfer); '
                              'retry later')
            commit = copy.git('rev-parse', 'HEAD').strip()
            self.forgejo.stage(site.org, site.worker_repository, dataset, commit, to_stage,
                               self.config['stage_timeout'], self.config['poll'], on_wait=waiting)
        failed: list[str] = []
        with copy.lock():
            if to_stage:
                copy.git('fetch', '-q', 'origin', 'git-annex')
                copy.git('annex', 'merge', '-q')
            if wanted:
                self.ledger.record('content', site.name, dataset, [(key, key_size(key)) for key in wanted],
                                   time.time())
                try:
                    failed = copy.get(wanted)
                finally:
                    try:
                        copy.drop_transit(wanted)
                    finally:
                        gone = [key for key in failed if not copy.present(key)]
                        self.ledger.remove('content', site.name, dataset, gone)
        if failed:
            log(event='get-failed', org=site.org, dataset=dataset, failed=len(failed), keys=len(wanted))
            self.mark(site.name, dataset, failed, UNAVAILABLE,
                      f'could not fetch the file from site {self.alias(site)}; see the provider logs')
        if wanted:
            log(event='fetched', org=site.org, dataset=dataset, keys=len(wanted) - len(failed))
        self.mark(site.name, dataset, [key for key in keys if key not in missing and key not in failed], PRESENT)

    def ensure(self, site, dataset: str, keys: list[str]) -> list[str]:
        """Fetch the keys that are not here; return the ones this call fetched."""
        copy = Dataset(site, dataset, self.config)
        missing = [key for key in keys if not copy.present(key)]
        if missing:
            if not self.forgejo.provisioned():
                raise NotProvisioned()
            self.fetch(site, dataset, missing)
        absent = [key for key in missing if not copy.present(key)]
        if absent:
            with self.lock:
                reason = self.status.get((site.name, dataset, absent[0]), {}).get('message')
            raise ProviderError(reason or f'site {self.alias(site)} has no copy of {len(absent)} file(s) '
                                          'of this recording')
        return missing

    # EEG chunks ------------------------------------------------------------------------------------------------

    def chunks(self, path, chunk_path) -> dict:
        """Prepare the EEG chunks of the recording at path in the requested chunkPath."""
        site, dataset, relative = self.resolve(path)
        match = CHUNK_PATH_RE.fullmatch(chunk_path) if isinstance(chunk_path, str) else None
        digest = hashlib.sha256(relative.encode()).hexdigest()
        if not match or match.groups() != (site.name, dataset, digest):
            raise BadRequest('chunkPath does not match path')
        if Path(relative).suffix not in self.config['chunkers']:
            raise BadRequest('unsupported recording format')
        copy = Dataset(site, dataset, self.config)
        if not copy.synchronized():
            return {'state': UNAVAILABLE, 'message': 'the dataset has not been synchronized yet'}
        root = Path(self.config['provider_chunks'])
        if not root.is_dir():
            return {'state': UNAVAILABLE, 'message': 'EEG chunk storage is not configured'}
        if not os.path.lexists(copy.clone / relative):
            return {'state': UNAVAILABLE, 'message': 'this recording is not available from this provider'}
        final = root / site.name / dataset / f'{digest}.chunks'
        ident = ('chunks', site.name, dataset, digest)
        with self.lock:
            status = self.status.get(ident)
            if status and status['state'] == FETCHING:
                return self.reply(status)
        if (final / 'index.json').is_file():
            if not self.ledger.touch('provider_chunks', site.name, dataset, digest, time.time()):
                self.ledger.record('provider_chunks', site.name, dataset, [(digest, tree_size(final))], time.time())
            return {'state': PRESENT}
        if status and time.monotonic() - status['since'] < 60:
            return self.reply(status)
        if not self.forgejo.provisioned():
            return {'state': UNAVAILABLE, 'message': CREDENTIALS_MESSAGE}
        with self.lock:
            status = self.status.get(ident)
            if status and status['state'] == FETCHING:
                return self.reply(status)
            self.status[ident] = status = {
                'state': FETCHING, 'since': time.monotonic(),
                'message': f'Preparing the EEG signal from site {self.alias(site)}; this can take a few minutes.'}
        threading.Thread(target=self.chunk_worker, args=(site, dataset, relative, digest), daemon=True).start()
        return self.reply(status)

    def pin(self, site: str, dataset: str, keys, step: int):
        with self.lock:
            for key in keys:
                count = self.pinned.get((site, dataset, key), 0) + step
                if count > 0:
                    self.pinned[(site, dataset, key)] = count
                else:
                    self.pinned.pop((site, dataset, key), None)

    def chunk_worker(self, site, dataset: str, path: str, digest: str):
        ident = ('chunks', site.name, dataset, digest)
        copy = Dataset(site, dataset, self.config)
        target = Path(self.config['provider_chunks']) / site.name / dataset
        temporary = target / f'.{digest}.tmp'
        started = time.time()
        pinned: list[str] = []
        try:
            parts = {name: copy.lookupkey(name) for name in [path, *companions(copy.clone, path)]}
            keys = sorted({key for key in parts.values() if key})
            self.pin(site.name, dataset, keys, 1)
            pinned = keys
            fetched = self.ensure(site, dataset, keys)
            held = [key for name, key in parts.items() if key in fetched and not is_metadata(name)]
            with self.chunker:
                for key in held:
                    self.ledger.touch('content', site.name, dataset, key, time.time())
                shutil.rmtree(temporary, ignore_errors=True)
                temporary.mkdir(parents=True)
                command = [*self.config['chunkers'][Path(path).suffix], str(copy.clone / path),
                           '--destination', str(temporary)]
                log(event='chunking', org=site.org, dataset=dataset, path=path)
                result = subprocess.run(command, capture_output=True, timeout=self.config['chunk_timeout'])
                for key in held:
                    self.ledger.touch('content', site.name, dataset, key, time.time())
                output = temporary / f'{Path(path).stem}.chunks'
                if result.returncode != 0 or not (output / 'index.json').is_file():
                    detail = result.stderr.decode(errors='replace').strip().splitlines()[-1:] or ['no index.json']
                    raise ProviderError(f'chunking failed: {detail[0][:300]}')
                final = target / f'{digest}.chunks'
                shutil.rmtree(final, ignore_errors=True)
                output.rename(final)
                self.ledger.record('provider_chunks', site.name, dataset, [(digest, tree_size(final))], time.time())
            log(event='chunked', org=site.org, dataset=dataset, path=path, seconds=round(time.time() - started))
            with self.lock:
                self.status.pop(ident, None)
        except subprocess.TimeoutExpired:
            self._chunks_failed(site, dataset, path, ident, 'chunking timed out')
        except (ProviderError, OSError, subprocess.SubprocessError) as error:
            self._chunks_failed(site, dataset, path, ident, str(error))
        finally:
            self.pin(site.name, dataset, pinned, -1)
            shutil.rmtree(temporary, ignore_errors=True)

    def _chunks_failed(self, site, dataset: str, path: str, ident: tuple, error: str):
        log(event='chunks-failed', org=site.org, dataset=dataset, path=path, error=error)
        with self.lock:
            self.status[ident] = {'state': UNAVAILABLE, 'since': time.monotonic(),
                                  'message': f'could not prepare the EEG signal from site {self.alias(site)}: '
                                             f'{error}'}

    # Expiry ----------------------------------------------------------------------------------------------------

    def sweep(self):
        """Drop expired content that has a site copy and expired chunk directories; forget content no longer here."""
        now = time.time()
        rows = self.ledger.expired(now, self.config['idle_ttl'], self.config['max_age'], self.config['max_bytes'])
        by_dataset: dict[tuple, list[str]] = {}
        for site_name, dataset, key, _, _, accessed, table in rows:
            if table == 'provider_chunks':
                with self.lock:
                    if (self.status.get(('chunks', site_name, dataset, key)) or {}).get('state') == FETCHING:
                        continue
                shutil.rmtree(Path(self.config['provider_chunks']) / site_name / dataset / f'{key}.chunks',
                              ignore_errors=True)
                self.ledger.remove('provider_chunks', site_name, dataset, [key])
                log(event='swept-chunks', site=site_name, dataset=dataset, key=key)
                continue
            with self.lock:
                active = (site_name, dataset, key) in self.pinned
            if active and now - accessed <= self.config['chunk_timeout']:
                continue
            by_dataset.setdefault((site_name, dataset), []).append(key)
        datasets = self.datasets()
        for (site_name, dataset), keys in by_dataset.items():
            site = datasets.get((site_name, dataset))
            copy = Dataset(site, dataset, self.config) if site else None
            if copy is None or not copy.synchronized():
                log(event='sweep-forget', site=site_name, dataset=dataset, keys=len(keys))
                self.ledger.remove('content', site_name, dataset, keys)
                continue
            with copy.lock(blocking=False) as locked:
                if not locked:
                    log(event='sweep-deferred', org=site.org, dataset=dataset, reason='dataset lock busy')
                    continue
                locations = copy.locations(keys)
                gone = [key for key in keys if not locations[key]['here']]
                safe = [key for key in keys if locations[key]['here'] and locations[key]['site']]
                dropped = [r['key'] for r in copy.batch(['drop', '--force'], safe) if r.get('success')]
                self.ledger.remove('content', site_name, dataset, gone + dropped)
                log(event='swept', org=site.org, dataset=dataset, dropped=len(dropped), forgotten=len(gone),
                    kept_without_site_copy=len(keys) - len(safe) - len(gone), failed=len(safe) - len(dropped))

    def sweeper(self):
        while True:
            time.sleep(self.config['sweep_seconds'])
            try:
                self.sweep()
            except Exception as error:  # noqa: BLE001 - keep sweeping on the next round
                log(event='sweep-failed', error=str(error))


class Handler(BaseHTTPRequestHandler):
    provider: Provider = None
    protocol_version = 'HTTP/1.1'

    def send(self, code: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == '/healthz':
            return self.send(200, {'ok': True})
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        self.close_connection = True
        if self.path not in ('/content', '/chunks'):
            return self.send(404, {'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self.send(400, {'error': f'expected a JSON body of at most {MAX_BODY} bytes'})
        fields = ('path',) if self.path == '/content' else ('path', 'chunkPath')
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict) or any(not isinstance(body.get(name), str) for name in fields):
            return self.send(400, {'error': 'expected {' + ', '.join(f'"{name}"' for name in fields) + '}'})
        try:
            reply = (self.provider.content(body['path']) if self.path == '/content'
                     else self.provider.chunks(body['path'], body['chunkPath']))
        except BadRequest as error:
            return self.send(400, {'error': str(error)})
        except Exception as error:  # noqa: BLE001 - answer terminal unavailable, keep serving
            log(event='request-failed', endpoint=self.path, error=str(error))
            reply = {'state': UNAVAILABLE, 'message': 'content provider error; see its logs'}
        self.send_reply(reply)

    def send_reply(self, reply: dict):
        state = reply.get('state')
        if state == PRESENT:
            return self.send(200, {'state': 'ready'})
        if state == FETCHING:
            return self.send(202, {'state': 'pending', 'message': reply['message'],
                                   'retryAfter': min(max(int(reply.get('retryAfter', 5)), 1), 60)})
        return self.send(404, {'state': 'unavailable', 'message': reply.get('message') or 'Content is unavailable.'})

    def log_message(self, fmt, *args):
        pass


def configuration(environ=os.environ) -> dict:
    forgejo_url = environ.get('LFX_FORGEJO_URL', '').rstrip('/')
    return {
        'forgejo_url': forgejo_url,
        'annex_url': environ.get('LFX_ANNEX_URL') or (f'annex+{forgejo_url}/git-annex-p2phttp' if forgejo_url else ''),
        'bot_dir': environ.get('LFX_BOT_DIR') or BOT_DIR,
        'clones': CLONES,
        'provider_chunks': PROVIDER_CHUNKS,
        'ledger': os.path.join(CLONES, '.cache-ledger.sqlite'),
        'idle_ttl': float(environ.get('LFX_CACHE_IDLE_TTL', '3600')),
        'max_age': float(environ.get('LFX_CACHE_MAX_AGE', '86400')),
        'max_bytes': int(environ.get('LFX_CACHE_MAX_BYTES', str(2 << 30))),
        'sweep_seconds': float(environ.get('LFX_CACHE_SWEEP_SECONDS', '600')),
        'offline_after': float(environ.get('LFX_CACHE_OFFLINE_AFTER', '60')),
        'batch_window': float(environ.get('LFX_CACHE_BATCH_WINDOW', '1')),
        'retry_after': int(float(environ.get('LFX_CACHE_RETRY_AFTER', '5'))),
        'chunk_timeout': float(environ.get('LFX_CACHE_CHUNK_TIMEOUT', '3600')),
        'stage_timeout': float(environ.get('LFX_STAGE_TIMEOUT', '10800')),
        'poll': float(environ.get('LFX_POLL_SECONDS', '5')),
        'chunkers': CHUNKERS,
    }


def main() -> int:
    os.umask(0o022)
    config = configuration()
    provider = Provider(config, Forgejo(config['forgejo_url'], config['bot_dir']),
                        os.environ.get('LFX_REGISTRY', '/etc/loris-forgejo-export/registry.json'))
    provider.datasets()
    Handler.provider = provider
    threading.Thread(target=provider.sweeper, daemon=True).start()
    port = int(os.environ.get('LFX_PORT', '8080'))
    log(event='listening', port=port, idle_ttl=config['idle_ttl'], max_age=config['max_age'],
        max_bytes=config['max_bytes'], provisioned=provider.forgejo.provisioned())
    server = ThreadingHTTPServer(('', port), Handler)
    server.daemon_threads = True
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
