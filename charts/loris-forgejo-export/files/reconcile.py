#!/usr/bin/env python3
"""Reconcile Forgejo and the enrollment Secrets with the loris-forgejo-export registry.

See CONTRACT.md, "Forgejo objects (reconciler)". In short, for the whole release:

* the restricted export bot ``settings.botUser`` and its token ``loris-forgejo-export``, stored in Secret
  ``<fullname>-bot``;

and for every managed site, in order: the organisation, the publication team, the export team (all repositories,
with the export bot), the dataset repositories, the ``settings.workerRepository`` repository and its template files,
the restricted publication bot and its token stored as an org Actions secret, the org Actions variables, the org push
webhook and, when enrollment is enabled, the Secret ``<fullname>-enrollment-<site>`` holding the site's runner
registration and publication token.

Nothing is deleted except stale tokens and runners this script owns and duplicate hooks pointing at the webhook URL;
anything else unexpected is reported as ``drift:``. Every step is idempotent: a second run against an up-to-date
Forgejo performs no writes (the hook HMAC secret, which Forgejo never returns, is refreshed on every run and counted
separately). ``LFX_DRY_RUN=1`` only logs the writes. A Lease serializes concurrent runs. Standard library only.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402 - shipped next to this script

BOT_TOKEN = 'loris-forgejo-export'
WORKER_TOKEN = 'loris-forgejo-worker'
PUBLICATION_TOKEN = 'loris-forgejo-publication'
TOKEN_SCOPES = ['write:repository']
HOOK_EVENTS = ['push']
PUBLICATION_UNITS = ['repo.code']
EXPORT_UNITS = ['repo.actions', 'repo.code']
RUNNER_KEYS = ('runnerUUID', 'runnerToken')
PUBLICATION_KEYS = ('publicationUser', 'publicationToken')
PLACEHOLDERS = {
    '@RUNNER_LABEL@': 'runnerLabel',
    '@REQUEST_COMMAND@': 'requestCommand',
    '@VARIABLE_PREFIX@': 'variablePrefix',
    '@WORKER_REPOSITORY@': 'workerRepository',
}
PAGE = 50
SA_DIR = Path('/var/run/secrets/kubernetes.io/serviceaccount')
LEASE_SECONDS = 300
LEASE_RENEW_SECONDS = 60
CHART = 'loris-forgejo-export'


class ForgejoError(RuntimeError):
    """An unexpected Forgejo API answer; the message names the request and status, never credentials."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class KubeError(RuntimeError):
    """An unexpected Kubernetes API answer."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class LeaseLost(RuntimeError):
    """The reconcile Lease could not be renewed; the run must stop writing."""


def log(message: str) -> None:
    print(message, flush=True)


def q(value: str) -> str:
    """Quote one URL path segment."""
    return urllib.parse.quote(value, safe='')


def no_guard() -> None:
    return None


class Forgejo:
    """Minimal Forgejo API client using admin basic auth.

    Reads always run. Writes go through :meth:`write`, which only logs them in dry-run mode and returns ``None``.
    """

    def __init__(self, url: str, user: str, password: str, dry_run: bool = False):
        self.base = url.rstrip('/') + '/api/v1'
        self.auth = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()
        self.dry_run = dry_run
        self.writes = 0
        self.refreshes = 0
        self.guard = no_guard

    def request(self, method: str, path: str, body: object = None, sudo: str | None = None,
                missing_ok: bool = False):
        """Send one API request; return the decoded JSON body (``None`` when empty, or on 404 if missing_ok)."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header('Authorization', self.auth)
        request.add_header('Accept', 'application/json')
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        if sudo:
            request.add_header('Sudo', sudo)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            detail = error.read()[:300].decode(errors='replace')
            raise ForgejoError(f'{method} {path}: HTTP {error.code} {detail}', error.code) from None
        return json.loads(payload) if payload else None

    def get(self, path: str, sudo: str | None = None):
        """GET a resource, or ``None`` if it does not exist."""
        return self.request('GET', path, sudo=sudo, missing_ok=True)

    def pages(self, path: str, sudo: str | None = None, key: str = 'data') -> list:
        """GET every page of a list endpoint (some endpoints wrap items in ``key``)."""
        items: list = []
        separator = '&' if '?' in path else '?'
        for page in range(1, 1000):
            # In dry run, objects planned for creation do not exist yet: list them as empty.
            chunk = self.request('GET', f'{path}{separator}limit={PAGE}&page={page}', sudo=sudo,
                                 missing_ok=self.dry_run)
            chunk = (chunk.get(key) or []) if isinstance(chunk, dict) else (chunk or [])
            items += chunk
            if len(chunk) < PAGE:
                return items
        raise ForgejoError(f'GET {path}: too many pages')

    def write(self, what: str, method: str, path: str, body: object = None, sudo: str | None = None):
        """Perform (or, in dry-run mode, only announce) a write; ``what`` is the log line."""
        self.guard()
        log(('would ' if self.dry_run else '') + what)
        self.writes += 1
        if self.dry_run:
            return None
        return self.request(method, path, body, sudo=sudo)

    def refresh(self, what: str, method: str, path: str, body: object = None):
        """A write repeated on every run because its state cannot be read back (the hook secret)."""
        self.guard()
        log(('would ' if self.dry_run else '') + what)
        self.refreshes += 1
        if not self.dry_run:
            self.request(method, path, body)


