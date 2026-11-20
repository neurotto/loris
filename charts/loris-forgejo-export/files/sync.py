#!/usr/bin/env python3
"""Synchronize managed Forgejo datasets into the dataset copies LORIS reads.

For each target ``(site, dataset)`` (``LFX_SYNC_REPOSITORY`` or every dataset of every enabled managed site), under
the per-dataset lock ``/clones/<site>/.<dataset>.lock``:

1. **Fetch.** Clone or fetch ``/clones/<site>/<dataset>`` from ``LFX_FORGEJO_URL/<org>/<dataset>.git``, refresh the
   origin annex UUID and resolve the target commit without checking it out. A history that does not descend from
   ``refs/loris-forgejo-export/imported`` is refused, except for one approved transition
   (``LFX_HISTORY_REPOSITORY``, ``LFX_HISTORY_OLD``, ``LFX_HISTORY_NEW``).
2. **Hydrate.** List the annex keys of imaging files added or changed since the imported ref (every file before the
   first import), have the site upload the keys Forgejo lacks (``stage-content`` on ``<org>/<workerRepository>``),
   then ``git annex get`` them from origin. The work tree does not move.
3. **Publish.** Check out the target commit (a first clone is built in ``.<dataset>.cloning`` and renamed). Sites
   without import stop here.
4. **Import.** Run the site's LORIS discovery and read the LORIS discovery and import logs. The imported ref moves
   only after a verified acknowledgement; otherwise the import is pending and the next sync retries.
5. **Drop.** Always runs: Forgejo transit copies with a recorded site copy are dropped; local imaging content is
   dropped only after an acknowledgement, when a site copy is recorded and the provider ledger has no row for it.
   Metadata files are never dropped.

Logs one JSON object per line (``event``, ``site``, ``dataset``). Exits 1 if any dataset failed; a pending import is
not a failure. See CONTRACT.md, "Sync".
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry  # noqa: E402

CLONES = Path('/clones')
BOT_DIR = '/var/run/secrets/loris-forgejo-export/bot'
SERVICE_ACCOUNT = Path('/var/run/secrets/kubernetes.io/serviceaccount')
CREDENTIALS_MISSING = 'bot credentials not provisioned yet; run the reconciler'
BATCH = 1000
"""Keys per stage-content run (the worker contract accepts at most 1000)."""
IMPORTED_REF = 'refs/loris-forgejo-export/imported'
DROPPED_REF = 'refs/loris-forgejo-export/dropped'
BACKUP_REFS = 'refs/loris-forgejo-export/history-backups/'
DATASET_LABEL = 'scilus.net/bids-dataset'
JOB_NAME_MAX = 53
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
SPECIAL_UUIDS = {'00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002'}
UUID_RE = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
KEY_RE = re.compile(r'^[A-Z0-9]+(-[a-zA-Z][0-9]+)*--[A-Za-z0-9._,+=-]+$')
ANNEX_TARGET_RE = re.compile(r'(?:^|/)annex/objects/(?:[^/]+/)*([^/\n]+)\n?$')
POINTER_MAX = 1024
METADATA_SUFFIXES = ('.json', '.jsonld', '.tsv', '.tsv.gz', '.csv', '.xlsx', '.bval', '.bvec', '.vhdr', '.vmrk',
                     '.txt', '.md', '.rst', '.cff', '.bidsignore')
METADATA_NAMES = {'README', 'CHANGES', 'LICENSE'}
SKIPPED_PREFIXES = ('derivatives/', '.datalad/', '.git/')
# The helper answers from the environment of the git process only; nothing is stored in .git/config.
CREDENTIAL_HELPER = ('!f() { test "$1" = get || exit 0; '
                     'printf "username=%s\\npassword=%s\\n" "$LFX_GIT_USERNAME" "$LFX_GIT_PASSWORD"; }; f')


class SyncError(RuntimeError):
    """A dataset could not be synchronized; the message says why."""


class CredentialsMissing(SyncError):
    def __init__(self):
        super().__init__(CREDENTIALS_MISSING)


def log(event: str, site: str | None = None, dataset: str | None = None, **fields):
    """Print one JSON log line."""
    print(json.dumps({'event': event, 'site': site, 'dataset': dataset, **fields}, sort_keys=True), flush=True)


def bot_credentials() -> tuple[str, str]:
    """Read the export bot username and token from the mounted Secret; raise CredentialsMissing if absent."""
    directory = Path(os.environ.get('LFX_BOT_DIR') or BOT_DIR)
    try:
        username = (directory / 'username').read_text().strip()
        token = (directory / 'token').read_text().strip()
    except OSError:
        raise CredentialsMissing() from None
    if not username or not token:
        raise CredentialsMissing()
    return username, token


def git_auth_env() -> dict[str, str]:
    """Environment that makes git and git-annex (p2phttp) authenticate as the bot without storing credentials."""
    username, token = bot_credentials()
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', LFX_GIT_USERNAME=username, LFX_GIT_PASSWORD=token)
    count = int(env.get('GIT_CONFIG_COUNT') or 0)
    for offset, value in enumerate(('', CREDENTIAL_HELPER)):
        env[f'GIT_CONFIG_KEY_{count + offset}'] = 'credential.helper'
        env[f'GIT_CONFIG_VALUE_{count + offset}'] = value
    env['GIT_CONFIG_COUNT'] = str(count + 2)
    return env


def dataset_identity(site: str, dataset: str) -> str:
    """LORIS ``bids_runtime.dataset_id``: the SHA-256 of ``<site>/<dataset>``."""
    return sha256(f'{site}/{dataset}'.encode()).hexdigest()


def is_metadata(path: str) -> bool:
    """Whether a dataset file is BIDS metadata, which stays in the copy."""
    name = path.rsplit('/', 1)[-1]
    return name in METADATA_NAMES or name.endswith(METADATA_SUFFIXES)


def annex_key(target: bytes) -> str | None:
    """Return the git-annex key a symlink target or pointer file refers to, else None."""
    try:
        text = target.decode()
    except UnicodeDecodeError:
        return None
    match = ANNEX_TARGET_RE.search(text)
    return match.group(1) if match and KEY_RE.match(match.group(1)) else None


def run(*args, cwd=None, stdin: str | None = None, check=True, binary=False, env=None):
    """Run a command and return its stdout; raise SyncError with stderr on failure."""
    result = subprocess.run(args, cwd=cwd, input=stdin.encode() if stdin is not None else None,
                            capture_output=True, env=env)
    if check and result.returncode != 0:
        raise SyncError(f'{" ".join(map(str, args[:3]))} failed: {result.stderr.decode(errors="replace").strip()}')
    return result.stdout if binary else result.stdout.decode()


class Forgejo:
    """Forgejo client authenticated as the export bot (credentials read on every request)."""

    def __init__(self, url: str):
        self.url = url.rstrip('/')

    @staticmethod
    def authorization() -> str:
        return 'Basic ' + b64encode(':'.join(bot_credentials()).encode()).decode()

    def request(self, method: str, path: str, body=None):
        request = urllib.request.Request(self.url + '/api/v1' + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={'Authorization': self.authorization(),
                                                  'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise SyncError(f'Forgejo {method} {path}: HTTP {error.code} {error.read()[:300]!r}') from error
        except (urllib.error.URLError, OSError) as error:
            raise SyncError(f'Forgejo {method} {path}: {error}') from error
        return json.loads(payload) if payload else None

    def wait_ready(self, seconds: float, poll: float):
        """Wait until the Forgejo API answers, at most seconds."""
        deadline = time.monotonic() + seconds
        while True:
            try:
                with urllib.request.urlopen(self.url + '/api/v1/version', timeout=10):
                    return
            except (urllib.error.URLError, OSError) as error:
                if time.monotonic() > deadline:
                    raise SyncError(f'Forgejo API not reachable after {seconds:.0f}s: {error}') from error
            time.sleep(max(poll, 1))

    def annex_uuid(self, org: str, dataset: str) -> str:
        request = urllib.request.Request(f'{self.url}/{org}/{dataset}.git/config',
                                         headers={'Authorization': self.authorization()})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(65537)
        except urllib.error.HTTPError as error:
            raise SyncError(f'Forgejo annex configuration: HTTP {error.code}') from error
        except (urllib.error.URLError, OSError) as error:
            raise SyncError(f'Forgejo annex configuration: {error}') from error
        if len(data) > 65536:
            raise SyncError('Forgejo annex configuration exceeds supported size')
        identity = run('git', 'config', '--file', '-', '--get', 'annex.uuid', stdin=data.decode(errors='replace'),
                       check=False).strip()
        if not UUID_RE.fullmatch(identity):
            raise SyncError('Forgejo advertised an invalid annex UUID')
        return identity

    def stage(self, site, dataset: str, commit: str, keys: list[str], timeout: float, poll: float):
        """Dispatch stage-content on the site worker for keys and wait until the run succeeds."""
        actions = f'/repos/{site.org}/{site.worker_repository}/actions'
        run_id = self.request('POST', actions + '/workflows/stage-content.yml/dispatches', {
            'ref': 'main', 'return_run_info': True,
            'inputs': {'dataset': dataset, 'commit': commit, 'keys': ' '.join(keys)}})['id']
        log('stage-dispatched', site.name, dataset, run=run_id, keys=len(keys))
        deadline = time.monotonic() + timeout
        while True:
            status = self.request('GET', f'{actions}/runs/{run_id}')['status']
            if status == 'success':
                return
            if status in ('failure', 'cancelled', 'skipped'):
                raise SyncError(f'stage-content run {run_id} of {site.org} ended with {status}; see its log in Forgejo')
            if time.monotonic() > deadline:
                self.request('POST', f'{actions}/runs/{run_id}/cancel')
                raise SyncError(f'stage-content run {run_id} of {site.org} still {status} after {timeout:.0f}s '
                                '(cancellation requested; check site availability and queue capacity)')
            time.sleep(poll)


class Kubernetes:
    """Namespace-scoped Kubernetes API client (stdlib only)."""

    def __init__(self, base: str, namespace: str, token_file: Path | None = None,
                 context: ssl.SSLContext | None = None):
        self.base = base.rstrip('/')
        self.namespace = namespace
        self.token_file = token_file
        self.context = context

    @classmethod
    def from_environment(cls) -> 'Kubernetes':
        namespace = os.environ.get('LFX_NAMESPACE') or (SERVICE_ACCOUNT / 'namespace').read_text().strip()
        host = os.environ['KUBERNETES_SERVICE_HOST']
        if ':' in host:
            host = f'[{host}]'
        base = f'https://{host}:{os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get("KUBERNETES_SERVICE_PORT", "443")}'
        return cls(base, namespace, SERVICE_ACCOUNT / 'token',
                   ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / 'ca.crt')))

    def request(self, method: str, path: str, body=None, api: str = 'apis/batch/v1', text: bool = False):
        headers = {'Content-Type': 'application/json'}
        if self.token_file is not None:
            headers['Authorization'] = 'Bearer ' + self.token_file.read_text().strip()
        request = urllib.request.Request(f'{self.base}/{api}/namespaces/{self.namespace}/{path}', method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=30) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise SyncError(f'Kubernetes {method} {path}: HTTP {error.code} {error.read()[:300]!r}') from error
        except (urllib.error.URLError, OSError) as error:
            raise SyncError(f'Kubernetes {method} {path}: {error}') from error
        return payload.decode(errors='replace') if text else json.loads(payload)


def job_state(job: dict) -> str | None:
    """Return Complete or Failed for a finished Job, else None."""
    for condition in (job.get('status') or {}).get('conditions') or []:
        if condition.get('type') in ('Complete', 'Failed') and condition.get('status') == 'True':
            return condition['type']
    return None


class Importer:
    """Runs a site's LORIS discovery and verifies the import acknowledgement from the LORIS logs."""

    def __init__(self, kubernetes: Kubernetes, timeout: float, poll: float):
        self.kubernetes = kubernetes
        self.timeout = timeout
        self.poll = poll

    def wait(self, name: str, deadline: float) -> str:
        while True:
            state = job_state(self.kubernetes.request('GET', f'jobs/{name}'))
            if state:
                return state
            if time.monotonic() > deadline:
                raise SyncError(f'Job {name} did not finish in {self.timeout:.0f}s')
            time.sleep(self.poll)

    def unfinished(self, label: str) -> list[str]:
        jobs = self.kubernetes.request('GET', 'jobs?labelSelector=' + urllib.parse.quote(label))['items']
        return sorted(job['metadata']['name'] for job in jobs if not job_state(job))

    def records(self, job: str) -> list[dict]:
        """Return the JSON objects printed by the pods of a Job, one per log line."""
        pods = self.kubernetes.request('GET', 'pods?labelSelector=' + urllib.parse.quote(f'job-name={job}'),
                                       api='api/v1')['items']
        records = []
        for pod in sorted(pods, key=lambda pod: pod['metadata'].get('creationTimestamp') or ''):
            text = self.kubernetes.request('GET', f'pods/{pod["metadata"]["name"]}/log', api='api/v1', text=True)
            for line in text.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    @staticmethod
    def discovery_name(site) -> str:
        base = site.import_cronjob[:JOB_NAME_MAX - len('-lfx-') - 8].rstrip('-')
        return f'{base}-lfx-{secrets.token_hex(4)}'

    def start_discovery(self, site) -> str:
        template = self.kubernetes.request('GET', f'cronjobs/{site.import_cronjob}')['spec']['jobTemplate']
        metadata = template.get('metadata') or {}
        name = self.discovery_name(site)
        self.kubernetes.request('POST', 'jobs', {
            'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'name': name, 'labels': dict(metadata.get('labels') or {}),
                         'annotations': {**(metadata.get('annotations') or {}),
                                         'cronjob.kubernetes.io/instantiate': 'manual',
                                         'loris-forgejo-export/triggered-by': 'sync'}},
            'spec': template['spec']})
        return name

    def run(self, site, dataset: str) -> bool:
        """Run discovery for the published dataset; return whether LORIS acknowledged its import."""
        deadline = time.monotonic() + self.timeout
        identity = dataset_identity(site.name, dataset)
        label = f'{DATASET_LABEL}={identity[:32]}'
        while waiting := self.unfinished(label):
            if time.monotonic() > deadline:
                raise SyncError(f'earlier LORIS import Jobs {", ".join(waiting)} did not finish in {self.timeout:.0f}s')
            log('import-waiting', site.name, dataset, jobs=waiting)
            time.sleep(self.poll)

        name = self.start_discovery(site)
        log('discovery-started', site.name, dataset, job=name)
        if self.wait(name, deadline) != 'Complete':
            raise SyncError(f'LORIS discovery Job {name} failed; see its logs')
        records = self.records(name)
        if any(record.get('discovery') == 'already-active' for record in records):
            log('discovery-already-active', site.name, dataset, job=name)
            return False

        dispatched = [record for record in records if record.get('dataset') == identity and record.get('job')]
        if not dispatched:
            waiting = self.unfinished(label)
            if waiting:
                log('import-still-running', site.name, dataset, jobs=waiting)
                return False
            log('import-not-needed', site.name, dataset, job=name)
            return True

        dispatch = dispatched[-1]
        job = str(dispatch['job'])
        log('import-started', site.name, dataset, job=job, attempt=dispatch.get('attempt'))
        if self.wait(job, deadline) != 'Complete':
            raise SyncError(f'LORIS import Job {job} failed; see its logs')
        for record in self.records(job):
            result = record.get('result')
            if (record.get('dataset') == identity and record.get('attempt') == dispatch.get('attempt')
                    and isinstance(result, dict) and result.get('mode') == 'import'):
                log('import-acknowledged', site.name, dataset, job=job, result=result)
                return True
        log('import-deferred', site.name, dataset, job=job)
        return False


