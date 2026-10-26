"""Discover site datasets and run ledger-backed, resumable LORIS imports.

Argo manages per-site discovery CronJobs; discovery creates immutable worker
attempts at runtime. MariaDB, not Kubernetes Job history, records completion.
Dataset identity is the site/folder pair, while the registered import-view path
stays stable across attempts so upstream can recognize already registered files.

The CLI consumes a generated runtime.json containing namespace, cronJob, site,
and common bidsImport settings. Database credentials come from the LORIS config.py
profile and environment, never from the runtime configuration or ledger.
Filesystem helper arguments are pathlib.Path objects; settings dictionaries are
the validated output of resolve_settings, not raw Helm values.
"""

import argparse
from contextlib import contextmanager, nullcontext
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import ssl
import sys
import urllib.error
import urllib.request

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, insert, select, text, update


ROOT = Path('/data/loris/data')
METADATA = MetaData()
VERSION = Table('scilus_bids_schema', METADATA,
                Column('id', Integer, primary_key=True), Column('version', Integer, nullable=False))
DATASETS = Table(
    'scilus_bids_datasets', METADATA,
    Column('id', String(64), primary_key=True),
    Column('site', String(63), nullable=False),
    Column('folder', String(255), nullable=False),
    Column('metadata_name', String(255), nullable=False),
    Column('fingerprint', String(64), nullable=False),
    Column('settings', Text, nullable=False),
    Column('view_path', Text, nullable=False),
    Column('status', String(20), nullable=False),
    Column('mode', String(20), nullable=False),
    Column('completed_modes', Text, nullable=False),
    Column('attempt', Integer, nullable=False),
    Column('job_name', String(63)),
    Column('updated_at', String(40), nullable=False),
    Column('error', Text),
)
ATTEMPTS = Table(
    'scilus_bids_attempts', METADATA,
    Column('dataset_id', String(64), primary_key=True),
    Column('number', Integer, primary_key=True),
    Column('job_name', String(63), nullable=False),
    Column('status', String(20), nullable=False),
    Column('updated_at', String(40), nullable=False),
    Column('error', Text),
)
SETTING_KEYS = {
    'project', 'projectAlias', 'cohort', 'recruitmentTarget', 'defaultVisit',
    'datasetType', 'createCandidates', 'createSessions', 'mode',
}
IDENTITY_KEYS = ('project', 'projectAlias', 'siteAlias', 'cohort', 'defaultVisit',
                 'datasetType', 'recruitmentTarget')