class Kube:
    """Minimal in-cluster Kubernetes API client (service account token and CA)."""

    def __init__(self, namespace: str, sa_dir: Path = SA_DIR):
        host = os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')
        port = os.environ.get('KUBERNETES_SERVICE_PORT', '443')
        if ':' in host:
            host = f'[{host}]'
        self.base = f'https://{host}:{port}'
        self.namespace = namespace
        self.token_file = sa_dir / 'token'
        self.ca_file = sa_dir / 'ca.crt'
        self.available = bool(namespace) and self.token_file.is_file()
        self.context = ssl.create_default_context(cafile=str(self.ca_file)) if self.ca_file.is_file() else None

    def request(self, method: str, path: str, body: object = None,
                content_type: str = 'application/json'):
        if not self.available:
            raise KubeError('Kubernetes API unavailable (no service account token or LFX_NAMESPACE)')
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header('Authorization', 'Bearer ' + self.token_file.read_text().strip())
        request.add_header('Accept', 'application/json')
        if data is not None:
            request.add_header('Content-Type', content_type)
        try:
            with urllib.request.urlopen(request, timeout=30, context=self.context) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read()[:300].decode(errors='replace')
            raise KubeError(f'{method} {path}: HTTP {error.code} {detail}', error.code) from None
        return json.loads(payload) if payload else None

    def get(self, path: str):
        try:
            return self.request('GET', path)
        except KubeError as error:
            if error.status == 404:
                return None
            raise

    def secret_path(self, name: str = '') -> str:
        return f'/api/v1/namespaces/{q(self.namespace)}/secrets' + (f'/{q(name)}' if name else '')

    def lease_path(self, name: str = '') -> str:
        return (f'/apis/coordination.k8s.io/v1/namespaces/{q(self.namespace)}/leases'
                + (f'/{q(name)}' if name else ''))


