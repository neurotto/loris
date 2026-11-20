import hashlib
import hmac
import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))

import webhook  # noqa: E402
from test_registry import data, site  # noqa: E402

SECRET = b'0123456789abcdef0123456789abcdef'
REPOSITORY = 'lfx-alpha/Study_A'
CRONJOB = {
    'metadata': {'name': 'lfx-sync', 'uid': 'cron-uid'},
    'spec': {'jobTemplate': {
        'metadata': {'labels': {'app.kubernetes.io/component': 'sync'}},
        'spec': {'ttlSecondsAfterFinished': 86400, 'template': {'spec': {'containers': [
            {'name': 'sync', 'env': [{'name': 'LFX_REGISTRY', 'value': '/r'},
                                     {'name': 'LFX_SYNC_REPOSITORY', 'value': ''}]},
            {'name': 'sidecar'}]}}}}},
}


class FakeKubernetes(BaseHTTPRequestHandler):
    jobs: list = []
    requests: list = []

    def reply(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.requests.append(('GET', self.path, self.headers.get('Authorization')))
        if self.path == '/apis/batch/v1/namespaces/ns/cronjobs/lfx-sync':
            return self.reply(200, CRONJOB)
        self.reply(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(('POST', self.path, self.headers.get('Authorization')))
        self.jobs.append(body)
        self.reply(201, body)

    def log_message(self, *args):
        pass


def serve(test, handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f'http://127.0.0.1:{server.server_address[1]}'


def push(repository=REPOSITORY, ref='refs/heads/main', branch='main'):
    return json.dumps({'ref': ref, 'repository': {'full_name': repository, 'default_branch': branch}}).encode()


class WebhookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / 'registry.json').write_text(json.dumps(data(site())))
        (self.tmp / 'secret').write_bytes(SECRET + b'\n')
        (self.tmp / 'token').write_text('sa-token\n')
        FakeKubernetes.jobs, FakeKubernetes.requests = [], []
        self.logs = []
        original = webhook.log
        webhook.log = lambda **fields: self.logs.append(fields)
        self.addCleanup(setattr, webhook, 'log', original)
        k8s_url = serve(self, FakeKubernetes)
        webhook.Handler.receiver = webhook.Receiver(
            webhook.Kubernetes(k8s_url, 'ns', self.tmp / 'token', None), 'lfx', 'lfx-sync',
            str(self.tmp / 'registry.json'), str(self.tmp / 'secret'))
        self.url = serve(self, webhook.Handler)

    def post(self, body, event='push', signature=None, prefix='Forgejo', path='/forgejo'):
        signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest() if signature is None else signature
        request = urllib.request.Request(self.url + path, data=body, method='POST', headers={
            'Content-Type': 'application/json', f'X-{prefix}-Event': event, f'X-{prefix}-Signature': signature})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_valid_push_creates_sync_job(self):
        code, body = self.post(push())
        self.assertEqual(code, 202)
        name = json.loads(body)['job']
        self.assertRegex(name, r'^lfx-push-[0-9a-f]{8}$')
        self.assertEqual(len(FakeKubernetes.jobs), 1)
        job = FakeKubernetes.jobs[0]
        self.assertEqual(FakeKubernetes.requests[-1][:2], ('POST', '/apis/batch/v1/namespaces/ns/jobs'))
        self.assertEqual({r[2] for r in FakeKubernetes.requests}, {'Bearer sa-token'})
        self.assertEqual(job['metadata']['name'], name)
        self.assertEqual(job['metadata']['labels'], {
            'app.kubernetes.io/component': 'sync',
            'loris-forgejo-export/repository': hashlib.sha256(REPOSITORY.encode()).hexdigest()[:32]})
        annotations = job['metadata']['annotations']
        self.assertEqual(annotations['loris-forgejo-export/repository'], REPOSITORY)
        self.assertEqual(annotations['loris-forgejo-export/triggered-by'], 'webhook')
        self.assertEqual(job['metadata']['ownerReferences'][0]['uid'], 'cron-uid')
        containers = job['spec']['template']['spec']['containers']
        self.assertEqual(containers[0]['env'], [{'name': 'LFX_REGISTRY', 'value': '/r'},
                                                {'name': 'LFX_SYNC_REPOSITORY', 'value': REPOSITORY}])
        self.assertNotIn('env', containers[1])
        self.assertEqual(job['spec']['ttlSecondsAfterFinished'], 86400)
        self.assertEqual(self.logs[-1], {'event': 'accepted', 'delivery': '', 'repository': REPOSITORY,
                                         'ref': 'refs/heads/main', 'job': name})
        # The CronJob template itself is not modified.
        self.assertEqual(CRONJOB['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['env'][1],
                         {'name': 'LFX_SYNC_REPOSITORY', 'value': ''})

    def test_gitea_headers_are_accepted(self):
        self.assertEqual(self.post(push(), prefix='Gitea')[0], 202)

    def test_bad_signature_is_rejected(self):
        self.assertEqual(self.post(push(), signature='0' * 64)[0], 401)
        self.assertEqual(self.post(push(), signature='')[0], 401)
        self.assertEqual(FakeKubernetes.jobs, [])
        self.assertEqual([entry['reason'] for entry in self.logs], ['bad signature'] * 2)
        self.assertNotIn(SECRET.decode(), json.dumps(self.logs))
        self.assertNotIn('full_name', json.dumps(self.logs))

    def test_ignored_pushes(self):
        for body, event in [(push('lfx-alpha/Unknown'), 'push'),
                            (push('lfx-alpha/.worker'), 'push'),
                            (push('other/Study_A'), 'push'),
                            (push(ref='refs/heads/git-annex'), 'push'),
                            (push(ref='refs/heads/dev'), 'push'),
                            (push(ref='refs/tags/main'), 'push'),
                            (push(), 'create')]:
            with self.subTest(body=body, event=event):
                self.assertEqual(self.post(body, event=event)[0], 204)
        self.assertEqual(FakeKubernetes.jobs, [])

    def test_oversized_and_malformed_bodies(self):
        # Declare an oversized length without streaming it: the receiver answers
        # from the header, and sending the full body would race its early close.
        parts = urllib.parse.urlsplit(self.url)
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        self.addCleanup(connection.close)
        connection.putrequest('POST', '/forgejo')
        connection.putheader('Content-Length', str(webhook.MAX_BODY + 1))
        connection.putheader('X-Forgejo-Event', 'push')
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 413)
        self.assertEqual(self.post(b'{"ref": 1}')[0], 400)
        self.assertEqual(self.post(push(), path='/other')[0], 404)
        self.assertEqual(FakeKubernetes.jobs, [])

    def test_kubernetes_failure(self):
        webhook.Handler.receiver.cronjob = 'missing'
        self.assertEqual(self.post(push())[0], 502)

    def test_missing_secret(self):
        (self.tmp / 'secret').unlink()
        self.assertEqual(self.post(push())[0], 503)
        self.assertEqual(FakeKubernetes.jobs, [])

    def test_healthz(self):
        with urllib.request.urlopen(self.url + '/healthz', timeout=5) as response:
            self.assertEqual(response.status, 200)


if __name__ == '__main__':
    unittest.main()