def now():
    """Return an ISO 8601 UTC timestamp for ledger audit fields."""
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    """Serialize a JSON-compatible value deterministically for storage or requests."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def helper(name):
    """Load a sibling Python module by basename, including hyphenated scripts.

    A fresh module is executed on each call; imports and execution errors propagate.
    """
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dataset_id(site, folder):
    """Return the stable SHA-256 identity of a site and immediate dataset folder."""
    return hashlib.sha256((site + '/' + folder).encode()).hexdigest()


def run_name(site, name, identity, attempt):
    """Build a DNS-compatible Job name, bounded to 63 characters.

    Human-readable site/dataset fragments are supplemented by an identity digest
    and attempt number so truncation or sanitization does not define identity.
    """
    suffix = '-' + identity[:12] + '-' + str(attempt)
    budget = 63 - len('loris-bids-') - len(suffix) - 1
    site_slug = re.sub('[^a-z0-9]+', '-', site.lower()).strip('-') or 'site'
    name_slug = re.sub('[^a-z0-9]+', '-', name.lower()).strip('-') or 'dataset'
    site_slug = site_slug[:min(16, budget // 2)].rstrip('-')
    name_slug = name_slug[:budget - len(site_slug)].rstrip('-')
    return 'loris-bids-' + site_slug + '-' + name_slug + suffix


def resolve_settings(config, folder, metadata_name):
    """Return validated registration settings for one discovered dataset.

    Folder overrides take precedence over site settings and common defaults.
    Explicit false/null values survive merging; an empty or absent cohort falls
    back to dataset_description.json Name. Invalid settings raise ValueError.
    This function neither changes config nor performs database or filesystem writes.
    """
    site = config['site']
    settings = dict(config['bidsImport']['defaults'])
    settings.update({key: value for key, value in site['bidsImport'].items() if key in SETTING_KEYS})
    override = site['bidsImport'].get('datasets', {}).get(folder, {})

    unknown = set(override) - SETTING_KEYS - {'enabled'}
    if unknown:
        raise ValueError('Unknown dataset override keys: ' + ', '.join(sorted(unknown)))

    settings.update({key: value for key, value in override.items() if key in SETTING_KEYS})
    settings['siteAlias'] = site['siteAlias']
    settings['cohort'] = settings.get('cohort') or metadata_name
    for key in ('project', 'cohort'):
        if not isinstance(settings.get(key), str) or not settings[key].strip() or len(settings[key]) > 255:
            raise ValueError(key + ' must be a nonempty string of at most 255 characters')

    for key in ('projectAlias', 'siteAlias'):
        if not isinstance(settings.get(key), str) or not 1 <= len(settings[key]) <= 4:
            raise ValueError(key + ' must contain 1-4 characters')

    target = settings.get('recruitmentTarget')
    if target is not None and (type(target) is not int or target <= 0):
        raise ValueError('recruitmentTarget must be a positive integer or null')

    if settings['mode'] not in ('preflight', 'setup', 'import'):
        raise ValueError('mode must be preflight, setup or import')

    if settings['datasetType'] not in ('raw', 'derivative'):
        raise ValueError('datasetType must be raw or derivative')

    if not isinstance(settings['defaultVisit'], str) or not settings['defaultVisit']:
        raise ValueError('defaultVisit must be a nonempty string')

    for key in ('createCandidates', 'createSessions'):
        if type(settings[key]) is not bool:
            raise ValueError(key + ' must be boolean')

    return settings


def discover_datasets(source, overrides):
    """Yield immediate dataset candidates in deterministic folder-name order.

    Hidden folders, derivatives, and explicitly disabled overrides are excluded.
    The override map is not an allowlist. Symlinks are yielded so the caller can
    report them as individual failures without blocking unrelated datasets.
    A dataset directly at the export root raises ValueError.
    """
    if not source.is_dir() or not os.access(source, os.R_OK | os.X_OK):
        raise ValueError('Site export root is not readable/traversable')

    if (source / 'dataset_description.json').exists():
        raise ValueError('Export root is a dataset; migrate to a shared root containing dataset folders')

    for folder in sorted(source.iterdir()):
        if folder.name.startswith('.') or folder.name == 'derivatives':
            continue

        if overrides.get(folder.name, {}).get('enabled', True) is False:
            continue

        if folder.is_symlink() or (folder.is_dir() and (folder / 'dataset_description.json').exists()):
            yield folder


def read_name(source):
    """Return a nonempty dataset_description.json Name of at most 255 characters.

    Filesystem and JSON decoding errors propagate; invalid Names raise ValueError.
    """
    with (source / 'dataset_description.json').open() as handle:
        metadata = json.load(handle)

    name = metadata.get('Name') if isinstance(metadata, dict) else None
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise ValueError('dataset_description.json Name must be a nonempty string of at most 255 characters')

    return name


def source_fingerprint(source):
    """Hash relative file paths and contents to detect published dataset changes.

    Root-level derivatives are excluded. File symlinks must resolve within the
    dataset; directory symlinks and non-regular files are rejected. The entire
    included dataset is read, so this check can be expensive on large exports.
    """
    if not source.is_dir():
        raise ValueError('Dataset source does not exist')

    digest = hashlib.sha256()

    def unreadable(error):
        """Propagate traversal errors instead of hashing a silently incomplete tree."""
        raise error

    for current, directories, files in os.walk(source, followlinks=False, onerror=unreadable):
        current = Path(current)
        if current == source:
            directories[:] = [name for name in directories if name != 'derivatives']

        for name in directories:
            if (current / name).is_symlink():
                raise ValueError('Source contains a symlinked directory')

        for name in sorted(files):
            path = current / name
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(source.resolve()):
                raise ValueError('Source file escapes the dataset root')

            if not resolved.is_file():
                raise ValueError('Dataset contains a non-regular source file')

            digest.update(encoded(str(path.relative_to(source))).encode())
            content = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    content.update(block)

            digest.update(content.digest())

        directories.sort()

    return digest.hexdigest()


@contextmanager
def database_lock(engine, name, timeout=0):
    """Yield whether a named MariaDB advisory lock was acquired.

    The lock uses a dedicated connection and a re-move-loris- prefix. timeout is
    the maximum acquisition wait in seconds; zero is nonblocking. Acquired locks
    are released on exit, including exceptional exits. Unsupported dialects or
    a lost lock raise an error rather than implying mutual exclusion.
    """
    with engine.connect() as connection:
        if engine.dialect.name != 'mysql':
            raise ValueError('Scheduled imports require MariaDB advisory locks')

        acquired = connection.scalar(text('SELECT GET_LOCK(:name, :timeout)'),
                                     {'name': 're-move-loris-' + name, 'timeout': timeout})

        if acquired != 1:
            yield False
            return

        try:
            yield True
        finally:
            released = connection.scalar(text('SELECT RELEASE_LOCK(:name)'), {'name': 're-move-loris-' + name})
            if released != 1:
                raise RuntimeError('Import database lock was lost')


def initialize(engine):
    """Create missing importer-owned tables and validate ledger schema version.

    Schema initialization is serialized with a bounded advisory-lock wait.
    Creating tables requires schema privileges; normal discovery can call this
    with the application account after the privileged init container provisions
    them. Upstream LORIS tables and application grants are not modified.
    """
    with database_lock(engine, 'schema', timeout=30) as acquired:
        if not acquired:
            raise RuntimeError('Import ledger initialization is already running; retry discovery')

        METADATA.create_all(engine)
        with engine.begin() as connection:
            version = connection.scalar(select(VERSION.c.version).where(VERSION.c.id == 1))
            if version is None:
                connection.execute(insert(VERSION).values(id=1, version=1))
            elif version != 1:
                raise ValueError('Unsupported import ledger schema version')


def get_dataset(engine, identity):
    """Return a detached ledger-row dictionary, or None if identity is unknown."""
    with engine.connect() as connection:
        row = connection.execute(select(DATASETS).where(DATASETS.c.id == identity)).mappings().one_or_none()

    return dict(row) if row else None


def registration(settings):
    """Extract settings whose changes require identity review rather than retry."""
    return {key: settings.get(key) for key in IDENTITY_KEYS}


def enroll(engine, site, folder, settings, metadata_name, view_path=None, fingerprint=None):
    """Register an immutable dataset definition and return its stable identity.

    Existing definitions must match registration settings, Name, and fingerprint;
    a known Name under another folder is rejected as a possible rename. An
    omitted fingerprint is computed from the site's source tree. view_path can
    preserve a verified legacy view; otherwise a deterministic path is assigned.
    Callers must hold the site's discovery lock to serialize enrollment.
    """
    identity = dataset_id(site, folder)
    fingerprint = fingerprint or source_fingerprint(ROOT / 'assembly_bids' / site / folder)
    with engine.begin() as connection:
        row = connection.execute(select(DATASETS).where(DATASETS.c.id == identity)).mappings().one_or_none()
        if row is not None:
            if row['metadata_name'] != metadata_name or registration(json.loads(row['settings'])) != registration(settings):
                raise ValueError('Known dataset identity/configuration changed; review migration instead of reimporting')

            if row['fingerprint'] != fingerprint:
                raise ValueError('Published dataset content changed; review migration instead of reimporting')

            return identity

        renamed = connection.execute(select(DATASETS.c.folder).where(
            DATASETS.c.site == site, DATASETS.c.metadata_name == metadata_name,
        )).first()
        if renamed is not None:
            raise ValueError('Dataset Name already known under another folder; review migration')

        connection.execute(insert(DATASETS).values(
            id=identity,
            site=site,
            folder=folder,
            metadata_name=metadata_name,
            fingerprint=fingerprint,
            settings=encoded(settings),
            view_path=str(view_path or ROOT / 'assembly_bids' / ('import-' + identity[:24])),
            status='new',
            mode=settings['mode'],
            completed_modes='[]',
            attempt=0,
            updated_at=now(),
        ))

    return identity


def adopt_dataset(engine, site, folder, settings, metadata_name, view_path, locked=False):
    """Mark an externally verified legacy import complete without executing it.

    The migration caller must verify records and view contents before adoption.
    Active attempts or conflicting definitions/paths raise ValueError. All modes
    are marked complete so discovery will not import this dataset again.
    locked=True requires the caller to already hold the site's discovery lock.
    """
    initialize(engine)
    with nullcontext(True) if locked else database_lock(engine, 'discovery-' + site) as acquired:
        if not acquired:
            raise RuntimeError('Discovery is active; stop it before migration')

        identity = enroll(engine, site, folder, settings, metadata_name, view_path)
        row = get_dataset(engine, identity)
        if row['status'] in ('reserved', 'running'):
            raise ValueError('Cannot adopt a dataset with an active attempt')

        if row['view_path'] != str(view_path):
            raise ValueError('Ledger view path conflicts with migrated view')

        with engine.begin() as connection:
            connection.execute(update(DATASETS).where(DATASETS.c.id == identity).values(
                status='succeeded',
                mode='import',
                completed_modes=encoded(['preflight', 'setup', 'import']),
                updated_at=now(),
                error=None,
            ))

    return identity


def record(engine, identity, attempt, status, error=None):
    """Atomically update the current dataset and its attempt audit record.

    The supplied attempt must still be current. Successful modes accumulate:
    import includes setup/preflight, but preflight alone never completes import.
    error is persisted for diagnosis and must not contain credentials.
    """
    with engine.begin() as connection:
        row = connection.execute(select(DATASETS).where(
            DATASETS.c.id == identity, DATASETS.c.attempt == attempt,
        ).with_for_update()).mappings().one()

        completed = json.loads(row['completed_modes'])
        if status == 'succeeded':
            completed = sorted(set(completed) | {
                'preflight': {'preflight'}, 'setup': {'preflight', 'setup'},
                'import': {'preflight', 'setup', 'import'},
            }[row['mode']])

        connection.execute(update(DATASETS).where(DATASETS.c.id == identity).values(
            status=status, error=error, updated_at=now(), completed_modes=encoded(completed),
        ))
        connection.execute(update(ATTEMPTS).where(
            ATTEMPTS.c.dataset_id == identity, ATTEMPTS.c.number == attempt,
        ).values(status=status, error=error, updated_at=now()))


class ApiError(RuntimeError):
    """Represent a Kubernetes HTTP failure with its numeric status preserved."""

    def __init__(self, status, message):
        """Format a diagnostic message and retain status for explicit handling."""
        super().__init__('Kubernetes API HTTP ' + str(status) + ': ' + message)
        self.status = status


class Kubernetes:
    """Minimal namespace-scoped Kubernetes client using in-cluster credentials.

    Supports the batch Jobs/CronJobs and core ConfigMaps needed by discovery.
    Workers do not use this client or receive an API service-account token.
    """

    def __init__(self, namespace):
        """Configure API address and trusted CA from the discovery pod environment."""
        self.namespace = namespace
        self.base = 'https://' + os.environ['KUBERNETES_SERVICE_HOST'] + ':' + os.environ.get(
            'KUBERNETES_SERVICE_PORT_HTTPS', '443')
        self.credentials = Path('/var/run/secrets/kubernetes.io/serviceaccount')
        self.context = ssl.create_default_context(cafile=str(self.credentials / 'ca.crt'))

    def path(self, kind, name=''):
        """Return the collection or named-resource API path within this namespace."""
        group = '/apis/batch/v1' if kind in ('jobs', 'cronjobs') else '/api/v1'
        return group + '/namespaces/' + self.namespace + '/' + kind + ('/' + name if name else '')

    def request(self, kind, name='', method='GET', body=None):
        """Send a JSON request and return the decoded API response.

        Reload the service-account token for each request to support rotation.
        PATCH uses JSON merge-patch semantics. HTTP failures become ApiError;
        transport, TLS, and decoding errors propagate without a success fallback.
        """
        token = (self.credentials / 'token').read_text().strip()
        request = urllib.request.Request(
            self.base + self.path(kind, name),
            data=encoded(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + token,
                     'Content-Type': 'application/merge-patch+json' if method == 'PATCH' else 'application/json'},
            method=method,
        )

        try:
            with urllib.request.urlopen(request, context=self.context, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ApiError(error.code, error.reason) from error

    def get(self, kind, name):
        """Fetch a named resource, returning None only for an HTTP 404."""
        try:
            return self.request(kind, name)
        except ApiError as error:
            if error.status == 404:
                return None
            raise


def job_terminal(job):
    """Return Complete/Failed for a true terminal Job condition, otherwise None."""
    for condition in job.get('status', {}).get('conditions', []):
        if condition.get('status') == 'True' and condition.get('type') in ('Complete', 'Failed'):
            return condition['type']

    return None


def reserve(engine, identity, settings):
    """Allocate and persist the next attempt under a dataset-row lock.

    Return the updated row, or None if an attempt is active or the requested mode
    already completed. Reservation commits before Kubernetes submission, allowing
    reconciliation to reuse the same deterministic Job name after interruptions.
    """
    with engine.begin() as connection:
        row = connection.execute(select(DATASETS).where(
            DATASETS.c.id == identity,
        ).with_for_update()).mappings().one()
        if row['status'] in ('reserved', 'running') or settings['mode'] in json.loads(row['completed_modes']):
            return None

        attempt = row['attempt'] + 1
        name = run_name(row['site'], row['metadata_name'], identity, attempt)
        connection.execute(update(DATASETS).where(DATASETS.c.id == identity).values(
            status='reserved',
            mode=settings['mode'],
            attempt=attempt,
            job_name=name,
            settings=encoded(settings),
            updated_at=now(),
            error=None,
        ))
        connection.execute(insert(ATTEMPTS).values(
            dataset_id=identity, number=attempt, job_name=name, status='reserved', updated_at=now(),
        ))

    return get_dataset(engine, identity)


def owner(resource):
    """Build a non-controller owner reference for garbage collection.

    blockOwnerDeletion is disabled so discovery needs no delete permission on
    owners. Ownership does not make runtime Jobs declarative Argo resources.
    """
    return {
        'apiVersion': resource['apiVersion'],
        'kind': resource['kind'],
        'name': resource['metadata']['name'],
        'uid': resource['metadata']['uid'],
        'controller': False,
        'blockOwnerDeletion': False
    }


def dispatch(api, cron, profile, row):
    """Submit a reserved worker using immutable, attempt-specific inputs.

    Snapshot profile data and the admitted Job spec before creating the Job.
    Repeated dispatch reuses that snapshot even if the CronJob/image changed.
    Strip discovery's privileged initializer, service account, and Argo metadata;
    workers receive only application credentials. Reparent the snapshot to the
    created Job for cleanup. Identity conflicts raise rather than replace objects.
    """
    name = row['job_name']
    frozen_name = name

    spec = copy.deepcopy(cron['spec']['jobTemplate']['spec'])
    spec['backoffLimit'] = 0

    pod = spec['template']['spec']
    pod.pop('initContainers', None)
    pod.pop('serviceAccountName', None)
    pod['automountServiceAccountToken'] = False

    container = pod['containers'][0]
    container['command'] = ['python3', '/import-profile/bids_runtime.py']
    container['args'] = ['worker', '--config', '/import-profile/runtime.json']
    container.setdefault('env', []).extend([
        {'name': 'BIDS_DATASET_ID', 'value': row['id']},
        {'name': 'BIDS_ATTEMPT', 'value': str(row['attempt'])},
    ])

    for volume in pod['volumes']:
        if volume['name'] == 'profile':
            volume['configMap']['name'] = frozen_name

    spec['template']['metadata'] = {
        'labels': {
            'app.kubernetes.io/name': 'loris-bids',
            'app.kubernetes.io/component': 'bids-import'
        }
    }
    job = {
        'apiVersion': 'batch/v1',
        'kind': 'Job',
        'metadata': {
            'name': name,
            'ownerReferences': [owner(cron)],
            'labels': {
                'app.kubernetes.io/name': 'loris-bids',
                'scilus.net/bids-dataset': row['id'][:32]
            }
        },
        'spec': spec
    }

    existing = api.get('configmaps', frozen_name)
    if existing is None:
        data = dict(profile['data'], **{'attempt-job.json': encoded(job)})
        frozen = {
            'apiVersion': 'v1',
            'kind': 'ConfigMap',
            'immutable': True,
            'metadata': {
                'name': frozen_name,
                'ownerReferences': [owner(cron)],
                'labels': {'scilus.net/bids-dataset': row['id'][:32]}
            },
            'data': data
        }
        try:
            existing = api.request('configmaps', method='POST', body=frozen)
        except ApiError as error:
            if error.status != 409:
                raise

            existing = api.get('configmaps', frozen_name)

    if existing is None or existing['metadata'].get('labels', {}).get('scilus.net/bids-dataset') != row['id'][:32]:
        raise ValueError('Frozen attempt profile identity conflicts')

    frozen_job = existing['data'].get('attempt-job.json')
    if frozen_job is None:
        raise ValueError('Frozen attempt profile has no admitted Job specification')

    job = json.loads(frozen_job)
    try:
        created = api.request('jobs', method='POST', body=job)
    except ApiError as error:
        if error.status != 409:
            raise

        created = api.get('jobs', name)

    if created is None or created['metadata'].get('labels', {}).get('scilus.net/bids-dataset') != row['id'][:32]:
        raise ValueError('Attempt Job identity conflicts')

    api.request('configmaps', frozen_name, method='PATCH',
                body={'metadata': {'ownerReferences': [owner(created)]}})


def reconcile(engine, api, row, cron, profile):
    """Reconcile a ledger-active attempt with Kubernetes and return retry eligibility.

    A missing reserved Job is dispatched with its existing attempt ID, not a new
    reservation. Missing running or terminal Jobs without recorded success are
    marked failed and return True; active/resubmitted Jobs return False.
    This function does not infer import success from Kubernetes completion alone.
    """
    job = api.get('jobs', row['job_name'])
    if job is None:
        if row['status'] == 'reserved':
            dispatch(api, cron, profile, row)
            return False

        record(engine, row['id'], row['attempt'], 'failed', 'Worker Job disappeared before verified completion')
        return True

    terminal = job_terminal(job)
    if terminal:
        record(engine, row['id'], row['attempt'], 'failed',
               'Worker terminated without recording verified success: ' + terminal)
        return True

    return False


def discover(engine, config, api):
    """Reconcile one site's history and dispatch eligible immediate datasets.

    Enforce enablement gates and serialize overlapping manual/scheduled discovery.
    Successful modes are skipped; failed/deferred modes can reserve a new attempt.
    Recover active attempts even if their source folder vanished. Per-dataset
    validation/submission failures are aggregated after other candidates proceed;
    infrastructure failures outside that loop propagate immediately.
    """
    site = config['site']
    if not config['bidsImport']['enabled'] or not site.get('enabled') or not site['bidsImport'].get('enabled', False):
        raise ValueError('Site import is disabled')

    initialize(engine)
    source = ROOT / 'assembly_bids' / site['name']
    failures = []
    with database_lock(engine, 'discovery-' + site['name']) as acquired:
        if not acquired:
            print(encoded({'discovery': 'already-active', 'site': site['name']}))
            return

        cron = api.get('cronjobs', config['cronJob'])
        if cron is None:
            raise ValueError('Discovery CronJob no longer exists')

        profile_name = next(volume['configMap']['name'] for volume in
                            cron['spec']['jobTemplate']['spec']['template']['spec']['volumes']
                            if volume['name'] == 'profile')

        profile = api.get('configmaps', profile_name)
        if profile is None:
            raise ValueError('Discovery profile no longer exists')

        with engine.connect() as connection:
            active = connection.execute(select(DATASETS).where(
                DATASETS.c.site == site['name'], DATASETS.c.status.in_(('reserved', 'running')),
            )).mappings().all()

        for row in active:
            reconcile(engine, api, row, cron, profile)

        for folder in discover_datasets(source, site['bidsImport'].get('datasets', {})):
            try:
                if folder.is_symlink():
                    raise ValueError('Symlinked dataset directory is not permitted')

                name = read_name(folder)
                settings = resolve_settings(config, folder.name, name)
                helper('bids-view').inspect_source(folder)
                identity = enroll(engine, site['name'], folder.name, settings, name)
                row = get_dataset(engine, identity)
                if row['status'] in ('reserved', 'running'):
                    if not reconcile(engine, api, row, cron, profile):
                        continue

                row = reserve(engine, identity, settings)
                if row is not None:
                    dispatch(api, cron, profile, row)
                    print(encoded({'dataset': identity, 'attempt': row['attempt'], 'job': row['job_name']}))

            except (ValueError, OSError, ApiError) as error:
                failures.append(folder.name + ': ' + str(error))

        if failures:
            raise ValueError('Discovery failures: ' + '; '.join(failures))


def preflight(engine, source):
    """Validate source metadata, read-only mounting, and configured data/log access.

    This checks operational prerequisites without registering metadata or scans.
    ROOT must match LORIS dataDirBasepath, with source below assembly_bids.
    """
    helper('bids-view').inspect_source(source)
    if not os.statvfs(source).f_flag & os.ST_RDONLY:
        raise ValueError('BIDS source must be mounted read-only')

    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT c.Value FROM Config c JOIN ConfigSettings s ON c.ConfigID = s.ID "
            "WHERE s.Name = 'dataDirBasepath'"
        )).scalars().all()

    if len(rows) != 1 or not rows[0]:
        raise ValueError('Configure one dataDirBasepath value before import')

    base = Path(rows[0]).resolve()
    if base != ROOT.resolve() or not source.resolve().is_relative_to(base / 'assembly_bids'):
        raise ValueError('Source must be below configured /data/loris/data/assembly_bids')

    if not os.access(base, os.R_OK | os.W_OK | os.X_OK):
        raise ValueError('LORIS data directory is not accessible')

    if not (base / 'logs').is_dir():
        raise ValueError('Create the LORIS data logs directory before import')


def setup_dataset(engine, source, site, settings):
    """Configure registration metadata, writable output folders, and viewer modules.

    Database setup commits before directory creation and HTTP module activation;
    those operations are not one transaction. The worker's global import lock
    must cover this call because configuration and registration state are shared.
    """
    from sqlalchemy.orm import Session

    setup = helper('bids-setup')
    subjects = [entry for entry in source.glob('sub-*') if entry.is_dir()]
    visits = {entry.name[4:] for subject in subjects for entry in subject.glob('ses-*') if entry.is_dir()}
    with Session(engine) as session, session.begin():
        setup.configure(session, settings['project'], settings['projectAlias'], site, settings['siteAlias'],
                        settings['cohort'], settings.get('recruitmentTarget'), visits,
                        settings['defaultVisit'], os.environ.get('LORIS_ADMIN_USER', 'admin'))

    for directory in ('logs', 'pic'):
        (ROOT / directory).mkdir(exist_ok=True)

    setup.activate_modules('http://loris', os.environ.get('LORIS_ADMIN_USER', 'admin'),
                           os.environ['LORIS_FRONTEND_ADMIN_PASSWORD'])


def verify_ownership(engine, source, site, settings):
    """Reject existing candidates/sessions registered under incompatible identities.

    Subject PSCIDs are global, not automatically namespaced by source site.
    Missing records are allowed for the upstream create flags to handle; existing
    site/project/cohort mismatches require explicit migration instead of reuse.
    """
    from lib.db.models.candidate import DbCandidate
    from lib.db.models.cohort import DbCohort
    from lib.db.models.project import DbProject
    from lib.db.models.session import DbSession
    from lib.db.models.site import DbSite

    with engine.connect() as connection:
        site_id = connection.scalar(select(DbSite.id).where(DbSite.name == site))
        project_id = connection.scalar(select(DbProject.id).where(DbProject.name == settings['project']))
        cohort_id = connection.scalar(select(DbCohort.id).where(DbCohort.name == settings['cohort']))

        for subject in source.glob('sub-*'):
            if not subject.is_dir():
                continue

            candidate = connection.execute(select(
                DbCandidate.id.label('id'),
                DbCandidate.registration_site_id.label('registration_site_id'),
                DbCandidate.registration_project_id.label('registration_project_id'),
            ).where(DbCandidate.psc_id == subject.name[4:])).one_or_none()
            if candidate is None:
                continue

            if candidate.registration_site_id != site_id or candidate.registration_project_id != project_id:
                raise ValueError('Existing candidate belongs to a different site/project; explicit migration required')

            labels = [entry.name[4:] for entry in subject.glob('ses-*') if entry.is_dir()]
            for label in labels or [settings['defaultVisit']]:
                session = connection.execute(select(
                    DbSession.site_id, DbSession.project_id, DbSession.cohort_id,
                ).where(DbSession.candidate_id == candidate.id, DbSession.visit_label == label)).one_or_none()
                if session is not None and tuple(session) != (site_id, project_id, cohort_id):
                    raise ValueError('Existing session belongs to a different site/project/cohort; migration required')


def import_dataset(view, settings):
    """Run the pinned LORIS-MRI importer and return verified acquisition counts.

    Temporary adapters repair previews on MRI path reuse and resume EEG artifacts.
    A summary with errors, no processed acquisitions, or no unique summary raises
    ValueError even when upstream would exit successfully. Upstream commits
    incrementally: failure does not roll back previously registered acquisitions.

    This modifies process-global callbacks and sys.argv until exit, so callers
    must not run imports concurrently in this Python process. Hooks are restored
    in finally, and the adapter rejects unverified upstream versions.
    """
    from importlib.metadata import version
    if version('loris-bids-importer') != '29.0.0':
        raise ValueError('Verified retry adapter requires LORIS-MRI 29.0.0')

    from loris_bids_importer import main as importer
    from loris_bids_importer.mri import main as mri
    from loris_bids_importer.scripts import import_bids_dataset as cli
    from loris_bids_importer.copy_files import get_loris_bids_file_path
    from lib.db.queries.file import try_get_file_with_path
    from lib.imaging_lib.nifti_pic import create_nifti_preview_picture

    original_summary = importer.print_bids_import_summary
    original_mri = mri.import_bids_mri_acquisition
    summary = []

    def checked_summary(env, result):
        """Capture upstream counts and reject incomplete or error-bearing outcomes."""
        original_summary(env, result)
        outcome = {
            'processed': result.processed_acquisitions_count,
            'imported': result.imported_acquisitions_count,
            'ignored': result.ignored_acquisitions_count,
            'errors': result.failed_acquisitions_count
        }
        
        summary.append(outcome)
        if outcome['errors'] or not outcome['processed']:
            raise ValueError('Import acquisition verification failed: ' + encoded(outcome))

    def checked_mri(env, import_env, session, acquisition, bids_info):
        """Repair previews before upstream skips an already registered MRI path."""
        path = get_loris_bids_file_path(import_env, session, bids_info.data_type, acquisition.nifti_path)

        existing = try_get_file_with_path(env.db, path)
        if existing is not None:
            # Upstream commits registration before preview generation, then skips the path on retry.
            create_nifti_preview_picture(env, existing)

        return original_mri(env, import_env, session, acquisition, bids_info)

    original_argv = sys.argv
    importer.print_bids_import_summary = checked_summary
    mri.import_bids_mri_acquisition = checked_mri
    sys.argv = [
        'import-bids-dataset',
        '--profile', 'config.py',
        '--directory', str(view),
        '--no-copy',
        '--type', settings['datasetType']
    ]

    for key, argument in (('createCandidates', '--create-candidate'), ('createSessions', '--create-session')):
        if settings[key]:
            sys.argv.append(argument)

    try:
        with helper('bids_ephys').resumable_ephys(view):
            cli.main()

        if len(summary) != 1:
            raise ValueError('Importer did not provide exactly one verified acquisition summary')

        return summary[0]
    finally:
        sys.argv = original_argv
        importer.print_bids_import_summary = original_summary
        mri.import_bids_mri_acquisition = original_mri


def worker(engine, config, identity, attempt):
    """Execute one reserved attempt and persist its verified outcome.

    A global nonblocking import lock serializes shared LORIS writes; contention
    records deferred work for later discovery. Revalidate reservation, source
    identity/fingerprint, and read-only containment before setup or import.
    Record success only after the requested mode finishes. Exceptions, including
    process-level exceptions caught here, are recorded as failed and re-raised;
    abrupt pod loss is handled later by discovery reconciliation.
    """
    row = get_dataset(engine, identity)
    if row is None or row['attempt'] != attempt or row['status'] != 'reserved':
        raise ValueError('Attempt is not reserved for this worker')

    site = config['site']
    if row['site'] != site['name']:
        raise ValueError('Worker site does not match its reserved dataset')

    source = ROOT / 'assembly_bids' / row['site'] / row['folder']
    settings = json.loads(row['settings'])
    with database_lock(engine, 'import') as acquired:
        if not acquired:
            record(engine, identity, attempt, 'deferred', 'Another import is active; retry next discovery')
            return

        # Refuse an old pod starting after a replacement attempt was admitted.
        current = get_dataset(engine, identity)
        if current['attempt'] != attempt or current['status'] != 'reserved':
            raise ValueError('Import reservation changed')

        record(engine, identity, attempt, 'running')
        try:
            if read_name(source) != row['metadata_name']:
                raise ValueError('Dataset Name changed after discovery')

            helper('bids-view').inspect_source(source)
            if source.is_symlink() or not source.resolve().is_relative_to(ROOT / 'assembly_bids' / row['site']):
                raise ValueError('Dataset source escapes its site mount')

            if not os.statvfs(source).f_flag & os.ST_RDONLY:
                raise ValueError('BIDS source must be mounted read-only')

            if source_fingerprint(source) != row['fingerprint']:
                raise ValueError('Published dataset changed after discovery')

            if settings['mode'] != 'preflight':
                setup_dataset(engine, source, row['site'], settings)

            preflight(engine, source)
            outcome = {'mode': settings['mode']}
            if settings['mode'] == 'import':
                verify_ownership(engine, source, row['site'], settings)
                view = Path(row['view_path'])
                if not view.is_relative_to(ROOT / 'assembly_bids'):
                    raise ValueError('Import view is outside the assembly directory')

                helper('bids-view').prepare_view(source, view, settings['project'], row['site'],
                                                settings['cohort'], reuse=True)
                outcome.update(import_dataset(view, settings))

            record(engine, identity, attempt, 'succeeded')
            print(encoded({'dataset': identity, 'attempt': attempt, 'result': outcome}))
        except BaseException as error:
            record(engine, identity, attempt, 'failed', type(error).__name__ + ': ' + str(error))
            raise


def main():
    """Dispatch initialize/discover/worker CLI actions using the generated profile.

    Workers obtain their reserved dataset ID and attempt from BIDS_DATASET_ID and
    BIDS_ATTEMPT. Initialization should use the privileged database profile;
    discovery and workers use the application profile.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('initialize', 'discover', 'worker'))
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())

    from lib.config_file import load_config
    from lib.db.connect import get_database_engine
    engine = get_database_engine(load_config('config.py').mysql)

    if args.action == 'initialize':
        initialize(engine)
    elif args.action == 'discover':
        discover(engine, config, Kubernetes(config['namespace']))
    else:
        worker(engine, config, os.environ['BIDS_DATASET_ID'], int(os.environ['BIDS_ATTEMPT']))


if __name__ == '__main__':
    main()