def now_micro() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def parse_time(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


class Lease:
    """Mutual exclusion through a coordination.k8s.io/v1 Lease, renewed by a background thread."""

    def __init__(self, kube: Kube, name: str, holder: str, duration: int = LEASE_SECONDS,
                 renew_every: float = LEASE_RENEW_SECONDS):
        self.kube = kube
        self.name = name
        self.holder = holder
        self.duration = duration
        self.renew_every = renew_every
        self.lost: str | None = None
        self.held = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _spec(self, previous: dict | None) -> dict:
        stamp = now_micro()
        spec = dict(previous or {})
        if spec.get('holderIdentity') != self.holder:
            spec['acquireTime'] = stamp
            spec['leaseTransitions'] = int(spec.get('leaseTransitions') or 0) + (1 if previous else 0)
        spec.update(holderIdentity=self.holder, leaseDurationSeconds=self.duration, renewTime=stamp)
        return spec

    def _try(self) -> tuple[bool, str]:
        """One acquisition attempt; return (acquired, current holder)."""
        lease = self.kube.get(self.kube.lease_path(self.name))
        try:
            if lease is None:
                self.kube.request('POST', self.kube.lease_path(), {
                    'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease',
                    'metadata': {'name': self.name, 'namespace': self.kube.namespace},
                    'spec': self._spec(None)})
                return True, self.holder
            spec = lease.get('spec') or {}
            holder = spec.get('holderIdentity') or ''
            renewed = parse_time(spec.get('renewTime')) or parse_time(spec.get('acquireTime'))
            duration = int(spec.get('leaseDurationSeconds') or 0)
            expired = renewed is None or (
                renewed + datetime.timedelta(seconds=duration) < datetime.datetime.now(datetime.timezone.utc))
            if holder and holder != self.holder and not expired:
                return False, holder
            lease['spec'] = self._spec(spec)
            self.kube.request('PUT', self.kube.lease_path(self.name), lease)
            return True, self.holder
        except KubeError as error:
            if error.status == 409:
                return False, 'a concurrent writer'
            raise

    def acquire(self, wait_seconds: float, poll: float = 5) -> bool:
        deadline = time.monotonic() + wait_seconds
        while True:
            acquired, holder = self._try()
            if acquired:
                self.held = True
                log(f'lease {self.name} acquired by {self.holder}')
                self._thread = threading.Thread(target=self._renew, daemon=True)
                self._thread.start()
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log(f'lease {self.name} is held by {holder}; giving up')
                return False
            log(f'lease {self.name} is held by {holder}; waiting')
            time.sleep(min(poll, remaining))

    def _renew(self) -> None:
        while not self._stop.wait(self.renew_every):
            try:
                lease = self.kube.get(self.kube.lease_path(self.name))
                if lease is None or (lease.get('spec') or {}).get('holderIdentity') != self.holder:
                    raise KubeError('lease was taken over or deleted')
                lease['spec'] = self._spec(lease['spec'])
                self.kube.request('PUT', self.kube.lease_path(self.name), lease)
            except (KubeError, OSError) as error:
                self.lost = str(error)
                log(f'lease {self.name} renewal failed: {error}')
                return

    def check(self) -> None:
        if self.lost is not None:
            raise LeaseLost(f'lease {self.name} lost: {self.lost}')

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if not self.held or self.lost is not None:
            return
        try:
            lease = self.kube.get(self.kube.lease_path(self.name))
            if lease and (lease.get('spec') or {}).get('holderIdentity') == self.holder:
                lease['spec'] = {k: v for k, v in lease['spec'].items() if k not in ('holderIdentity', 'renewTime')}
                self.kube.request('PUT', self.kube.lease_path(self.name), lease)
        except (KubeError, OSError) as error:
            log(f'lease {self.name} release failed (it expires on its own): {error}')
        self.held = False


@dataclass(frozen=True)
class Config:
    fullname: str
    release: str
    public_url: str
    public_ip: str = ''
    webhook_url: str = ''
    webhook_secret: str = ''
    enrollment: bool = False
    dry_run: bool = False


def read_template(directory: str | Path, settings: registry.Settings) -> dict[str, bytes]:
    """Read the worker template (ConfigMap keys use ``__`` for ``/``) and substitute the placeholders."""
    root = Path(directory)
    if not root.is_dir():
        return {}
    files = {}
    for entry in sorted(root.iterdir()):
        if entry.name.startswith('..') or not entry.is_file():
            continue
        content = entry.read_bytes()
        for placeholder, key in PLACEHOLDERS.items():
            content = content.replace(placeholder.encode(), getattr(settings, key).encode())
        files[entry.name.replace('__', '/')] = content
    return files


def plan_hooks(hooks: list[dict], url: str) -> tuple[dict | None, list[int]]:
    """Choose the hook to keep for ``url`` (the oldest) and the duplicate ids to delete."""
    ours = sorted((hook for hook in hooks if (hook.get('config') or {}).get('url') == url), key=lambda h: h['id'])
    return (ours[0] if ours else None), [hook['id'] for hook in ours[1:]]


def hook_matches(hook: dict, url: str) -> bool:
    config = hook.get('config') or {}
    return (hook.get('active') is True and sorted(hook.get('events') or []) == HOOK_EVENTS
            and config.get('content_type') == 'json' and hook.get('type') == 'forgejo'
            and config.get('url') == url)


def team_units(team: dict) -> list[str]:
    units = team.get('units')
    if units is None:
        units = [unit for unit, mode in (team.get('units_map') or {}).items() if mode != 'none']
    return sorted(units)


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def decode_data(secret: dict | None) -> dict[str, str]:
    data = (secret or {}).get('data') or {}
    return {key: base64.b64decode(value or '').decode(errors='replace') for key, value in data.items()}


class Reconciler:
    """Applies the registry to Forgejo and to the enrollment Secrets."""

    def __init__(self, api: Forgejo, kube: Kube, settings: registry.Settings, config: Config,
                 template: dict[str, bytes]):
        self.api = api
        self.kube = kube
        self.settings = settings
        self.config = config
        self.template = template
        self.secret_writes = 0
        self.guard = no_guard
        self._kube_warned = False

    # Kubernetes Secrets -------------------------------------------------------------------------------------------

    def labels(self, component: str, site: str | None = None) -> dict[str, str]:
        labels = {'app.kubernetes.io/name': CHART, 'app.kubernetes.io/instance': self.config.release,
                  'app.kubernetes.io/component': component}
        if site is not None:
            labels['loris-forgejo-export/site'] = site
        return labels

    def read_secret(self, name: str) -> dict | None:
        if self.config.dry_run and not self.kube.available:
            if not self._kube_warned:
                log('Kubernetes API unavailable in dry run: Secrets are assumed missing')
                self._kube_warned = True
            return None
        try:
            return self.kube.get(self.kube.secret_path(name))
        except (KubeError, OSError) as error:
            raise ForgejoError(f'reading Secret {name}: {error}') from None

    def write_secret(self, name: str, data: dict[str, str], labels: dict[str, str], exists: bool) -> None:
        """Create the Secret, or merge ``data`` and ``labels`` into it."""
        self.guard()
        log(('would ' if self.config.dry_run else '') + f'{"update" if exists else "create"} Secret {name} '
            f'keys {", ".join(sorted(data)) or "(labels)"}')
        self.secret_writes += 1
        if self.config.dry_run:
            return
        encoded = {key: b64(value) for key, value in data.items()}
        try:
            if exists:
                self.kube.request('PATCH', self.kube.secret_path(name),
                                  {'metadata': {'labels': labels}, 'data': encoded},
                                  'application/merge-patch+json')
            else:
                self.kube.request('POST', self.kube.secret_path(), {
                    'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                    'metadata': {'name': name, 'namespace': self.kube.namespace, 'labels': labels},
                    'data': encoded})
        except (KubeError, OSError) as error:
            raise ForgejoError(f'writing Secret {name}: {error}') from None

    # Forgejo identities -------------------------------------------------------------------------------------------

    def user(self, name: str, purpose: str) -> bool:
        """Ensure a restricted, private bot user; return whether it exists (False only in dry run)."""
        if self.api.get(f'/users/{q(name)}') is not None:
            return True
        created = self.api.write(f'create bot user {name} ({purpose})', 'POST', '/admin/users', {
            'username': name, 'email': f'{name}@loris-forgejo-export.invalid', 'password': secrets.token_urlsafe(32),
            'must_change_password': False, 'restricted': True, 'visibility': 'private', 'send_notify': False})
        return created is not None

    def tokens(self, user: str, name: str) -> list[dict]:
        return [t for t in self.api.pages(f'/users/{q(user)}/tokens', sudo=user) if t.get('name') == name]

    def delete_token(self, user: str, token: dict, reason: str) -> None:
        self.api.write(f'delete token {user}/{token["name"]} ({reason})', 'DELETE',
                       f'/users/{q(user)}/tokens/{token["id"]}', sudo=user)

    def create_token(self, user: str, name: str) -> dict | None:
        token = self.api.write(f'create token {user}/{name}', 'POST', f'/users/{q(user)}/tokens',
                               {'name': name, 'scopes': TOKEN_SCOPES}, sudo=user)
        if token is not None and not (isinstance(token, dict) and token.get('sha1') and token.get('id')):
            raise ForgejoError(f'token API did not return a token for {user}/{name}')
        return token

    def discard_token(self, user: str, token: dict) -> None:
        """Best-effort removal of a token whose value could not be stored."""
        try:
            self.api.request('DELETE', f'/users/{q(user)}/tokens/{token["id"]}', sudo=user)
            log(f'deleted unstored token {user}/{token.get("name")}')
        except (ForgejoError, OSError) as error:
            log(f'could not delete unstored token {user}/{token.get("name")}: {error}')

    def export_bot(self) -> None:
        """Ensure the export bot and its token, stored in Secret ``<fullname>-bot``."""
        bot = self.settings.botUser
        name = f'{self.config.fullname}-bot'
        if not self.user(bot, 'export'):
            return log(f'would create token {bot}/{BOT_TOKEN} and write Secret {name}')
        secret = self.read_secret(name)
        data = decode_data(secret)
        stored = data.get('username') == bot and bool(data.get('token'))
        tokens = self.tokens(bot, BOT_TOKEN)
        if stored and tokens:
            return
        for old in tokens:
            self.delete_token(bot, old, f'Secret {name} missing or empty')
        token = self.create_token(bot, BOT_TOKEN)
        if token is None:
            return log(f'would write Secret {name}')
        try:
            self.write_secret(name, {'username': bot, 'token': token['sha1']}, self.labels('bot'), secret is not None)
        except BaseException:
            self.discard_token(bot, token)
            raise

    # Per site -----------------------------------------------------------------------------------------------------

    def site(self, site: registry.Site) -> None:
        self.org(site)
        publication = self.team(site.org, self.settings.publicationTeam, PUBLICATION_UNITS, False,
                                'Site dataset writers and the site publication bot')
        export = self.team(site.org, self.settings.exportTeam, EXPORT_UNITS, True,
                           'loris-forgejo-export bot: reads datasets and dispatches worker workflows')
        self.member(export, site.org, self.settings.exportTeam, self.settings.botUser)
        for dataset in site.datasets:
            self.repo(site.org, dataset, actions=False, private=dataset not in site.signed_in_datasets)
            self.team_repo(publication, site.org, dataset)
        self.team_extra_repos(publication, site)
        if self.repo(site.org, self.settings.workerRepository, actions=True, auto_init=True):
            self.worker_files(site.org)
        self.publication_bot(site, publication)
        self.variables(site.org)
        self.hook(site.org)
        if self.config.enrollment:
            self.enrollment(site)

    def org(self, site: registry.Site) -> None:
        """Establish the anonymous-blocking organisation boundary before any repository is opened."""
        visibility = 'limited' if site.signed_in_datasets else 'private'
        full_name = f'LORIS site {site.alias or site.name}'
        current = self.api.get(f'/orgs/{q(site.org)}')
        if current is None:
            self.api.write(f'create organisation {site.org} ({visibility})', 'POST', '/orgs', {
                'username': site.org, 'visibility': visibility, 'full_name': full_name,
                'description': f'Datasets exported to LORIS by site {site.alias or site.name}'})
        else:
            if current.get('visibility') not in ('private', 'limited'):
                raise ForgejoError(f'{site.org}: unexpected organisation visibility '
                                   f'{current.get("visibility")!r}; review before reconciling')
            change: dict = {}
            if current.get('visibility') != visibility:
                if visibility == 'limited':
                    for repo in self.api.pages(f'/orgs/{q(site.org)}/repos'):
                        if repo['name'] not in site.repos and repo.get('private') is not True:
                            raise ForgejoError(f'{site.org}/{repo["name"]}: non-private unmanaged repository '
                                               'blocks the visibility change')
                    for name in site.repos:
                        worker = name == self.settings.workerRepository
                        self.repo(site.org, name, actions=worker, auto_init=worker, private=True)
                change['visibility'] = visibility
            if current.get('full_name') != full_name:
                change['full_name'] = full_name
            if change:
                self.api.write(f'set organisation {site.org} ' + ', '.join(f'{k}={v}' for k, v in change.items()),
                               'PATCH', f'/orgs/{q(site.org)}', change)
        if not self.api.dry_run:
            actual = self.api.get(f'/orgs/{q(site.org)}')
            if not actual or actual.get('visibility') != visibility:
                raise ForgejoError(f'{site.org}: organisation visibility was not established')

    def team(self, org: str, name: str, units: list[str], all_repos: bool, description: str) -> int | None:
        """Return the team id, creating or correcting it (permission write, given units and repository scope)."""
        found = self.api.get(f'/orgs/{q(org)}/teams/search?q={q(name)}')
        body = {'name': name, 'permission': 'write', 'units': units, 'includes_all_repositories': all_repos,
                'can_create_org_repo': False, 'description': description}
        for team in (found or {}).get('data', []):
            if team.get('name') != name:
                continue
            if (team.get('permission') != 'write' or team_units(team) != sorted(units)
                    or team.get('includes_all_repositories') is not all_repos):
                self.api.write(f'fix team {org}/{name} permissions', 'PATCH', f'/teams/{team["id"]}', body)
            return team['id']
        created = self.api.write(f'create team {org}/{name}', 'POST', f'/orgs/{q(org)}/teams', body)
        return created['id'] if created else None

    def member(self, team: int | None, org: str, team_name: str, user: str) -> None:
        if team is None:
            return log(f'would add {user} to team {org}/{team_name}')
        if self.api.get(f'/teams/{team}/members/{q(user)}') is None:
            self.api.write(f'add {user} to team {org}/{team_name}', 'PUT', f'/teams/{team}/members/{q(user)}')

    def repo(self, org: str, name: str, actions: bool, auto_init: bool = False, private: bool = True) -> bool:
        """Ensure repository privacy and Actions; return whether it exists (False only in dry run)."""
        repo = self.api.get(f'/repos/{q(org)}/{q(name)}')
        if repo is None:
            body = {'name': name, 'private': private}
            if auto_init:
                body.update(auto_init=True, default_branch='main')
            repo = self.api.write(f'create repository {org}/{name}', 'POST', f'/orgs/{q(org)}/repos', body)
            if repo is None:
                return False
        if repo.get('has_actions') is not actions or repo.get('private') is not private:
            self.api.write(f'set {org}/{name} private={private}, actions={actions}', 'PATCH',
                           f'/repos/{q(org)}/{q(name)}', {'has_actions': actions, 'private': private})
        if not self.api.dry_run:
            actual = self.api.get(f'/repos/{q(org)}/{q(name)}')
            if not actual or actual.get('private') is not private or actual.get('has_actions') is not actions:
                raise ForgejoError(f'{org}/{name}: repository privacy or Actions settings were not established')
        return True

    def team_repo(self, team: int | None, org: str, repo: str) -> None:
        name = self.settings.publicationTeam
        if team is None:
            return log(f'would add {org}/{repo} to team {name}')
        if self.api.get(f'/teams/{team}/repos/{q(org)}/{q(repo)}') is None:
            self.api.write(f'add {org}/{repo} to team {name}', 'PUT', f'/teams/{team}/repos/{q(org)}/{q(repo)}')

    def team_extra_repos(self, team: int | None, site: registry.Site) -> None:
        """The publication team must never reach the worker repository; other extras are reported."""
        if team is None:
            return
        for repo in self.api.pages(f'/teams/{team}/repos'):
            name = repo.get('name')
            if name == self.settings.workerRepository:
                raise ForgejoError(f'{site.org}/{name} is in team {self.settings.publicationTeam}, which would let '
                                   'dataset writers change worker workflows; remove it from the team and rerun')
            if name not in site.datasets:
                log(f'drift: repository {site.org}/{name} is in team {self.settings.publicationTeam} '
                    'but not in the registry (left untouched)')

    def worker_files(self, org: str) -> None:
        """Make the template files identical on ``main``; extra files are only reported."""
        worker = self.settings.workerRepository
        base = f'/repos/{q(org)}/{q(worker)}/contents'
        for path, content in self.template.items():
            url = f'{base}/{urllib.parse.quote(path)}'
            current = self.api.get(f'{url}?ref=main')
            if current is not None and base64.b64decode(current.get('content') or '') == content:
                continue
            body = {'content': base64.b64encode(content).decode(), 'branch': 'main',
                    'message': f'loris-forgejo-export reconciler: sync {path}'}
            if current is None:
                self.api.write(f'add {org}/{worker}/{path}', 'POST', url, body)
            else:
                body['sha'] = current['sha']
                self.api.write(f'update {org}/{worker}/{path}', 'PUT', url, body)
        for path in self.worker_tree(org):
            if path not in self.template:
                log(f'drift: file {org}/{worker}/{path} is not in the worker template (left untouched)')

    def worker_tree(self, org: str) -> list[str]:
        worker = self.settings.workerRepository
        paths: list[str] = []
        for page in range(1, 1000):
            tree = self.api.get(f'/repos/{q(org)}/{q(worker)}/git/trees/main?recursive=true&per_page=1000'
                                f'&page={page}')
            entries = (tree or {}).get('tree') or []
            paths += [entry['path'] for entry in entries if entry.get('type') == 'blob']
            if not tree or not tree.get('truncated') or not entries:
                return paths
        return paths

    def publication_bot(self, site: registry.Site, team: int | None) -> None:
        """Ensure the publication bot, its membership, and its token stored as the org Actions secret."""
        bot = site.bot
        secret = f'{self.settings.variablePrefix}WORKER_TOKEN'
        if not self.user(bot, f'publication for {site.name}'):
            return log(f'would add {bot} to team {self.settings.publicationTeam}, create token {WORKER_TOKEN}, '
                       f'set secret {secret}')
        self.member(team, site.org, self.settings.publicationTeam, bot)
        tokens = self.tokens(bot, WORKER_TOKEN)
        stored = any(s.get('name') == secret for s in self.api.pages(f'/orgs/{q(site.org)}/actions/secrets'))
        if tokens and stored:
            return
        for old in tokens:
            self.delete_token(bot, old, f'org secret {secret} missing')
        token = self.create_token(bot, WORKER_TOKEN)
        if token is None:
            return log(f'would set secret {site.org}/{secret}')
        try:
            self.api.write(f'set secret {site.org}/{secret}', 'PUT',
                           f'/orgs/{q(site.org)}/actions/secrets/{secret}', {'data': token['sha1']})
        except BaseException:
            self.discard_token(bot, token)
            raise

    def variables(self, org: str) -> None:
        prefix = self.settings.variablePrefix
        wanted = {f'{prefix}FORGE_URL': self.config.public_url}
        if self.config.public_ip:
            wanted[f'{prefix}FORGE_IP'] = self.config.public_ip
        for name, value in wanted.items():
            current = self.api.get(f'/orgs/{q(org)}/actions/variables/{name}')
            if current is None:
                self.api.write(f'create variable {org}/{name}', 'POST', f'/orgs/{q(org)}/actions/variables/{name}',
                               {'value': value})
            elif current.get('data') != value:
                self.api.write(f'update variable {org}/{name}', 'PUT', f'/orgs/{q(org)}/actions/variables/{name}',
                               {'value': value})

    def hook(self, org: str) -> None:
        """Keep exactly one push webhook to the webhook URL; delete duplicates of that URL only."""
        hooks = self.api.pages(f'/orgs/{q(org)}/hooks')
        url = self.config.webhook_url
        if not url:
            urls = ', '.join(sorted(str((h.get('config') or {}).get('url')) for h in hooks)) or 'none'
            return log(f'{org}: webhook management disabled (scheduled sync only); existing hooks: {urls}')
        config = {'url': url, 'content_type': 'json'}
        if self.config.webhook_secret:
            config['secret'] = self.config.webhook_secret
        body = {'config': config, 'events': HOOK_EVENTS, 'active': True}
        keep, duplicates = plan_hooks(hooks, url)
        if keep is None:
            self.api.write(f'create push webhook for {org}', 'POST', f'/orgs/{q(org)}/hooks',
                           {'type': 'forgejo', **body})
        elif not hook_matches(keep, url):
            self.api.write(f'fix push webhook {keep["id"]} of {org}', 'PATCH', f'/orgs/{q(org)}/hooks/{keep["id"]}',
                           body)
        elif self.config.webhook_secret:
            self.api.refresh(f'refresh secret of push webhook {keep["id"]} of {org}', 'PATCH',
                             f'/orgs/{q(org)}/hooks/{keep["id"]}', body)
        for hook_id in duplicates:
            self.api.write(f'delete duplicate webhook {hook_id} of {org}', 'DELETE', f'/orgs/{q(org)}/hooks/{hook_id}')

    def runners(self, org: str) -> list[dict]:
        response = self.api.request('GET', f'/orgs/{q(org)}/actions/runners', missing_ok=self.api.dry_run)
        if response is None and self.api.dry_run:
            return []
        if isinstance(response, dict):
            response = response.get('runners', response.get('data'))
        if not isinstance(response, list):
            raise ForgejoError(f'{org}: runner API did not return a runner list')
        return response

    def enrollment(self, site: registry.Site) -> None:
        """Ensure Secret ``<fullname>-enrollment-<site>``: runner registration, publication token, settings."""
        name = f'{self.config.fullname}-enrollment-{site.name}'
        runner_name = f'{site.name}-worker'
        labels = self.labels('enrollment', site.name)
        secret = self.read_secret(name)
        current = decode_data(secret)
        wanted = {
            'forgeUrl': self.config.public_url, 'forgeIP': self.config.public_ip, 'organization': site.org,
            'workerRepository': self.settings.workerRepository, 'runnerLabel': self.settings.runnerLabel,
            'requestCommand': self.settings.requestCommand, 'runnerName': runner_name,
            'repositories': json.dumps(list(site.datasets)),
        }
        pending = {key: value for key, value in wanted.items() if current.get(key) != value}
        state = {'exists': secret is not None, 'pending': pending}
        stale_labels = secret is not None and any(
            ((secret.get('metadata') or {}).get('labels') or {}).get(k) != v for k, v in labels.items())

        def store(keys: dict[str, str]) -> None:
            self.write_secret(name, {**state['pending'], **keys}, labels, state['exists'])
            state['exists'], state['pending'] = True, {}

        if not all(current.get(key) for key in RUNNER_KEYS):
            for runner in self.runners(site.org):
                if runner.get('name') != runner_name:
                    continue
                try:
                    self.api.write(f'delete runner {site.org}/{runner_name} ({runner["id"]}): '
                                   f'Secret {name} lacks its keys', 'DELETE',
                                   f'/orgs/{q(site.org)}/actions/runners/{runner["id"]}')
                except ForgejoError as error:
                    if error.status in (404, 405, 501):
                        raise ForgejoError(
                            f'{site.org}: runner {runner_name} exists but Secret {name} lacks its keys, and this '
                            'Forgejo cannot delete runners through the API; delete the runner in the organisation '
                            'settings (Actions > Runners) and rerun') from None
                    raise
            runner = self.api.write(f'register runner {site.org}/{runner_name}', 'POST',
                                    f'/orgs/{q(site.org)}/actions/runners', {'name': runner_name})
            if runner is None:
                log(f'would write {", ".join(RUNNER_KEYS)} to Secret {name}')
            else:
                if not isinstance(runner, dict) or not all(runner.get(k) for k in ('id', 'uuid', 'token')):
                    raise ForgejoError(f'{site.org}: runner API did not return id, uuid and token')
                try:
                    store({'runnerUUID': str(runner['uuid']), 'runnerToken': str(runner['token'])})
                except BaseException:
                    try:
                        self.api.request('DELETE', f'/orgs/{q(site.org)}/actions/runners/{runner["id"]}')
                    except (ForgejoError, OSError) as error:
                        log(f'could not delete unstored runner {site.org}/{runner_name}: {error}')
                    raise

        if not (current.get('publicationUser') == site.bot and current.get('publicationToken')):
            planned_bot = self.api.dry_run and self.api.get(f'/users/{q(site.bot)}') is None
            for old in [] if planned_bot else self.tokens(site.bot, PUBLICATION_TOKEN):
                self.delete_token(site.bot, old, f'Secret {name} lacks it')
            token = self.create_token(site.bot, PUBLICATION_TOKEN)
            if token is None:
                log(f'would write {", ".join(PUBLICATION_KEYS)} to Secret {name}')
            else:
                try:
                    store({'publicationUser': site.bot, 'publicationToken': token['sha1']})
                except BaseException:
                    self.discard_token(site.bot, token)
                    raise

        if state['pending'] or stale_labels:
            store({})

    # Drift --------------------------------------------------------------------------------------------------------

    def drift(self, sites: tuple[registry.Site, ...], known_names: list[str]) -> None:
        """Report organisations and repositories that the registry does not manage."""
        managed = {site.org: site for site in sites}
        prefix = self.settings.organizationPrefix
        known = {prefix + name for name in known_names} | set(managed)
        for org in self.api.pages('/admin/orgs'):
            name = org.get('username') or org.get('name')
            if not (name.startswith(prefix) if prefix else name in known):
                continue
            if name not in managed:
                log(f'drift: organisation {name} is not managed by the registry (left untouched)')
                continue
            for repo in self.api.pages(f'/orgs/{q(name)}/repos'):
                if repo['name'] not in managed[name].repos:
                    log(f'drift: repository {name}/{repo["name"]} is not in the registry (left untouched)')


def wait_for(api: Forgejo, seconds: float, poll: float = 5) -> None:
    """Wait until the Forgejo API answers, or raise after ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            version = api.request('GET', '/version')
            return log(f'Forgejo {(version or {}).get("version")} at {api.base}')
        except (OSError, ForgejoError) as error:
            if time.monotonic() > deadline:
                raise ForgejoError(f'Forgejo not ready: {error}') from None
            time.sleep(poll)


def run(reg: registry.Registry, api: Forgejo, kube: Kube, config: Config, template: dict[str, bytes],
        known_names: list[str], holder: str, wait_seconds: float = 300, poll: float = 5) -> int:
    """Reconcile everything; return the exit code (1 if anything failed)."""
    try:
        wait_for(api, wait_seconds, poll)
    except ForgejoError as error:
        log(str(error))
        return 1
    if not template:
        log('warning: the worker template is empty')
    reconciler = Reconciler(api, kube, reg.settings, config, template)
    writes, refreshes = api.writes, api.refreshes
    lease = None
    if not config.dry_run:
        lease = Lease(kube, f'{config.fullname}-reconcile', holder)
        try:
            if not lease.acquire(wait_seconds, poll):
                return 1
        except (KubeError, OSError) as error:
            log(f'cannot acquire lease {lease.name}: {error}')
            return 1
        api.guard = reconciler.guard = lease.check
    failed: list[str] = []
    try:
        try:
            reconciler.export_bot()
        except (ForgejoError, OSError) as error:
            failed.append('export bot')
            log(f'export bot: FAILED: {error}')
        for site in reg.sites:
            log(f'{site.name}: reconciling {site.org} ({len(site.datasets)} datasets)')
            try:
                reconciler.site(site)
            except (ForgejoError, OSError) as error:
                failed.append(site.name)
                log(f'{site.name}: FAILED: {error}')
        try:
            reconciler.drift(reg.sites, known_names)
        except (ForgejoError, OSError) as error:
            failed.append('drift report')
            log(f'drift report: FAILED: {error}')
    except LeaseLost as error:
        log(f'aborted: {error}')
        return 1
    finally:
        if lease is not None:
            lease.release()
    planned = 'planned ' if config.dry_run else ''
    log(f'done: {api.writes - writes} {planned}Forgejo writes, {reconciler.secret_writes} {planned}Secret writes, '
        f'{api.refreshes - refreshes} {planned}webhook secret refreshes, failed: {", ".join(failed) or "none"}')
    return 1 if failed else 0


def release_name(fullname: str) -> str:
    """The Helm release for the instance label; LFX_RELEASE wins, otherwise derived from the fullname helper."""
    explicit = os.environ.get('LFX_RELEASE', '')
    if explicit:
        return explicit
    return fullname[:-len('-' + CHART)] if fullname.endswith('-' + CHART) else fullname


def main() -> int:
    path = os.environ.get('LFX_REGISTRY', '/etc/loris-forgejo-export/registry.json')
    try:
        reg = registry.load(path)
        with open(path, encoding='utf-8') as stream:
            known = [s['name'] for s in json.load(stream).get('sites') or [] if isinstance(s, dict)]
    except (OSError, ValueError) as error:
        log(f'cannot load registry {path}: {error}')
        return 1
    dry_run = os.environ.get('LFX_DRY_RUN', '0') == '1'
    fullname = os.environ['LFX_FULLNAME']
    config = Config(
        fullname=fullname, release=release_name(fullname),
        public_url=os.environ['LFX_FORGEJO_PUBLIC_URL'], public_ip=os.environ.get('LFX_FORGEJO_PUBLIC_IP', ''),
        webhook_url=os.environ.get('LFX_WEBHOOK_URL', ''),
        # The receiver strips the same value, so a trailing newline in an existing Secret still verifies.
        webhook_secret=os.environ.get('LFX_WEBHOOK_SECRET', '').strip(),
        enrollment=os.environ.get('LFX_ENROLLMENT', '0') == '1', dry_run=dry_run)
    api = Forgejo(os.environ['LFX_FORGEJO_URL'], os.environ['LFX_ADMIN_USER'], os.environ['LFX_ADMIN_PASS'],
                  dry_run=dry_run)
    kube = Kube(os.environ.get('LFX_NAMESPACE', ''))
    template = read_template(os.environ.get('LFX_WORKER_TEMPLATE', '/etc/loris-forgejo-export/worker-template'),
                             reg.settings)
    holder = os.environ.get('HOSTNAME') or socket.gethostname()
    return run(reg, api, kube, config, template, known, holder, float(os.environ.get('LFX_WAIT_SECONDS', '300')))


if __name__ == '__main__':
    sys.exit(main())