class DatasetSync:
    """Synchronizes one dataset of a managed site."""

    def __init__(self, site, dataset: str, config: dict, forgejo, importer: Importer | None):
        self.site = site
        self.dataset = dataset
        self.config = config
        self.forgejo = forgejo
        self.importer = importer
        self.clone = Path(config['clones']) / site.name / dataset
        self.staging = self.clone.with_name('.' + dataset + '.cloning')
        self.work = self.clone

    def log(self, event: str, **fields):
        log(event, self.site.name, self.dataset, **fields)

    def git(self, *args, remote=False, **kwargs):
        """Run git in the copy; remote operations get the bot credentials through the environment."""
        return run('git', *args, cwd=self.work, env=git_auth_env() if remote else None, **kwargs)

    def ancestor(self, old: str, new: str) -> bool:
        return subprocess.run(['git', 'merge-base', '--is-ancestor', old, new], cwd=self.work,
                              capture_output=True).returncode == 0

    def rev(self, ref: str) -> str:
        return self.git('rev-parse', '--verify', '-q', ref + '^{commit}', check=False).strip()

    def configure(self):
        """Store non-secret settings: annex p2phttp URL, allowed annex IPs and the committer identity."""
        settings = {'user.name': 'loris-forgejo-export sync', 'user.email': 'sync@loris-forgejo-export.invalid'}
        if self.config.get('annex_ips'):
            settings['annex.security.allowed-ip-addresses'] = self.config['annex_ips']
        if self.config.get('annex_url'):
            settings['remote.origin.annexUrl'] = self.config['annex_url']
        for key, value in settings.items():
            self.git('config', key, value)

    def fetch(self) -> str | None:
        """Clone or fetch the dataset; return the remote default-branch commit, or None for an empty repository."""
        if not (self.clone / '.git').is_dir():
            self.work = self.staging
            if not (self.staging / '.git').is_dir():
                shutil.rmtree(self.staging, ignore_errors=True)
                url = f'{self.config["forgejo_url"]}/{self.site.org}/{self.dataset}.git'
                run('git', 'clone', '-q', '--no-checkout', url, str(self.staging), env=git_auth_env())
                self.git('config', 'annex.private', 'true')
                self.configure()
                self.git('annex', 'init', '-q', 'loris-forgejo-export copy (private)', remote=True)
                self.log('cloned')
        self.configure()
        self.git('fetch', '-q', '--prune', 'origin', remote=True)
        self.git('remote', 'set-head', 'origin', '--auto', check=False, remote=True)
        self.git('annex', 'merge', '-q')
        identity = self.forgejo.annex_uuid(self.site.org, self.dataset)
        prior = self.git('config', '--get', 'remote.origin.annex-uuid', check=False).strip()
        if prior != identity:
            self.git('config', 'remote.origin.annex-uuid', identity)
            self.log('annex-identity-updated', previous=prior, current=identity)
        return self.rev('refs/remotes/origin/HEAD') or None

    def wanted_keys(self, new: str, base: str | None = None) -> dict[str, str]:
        """Return {key: path} of annexed files added or changed since base (default: the imported ref)."""
        old = (self.rev(base) if base else self.rev(IMPORTED_REF)) or EMPTY_TREE
        fields = self.git('diff-tree', '-r', '-z', '--no-renames', '--diff-filter=AM', old, new,
                          binary=True).split(b'\0')
        paths: dict[str, list[str]] = {}
        for meta, path in zip(fields[0::2], fields[1::2]):
            _, mode, _, blob, _ = meta.decode().lstrip(':').split(' ')
            name = path.decode()
            if mode in ('120000', '100644', '100755') and not name.startswith(SKIPPED_PREFIXES):
                paths.setdefault(blob, []).append(name)
        if not paths:
            return {}
        sizes = self.git('cat-file', '--batch-check=%(objectname) %(objectsize)', stdin='\n'.join(paths) + '\n')
        small = [blob for blob, size in (line.split() for line in sizes.splitlines()) if int(size) <= POINTER_MAX]
        output = self.git('cat-file', '--batch', stdin=''.join(blob + '\n' for blob in small), binary=True)
        keys: dict[str, str] = {}
        offset = 0
        while offset < len(output):
            end = output.index(b'\n', offset)
            blob, _, size = output[offset:end].decode().split(' ')
            content = output[end + 1:end + 1 + int(size)]
            offset = end + 2 + int(size)
            key = annex_key(content)
            if key:
                for name in paths[blob]:
                    keys.setdefault(key, name)
        return keys

    def batch(self, args: list[str], keys: list[str], remote=False) -> list[dict]:
        """Run a git-annex ``--batch-keys --json`` command over keys and return one record per key."""
        if not keys:
            return []
        output = self.git('annex', *args, '--batch-keys', '--json', '--json-error-messages',
                          stdin=''.join(key + '\n' for key in keys), check=False, remote=remote)
        return [json.loads(line) for line in output.splitlines() if line.startswith('{')]

    def locations(self, keys: list[str]) -> dict[str, dict]:
        """Return {key: {'here', 'origin', 'site'}} presence flags from the location log."""
        origin = self.git('config', '--get', 'remote.origin.annex-uuid', check=False).strip()
        here = self.git('config', '--get', 'annex.uuid').strip()
        result = {key: {'here': False, 'origin': False, 'site': False} for key in keys}
        for record in self.batch(['whereis'], keys):
            uuids = {entry['uuid'] for entry in record.get('whereis', []) + record.get('untrusted', [])}
            result[record['key']] = {'here': here in uuids, 'origin': bool(origin) and origin in uuids,
                                     'site': bool(uuids - {here, origin} - SPECIAL_UUIDS)}
        return result

    def stage(self, commit: str, keys: list[str]):
        """Have the site worker upload keys, then refresh the location log."""
        for start in range(0, len(keys), BATCH):
            self.forgejo.stage(self.site, self.dataset, commit, keys[start:start + BATCH],
                               self.config['stage_timeout'], self.config['poll'])
        self.git('fetch', '-q', 'origin', 'git-annex', remote=True)
        self.git('annex', 'merge', '-q')

    def get(self, keys: list[str]):
        got = {record['key'] for record in self.batch(['get', '--from', 'origin'], keys, remote=True)
               if record.get('success')}
        failed = [key for key in keys if key not in got]
        if failed:
            raise SyncError(f'could not get {len(failed)}/{len(keys)} keys from Forgejo, e.g. {failed[0]}')

    def hydrate(self, commit: str, keys: dict[str, str]):
        locations = self.locations(list(keys))
        missing = sorted(key for key in keys if not locations[key]['here'])
        to_stage = [key for key in missing if not locations[key]['origin']]
        self.log('plan', commit=commit, keys=len(keys), missing=len(missing), stage=len(to_stage))
        if to_stage:
            self.stage(commit, to_stage)
        self.get(missing)

    def checkout(self, commit: str):
        """Publish commit in the work tree LORIS reads; publish a new copy at its LORIS path."""
        head = self.rev('HEAD')
        if self.work == self.clone and head and self.ancestor(head, commit):
            self.git('merge', '-q', '--ff-only', commit)
        else:
            # A new copy, an unimported force-push, or the approved transition (history was checked in _run).
            self.git('reset', '-q', '--hard', commit)
        if self.work != self.clone:
            self.staging.rename(self.clone)
            self.work = self.clone
            self.log('published', path=str(self.clone))

    def check_history_transition(self, current: str, commit: str):
        approval = self.config.get('history_transition')
        expected = {'repository': f'{self.site.org}/{self.dataset}', 'old': current, 'new': commit}
        if approval != expected:
            raise SyncError(f'{self.site.org}/{self.dataset}: history was rewritten ({current[:12]} is not an '
                            f'ancestor of {commit[:12]}); registered LORIS files may break, review before importing')

        def raw_tree(ref):
            entries = {}
            for record in self.git('ls-tree', '-r', '-z', ref, binary=True).split(b'\0'):
                if not record:
                    continue
                metadata, path = record.split(b'\t', 1)
                mode, _, blob = metadata.decode().split()
                name = path.decode()
                if name.split('/', 1)[0].startswith('sub-'):
                    entries[name] = (mode, blob)
            return entries

        old, new = raw_tree(current), raw_tree(commit)
        if not old:
            raise SyncError('history transition requires an existing raw BIDS tree to compare')
        missing = old.keys() - new.keys()
        if missing:
            raise SyncError(f'history transition refused: {len(missing)} previously imported raw BIDS files missing')
        for path, (mode, blob) in old.items():
            if new[path] == (mode, blob):
                continue
            new_mode, new_blob = new[path]
            old_key = annex_key(self.git('cat-file', 'blob', blob, binary=True))
            if is_metadata(path) and new_mode in ('100644', '100755') and old_key:
                # Legacy metadata may have been annexed. Accept only identical bytes.
                match = re.fullmatch(r'SHA256E?-s[0-9]+--([0-9a-f]{64})(?:\..*)?', old_key)
                if match and sha256(self.git('cat-file', 'blob', new_blob, binary=True)).hexdigest() == match[1]:
                    continue
            raise SyncError('history transition refused: previously imported raw BIDS content or pointer changed')
        self.git('update-ref', BACKUP_REFS + current, current)
        self.log('history-transition-verified', old=current, new=commit, preserved_raw_files=len(old))

    def drop(self, keys: dict[str, str], local: bool):
        """Drop transit copies on Forgejo, and local imaging content when local, where a site copy is recorded.

        Keys with a provider ledger row stay here; the provider expires them. Metadata is never dropped.
        """
        locations = self.locations(list(keys))
        safe = [key for key in keys if locations[key]['site']]
        cached = ledger_keys(self.config.get('ledger'), self.site.name, self.dataset) if local else set()
        here_keys = [key for key in safe if local and locations[key]['here'] and not is_metadata(keys[key])
                     and key not in cached]
        transit = [key for key in safe if locations[key]['origin']]
        here = [r['key'] for r in self.batch(['drop', '--force'], here_keys) if r.get('success')]
        origin = [r['key'] for r in self.batch(['drop', '--from', 'origin', '--force'], transit, remote=True)
                  if r.get('success')]
        if origin:
            self.git('annex', 'sync', '-q', '--no-content', '--only-annex', '--no-pull', 'origin', remote=True)
        self.log('dropped', here=len(here), transit=len(origin), kept_without_site_copy=len(keys) - len(safe),
                 kept_in_cache=len(cached & set(safe)), kept_unacknowledged=not local)
        if len(here) != len(here_keys) or len(origin) != len(transit):
            raise SyncError(f'dropped {len(here)}/{len(here_keys)} local and {len(origin)}/{len(transit)} '
                            'transit keys')

    def drop_pending(self, current: str):
        """Retry the local drop of imported keys that an earlier failed drop left behind."""
        if not current or self.rev(DROPPED_REF) == current:
            return
        keys = self.wanted_keys(current, DROPPED_REF)
        if keys:
            self.drop(keys, local=True)
        self.git('update-ref', DROPPED_REF, current)

    def run(self) -> dict:
        self.clone.parent.mkdir(parents=True, exist_ok=True)
        with open(self.clone.parent / f'.{self.dataset}.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._run()

    def _run(self) -> dict:
        new = self.fetch()
        if new is None:
            return {'status': 'empty'}
        current = self.rev(IMPORTED_REF)
        if current and not self.ancestor(current, new):
            self.check_history_transition(current, new)
        if not self.site.import_enabled or self.importer is None:
            self.checkout(new)
            return {'status': 'fetched', 'commit': new}
        error = None
        try:
            self.drop_pending(current)
        except SyncError as failure:
            error = failure
        if current == new:
            self.checkout(new)
            if error:
                raise error
            return {'status': 'up-to-date', 'commit': new}

        keys = self.wanted_keys(new)
        acknowledged = False
        try:
            self.hydrate(new, keys)
            self.checkout(new)
            acknowledged = self.importer.run(self.site, self.dataset)
            if acknowledged:
                self.git('update-ref', IMPORTED_REF, new)
            else:
                self.log('import-pending', commit=new)
        except SyncError as failure:
            error = failure
        try:
            self.drop(keys, local=acknowledged)
            if acknowledged and self.rev(DROPPED_REF) == (current or ''):
                self.git('update-ref', DROPPED_REF, new)
        except SyncError as failure:
            error = error or failure
        if error:
            raise error
        return {'status': 'imported' if acknowledged else 'import-pending', 'commit': new, 'keys': len(keys)}


def ledger_keys(path: str | None, site: str, dataset: str) -> set[str]:
    """Return the keys the provider ledger holds for a dataset (read-only); empty without a ledger."""
    if not path or not Path(path).is_file():
        return set()
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30)
    try:
        return {row[0] for row in connection.execute(
            'SELECT key FROM content WHERE site = ? AND dataset = ?', (site, dataset))}
    except sqlite3.OperationalError as error:
        if 'no such table' in str(error):
            return set()
        raise SyncError(f'cannot read the provider ledger: {error}') from error
    finally:
        connection.close()


