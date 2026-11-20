#!/usr/bin/env python3
"""loris-forgejo-export push receiver: start a sync Job for a signed Forgejo push to a managed dataset.

``POST /forgejo`` accepts Forgejo (or Gitea) push webhooks of at most 1 MiB, signed with the hex HMAC-SHA256 of the
raw body in ``X-Forgejo-Signature`` (fallback ``X-Gitea-Signature``). A push to the default branch of a managed
``<org>/<dataset>`` repository creates Job ``<LFX_FULLNAME>-push-<8 hex>`` from CronJob ``LFX_SYNC_CRONJOB``'s
``jobTemplate``, with ``LFX_SYNC_REPOSITORY`` set in its first container, and answers 202 ``{"job": name}``. Other
events and pushes (unmanaged repositories, ``.worker``, other branches, ``git-annex``) are ignored with 204. A wrong
signature returns 401. ``GET /healthz`` returns 200.

See CONTRACT.md ("Webhook").
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry  # noqa: E402

MAX_BODY = 1 << 20
SECRET_FILE = '/var/run/secrets/loris-forgejo-export/webhook/secret'
SERVICE_ACCOUNT = Path('/var/run/secrets/kubernetes.io/serviceaccount')
REPOSITORY_LABEL = 'loris-forgejo-export/repository'


def log(**fields):
    """Print one JSON log line."""
    print(json.dumps(fields, sort_keys=True), flush=True)


class KubernetesError(RuntimeError):
    pass


class Kubernetes:
    """Minimal batch/v1 client using the pod's service-account token and CA."""

    def __init__(self, base: str, namespace: str, token_file: str | Path, cafile: str | Path | None):
        self.base = base.rstrip('/')
        self.namespace = namespace
        self.token_file = Path(token_file)
        self.context = ssl.create_default_context(cafile=str(cafile)) if cafile else None

    @classmethod
    def in_cluster(cls, namespace: str) -> 'Kubernetes':
        host = os.environ['KUBERNETES_SERVICE_HOST']
        if ':' in host:
            host = f'[{host}]'
        port = os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS') or os.environ.get('KUBERNETES_SERVICE_PORT', '443')
        return cls(f'https://{host}:{port}', namespace, SERVICE_ACCOUNT / 'token', SERVICE_ACCOUNT / 'ca.crt')

    def request(self, method: str, path: str, body=None) -> dict:
        url = f'{self.base}/apis/batch/v1/namespaces/{urllib.parse.quote(self.namespace)}/{path}'
        request = urllib.request.Request(
            url, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token_file.read_text().strip(),
                     'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise KubernetesError(f'Kubernetes {method} {path}: HTTP {error.code}') from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise KubernetesError(f'Kubernetes {method} {path}: {error}') from error


def push_job(cronjob: dict, name: str, repository: str) -> dict:
    """Build a Job from a CronJob's jobTemplate that syncs one repository."""
    template = cronjob['spec']['jobTemplate']
    metadata = template.get('metadata') or {}
    spec = copy.deepcopy(template['spec'])
    container = spec['template']['spec']['containers'][0]
    env = [entry for entry in container.get('env') or [] if entry.get('name') != 'LFX_SYNC_REPOSITORY']
    container['env'] = env + [{'name': 'LFX_SYNC_REPOSITORY', 'value': repository}]
    labels = dict(metadata.get('labels') or {})
    labels[REPOSITORY_LABEL] = hashlib.sha256(repository.encode()).hexdigest()[:32]
    annotations = dict(metadata.get('annotations') or {})
    annotations.update({'cronjob.kubernetes.io/instantiate': 'manual',
                        REPOSITORY_LABEL: repository,
                        'loris-forgejo-export/triggered-by': 'webhook'})
    job_metadata = {'name': name, 'labels': labels, 'annotations': annotations}
    owner = cronjob.get('metadata') or {}
    if owner.get('uid'):
        job_metadata['ownerReferences'] = [{'apiVersion': 'batch/v1', 'kind': 'CronJob', 'name': owner['name'],
                                            'uid': owner['uid'], 'controller': True}]
    return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': job_metadata, 'spec': spec}


class Receiver:
    def __init__(self, kubernetes: Kubernetes, fullname: str, cronjob: str, registry_path: str, secret_file: str):
        self.kubernetes = kubernetes
        self.fullname = fullname
        self.cronjob = cronjob
        self.registry_path = registry_path
        self.secret_file = secret_file
        self._registry = (None, None)

    def registry(self) -> registry.Registry:
        mtime = os.stat(self.registry_path).st_mtime
        if self._registry[0] != mtime:
            self._registry = (mtime, registry.load(self.registry_path))
        return self._registry[1]

    def secret(self) -> bytes:
        return Path(self.secret_file).read_bytes().strip()

    def start(self, repository: str) -> str:
        cronjob = self.kubernetes.request('GET', f'cronjobs/{urllib.parse.quote(self.cronjob)}')
        name = f'{self.fullname}-push-{secrets.token_hex(4)}'
        self.kubernetes.request('POST', 'jobs', push_job(cronjob, name, repository))
        return name


class Handler(BaseHTTPRequestHandler):
    receiver: Receiver = None
    protocol_version = 'HTTP/1.1'

    def send(self, code: int, body: dict | None = None):
        payload = json.dumps(body).encode() if body is not None else b''
        self.send_response(code)
        if body is not None:
            self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def header(self, name: str) -> str:
        return (self.headers.get(f'X-Forgejo-{name}') or self.headers.get(f'X-Gitea-{name}') or '').strip()

    def do_GET(self):
        if self.path == '/healthz':
            return self.send(200, {'ok': True})
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        self.close_connection = True
        if self.path != '/forgejo':
            return self.send(404, {'error': 'not found'})
        delivery = self.header('Delivery')[:64]
        try:
            length = int(self.headers.get('Content-Length') or -1)
        except ValueError:
            length = -1
        if length > MAX_BODY:
            log(event='rejected', delivery=delivery, reason='body too large', size=length)
            return self.send(413, {'error': f'body exceeds {MAX_BODY} bytes'})
        if length < 0:
            return self.send(411, {'error': 'Content-Length required'})
        body = self.rfile.read(length)
        try:
            secret = self.receiver.secret()
        except OSError:
            secret = b''
        if not secret:
            log(event='failed', delivery=delivery, error='webhook secret is not available')
            return self.send(503, {'error': 'webhook secret is not available'})
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        signature = self.header('Signature').lower()
        if signature.startswith('sha256='):
            signature = signature[len('sha256='):]
        if not hmac.compare_digest(expected.encode(), signature.encode('ascii', 'replace')):
            log(event='rejected', delivery=delivery, reason='bad signature')
            return self.send(401, {'error': 'bad signature'})
        event = self.header('Event')
        if event != 'push':
            log(event='ignored', delivery=delivery, reason='not a push event', hook_event=event[:64])
            return self.send(204)
        try:
            payload = json.loads(body)
            repository = payload['repository']['full_name']
            branch = payload['repository']['default_branch']
            ref = payload['ref']
            if not all(isinstance(value, str) for value in (repository, branch, ref)):
                raise TypeError('expected strings')
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            log(event='rejected', delivery=delivery, reason='malformed push payload')
            return self.send(400, {'error': 'malformed push payload'})
        try:
            managed = self.receiver.registry().by_repository(repository)
        except (OSError, registry.RegistryError) as error:
            log(event='failed', delivery=delivery, error=f'registry: {error}')
            return self.send(503, {'error': 'registry unavailable'})
        if managed is None:
            log(event='ignored', delivery=delivery, repository=repository[:200], reason='not a managed dataset')
            return self.send(204)
        if ref != f'refs/heads/{branch}' or branch == 'git-annex':
            log(event='ignored', delivery=delivery, repository=repository, ref=ref[:200],
                reason='not the default branch')
            return self.send(204)
        try:
            name = self.receiver.start(repository)
        except KubernetesError as error:
            log(event='failed', delivery=delivery, repository=repository, error=str(error))
            return self.send(502, {'error': 'could not create the sync Job'})
        log(event='accepted', delivery=delivery, repository=repository, ref=ref, job=name)
        self.send(202, {'job': name})

    def log_message(self, fmt, *args):
        pass


def namespace() -> str:
    value = os.environ.get('LFX_NAMESPACE')
    if value:
        return value
    return (SERVICE_ACCOUNT / 'namespace').read_text().strip()


def main() -> int:
    fullname = os.environ['LFX_FULLNAME']
    Handler.receiver = Receiver(
        Kubernetes.in_cluster(namespace()), fullname,
        os.environ.get('LFX_SYNC_CRONJOB') or f'{fullname}-sync',
        os.environ.get('LFX_REGISTRY', '/etc/loris-forgejo-export/registry.json'),
        os.environ.get('LFX_WEBHOOK_SECRET_FILE') or SECRET_FILE)
    Handler.receiver.registry()
    port = int(os.environ.get('LFX_PORT', '8080'))
    log(event='listening', port=port, cronjob=Handler.receiver.cronjob)
    server = ThreadingHTTPServer(('', port), Handler)
    server.daemon_threads = True
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