def targets(reg, repository: str | None) -> list | None:
    """Return the (site, dataset) pairs to synchronize, or None when repository is not a managed dataset."""
    if not repository:
        return [(site, dataset) for site in reg.sites if site.enabled for dataset in site.datasets]
    found = reg.by_repository(repository)
    return [found] if found else None


def configuration() -> dict:
    forgejo_url = os.environ['LFX_FORGEJO_URL'].rstrip('/')
    annex_url = os.environ.get('LFX_ANNEX_URL', '')
    if not annex_url and forgejo_url.startswith(('http://', 'https://')):
        annex_url = 'annex+' + forgejo_url + '/git-annex-p2phttp'
    hosts = {urllib.parse.urlsplit(url.removeprefix('annex+')).hostname for url in (forgejo_url, annex_url)
             if url.startswith(('http://', 'https://', 'annex+'))} - {None}
    addresses = set()
    for host in hosts:
        try:
            addresses |= {info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
        except OSError as error:
            raise SyncError(f'cannot resolve {host}: {error}') from error
    names = ('LFX_HISTORY_REPOSITORY', 'LFX_HISTORY_OLD', 'LFX_HISTORY_NEW')
    values = [os.environ.get(name) for name in names]
    if any(values) and (not all(values) or not all(re.fullmatch(r'[0-9a-f]{40}', value) for value in values[1:])):
        raise SyncError('history transition requires one repository and full old/new commit IDs')
    return {
        'history_transition': dict(zip(('repository', 'old', 'new'), values)) if all(values) else None,
        'forgejo_url': forgejo_url,
        'annex_url': annex_url,
        'annex_ips': ' '.join(sorted(addresses)),
        'clones': str(CLONES),
        'ledger': str(CLONES / '.cache-ledger.sqlite'),
        'stage_timeout': float(os.environ.get('LFX_STAGE_TIMEOUT') or 10800),
        'import_timeout': float(os.environ.get('LFX_IMPORT_TIMEOUT') or 10800),
        'poll': float(os.environ.get('LFX_POLL_SECONDS') or 5),
        'wait': float(os.environ.get('LFX_WAIT_SECONDS') or 300),
    }


def prepare_site_directories(reg, clones: Path) -> int:
    """Create /clones/<site> for every managed site, so the kubelet never creates it root-owned for LORIS.

    Return the failure count.
    """
    failures = 0
    for site in reg.sites:
        path = clones / site.name
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            failures += 1
            log('failed', site.name, error=f'cannot create {path}: {error}')
            continue
        if not os.access(path, os.W_OK | os.X_OK):
            failures += 1
            log('failed', site.name, error=f'{path} is not writable; give it to the LORIS data owner')
    return failures


def main(argv: list[str]) -> int:
    os.umask(0o022)
    repository = (argv[1] if len(argv) > 1 else os.environ.get('LFX_SYNC_REPOSITORY')) or None
    try:
        reg = registry.load(os.environ.get('LFX_REGISTRY', '/etc/loris-forgejo-export/registry.json'))
    except (registry.RegistryError, OSError, ValueError) as error:
        log('failed', error=str(error))
        return 1
    selected = targets(reg, repository)
    if selected is None:
        log('failed', repository=repository, error='not a managed dataset repository')
        return 1
    try:
        bot_credentials()
        config = configuration()
    except (SyncError, KeyError, ValueError) as error:
        log('failed', error=str(error) if not isinstance(error, KeyError) else f'missing environment {error}')
        return 1
    failures = prepare_site_directories(reg, Path(config['clones']))
    work = []
    for site, dataset in selected:
        if site.enabled:
            work.append((site, dataset))
        else:
            log('skipped', site.name, dataset, reason='site disabled')
    if not work:
        return 1 if failures else 0
    forgejo = Forgejo(config['forgejo_url'])
    try:
        forgejo.wait_ready(config['wait'], config['poll'])
        importer = None
        if any(site.import_enabled for site, _ in work):
            importer = Importer(Kubernetes.from_environment(), config['import_timeout'], config['poll'])
    except (SyncError, KeyError, OSError) as error:
        log('failed', error=str(error))
        return 1
    for site, dataset in work:
        try:
            result = DatasetSync(site, dataset, config, forgejo, importer).run()
            log('synced', site.name, dataset, **result)
        except (SyncError, OSError) as error:
            failures += 1
            log('failed', site.name, dataset, error=str(error))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
