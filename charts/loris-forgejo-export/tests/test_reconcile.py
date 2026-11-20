"""Tests for files/reconcile.py against an in-memory Forgejo and Kubernetes API (no network)."""

import base64
import contextlib
import copy
import io
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

FILES = Path(__file__).resolve().parents[1] / 'files'
sys.path.insert(0, str(FILES))

import reconcile  # noqa: E402
import registry  # noqa: E402

WEBHOOK = 'http://lfx-webhook.ns.svc.cluster.local:8080/forgejo'
SETTINGS = {
    'organizationPrefix': 'lfx-', 'botUser': 'lfx-loris-export', 'publicationBotSuffix': '-worker',
    'publicationTeam': 'datasets', 'exportTeam': 'loris-export', 'workerRepository': '.worker',
    'runnerLabel': 'loris-forgejo-worker', 'requestCommand': 'loris-forgejo-request',
    'variablePrefix': 'LORIS_FORGEJO_', 'dataClaim': 'loris-data', 'clonesSubPath': 'forgejo/clones',
    'importCronJobPrefix': 'loris-bids-',
}
TEMPLATE = {'README.md': b'managed\n', '.forgejo/workflows/stage-content.yml': b'runs-on: x\n'}


def site_entry(name, datasets, visibility=None, forgejo=True):
    return {'name': name, 'enabled': True, 'source': 'pvc', 'claimName': 'loris-data',
            'subPath': f'forgejo/clones/{name}', 'siteAlias': name.upper(),
            'forgejo': {'enabled': forgejo, 'datasets': datasets, 'visibility': visibility or {}}}


def make_registry(beta_datasets=('Study_C',)):
    raw = {'settings': dict(SETTINGS), 'bidsImport': {'enabled': False}, 'sites': [
        site_entry('alpha', ['Study_A', 'Study_B']),
        site_entry('beta', list(beta_datasets), {'Study_C': 'signed-in'} if 'Study_C' in beta_datasets else {}),
        site_entry('gamma', [], forgejo=False),
    ]}
    return registry.parse(raw), [s['name'] for s in raw['sites']]


def config(**overrides):
    values = dict(fullname='lfx', release='rel', public_url='https://forge.example', public_ip='10.0.0.1',
                  webhook_url=WEBHOOK, webhook_secret='hmac-secret', enrollment=True)
    values.update(overrides)
    return reconcile.Config(**values)


class FakeForgejo(reconcile.Forgejo):
    """In-memory Forgejo covering the endpoints the reconciler uses."""

    def __init__(self, dry_run=False):
        super().__init__('http://forgejo.invalid', 'admin', 'secret', dry_run=dry_run)
        self.orgs, self.repos, self.teams, self.users = {}, {}, {}, {}
        self.secrets, self.variables, self.hooks, self.files, self.runners = {}, {}, {}, {}, {}
        self.ids = iter(range(1, 100_000))
        self.log = []
        self.fail = None
        self.runner_delete_status = None

    def add_hook(self, org, url, **extra):
        hook = {'id': next(self.ids), 'type': 'forgejo', 'events': ['push'], 'active': True,
                'config': {'url': url, 'content_type': 'json'}, **extra}
        self.hooks.setdefault(org, {})[hook['id']] = hook
        return hook

    def tokens_of(self, user, name=None):
        return [t for t in self.users[user]['tokens'].values() if name is None or t['name'] == name]

    def request(self, method, path, body=None, sudo=None, missing_ok=False):
        url = urlsplit(path)
        p, query = unquote(url.path), dict(parse_qsl(url.query))
        if method != 'GET':
            self.log.append((method, p))
        if self.fail and self.fail(method, p, body):
            raise reconcile.ForgejoError(f'{method} {p}: HTTP 500 injected', 500)
        result = self._route(method, p, query, copy.deepcopy(body) if body is not None else {}, sudo)
        if result is KeyError:
            if missing_ok:
                return None
            raise reconcile.ForgejoError(f'{method} {path}: HTTP 404', 404)
        return copy.deepcopy(result)

    @staticmethod
    def _page(items, query):
        page, limit = int(query.get('page', 1)), int(query.get('limit', 50))
        return items[(page - 1) * limit:page * limit]

    def _route(self, m, p, query, body, sudo):  # noqa: C901 - a flat router reads best here
        if p == '/version':
            return {'version': 'fake'}
        if m == 'POST' and p == '/orgs':
            self.orgs[body['username']] = body
            return body
        if p == '/admin/orgs':
            return self._page([{'username': name} for name in self.orgs], query)
        if m == 'POST' and p == '/admin/users':
            self.users[body['username']] = {'tokens': {}, **body}
            return {'login': body['username']}
        if match := re.fullmatch(r'/orgs/([^/]+)', p):
            if match[1] not in self.orgs:
                return KeyError
            if m == 'PATCH':
                self.orgs[match[1]].update(body)
            return self.orgs[match[1]]
        if match := re.fullmatch(r'/orgs/([^/]+)/teams/search', p):
            return {'data': [team for team in self.teams.values()
                             if team['org'] == match[1] and query['q'] in team['name']]}
        if match := re.fullmatch(r'/orgs/([^/]+)/teams', p):
            team = {'id': next(self.ids), 'org': match[1], 'repos': set(), 'members': set(), **body}
            self.teams[team['id']] = team
            return {k: v for k, v in team.items() if k not in ('repos', 'members')}
        if match := re.fullmatch(r'/teams/(\d+)', p):
            self.teams[int(match[1])].update(body)
            return {}
        if match := re.fullmatch(r'/teams/(\d+)/repos', p):
            return self._page([{'name': r.split('/', 1)[1]} for r in sorted(self.teams[int(match[1])]['repos'])],
                              query)
        if match := re.fullmatch(r'/teams/(\d+)/(repos|members)/(.+)', p):
            members = self.teams[int(match[1])][match[2]]
            if m == 'PUT':
                members.add(match[3])
                return None
            return {} if match[3] in members else KeyError
        if match := re.fullmatch(r'/orgs/([^/]+)/repos', p):
            if m == 'GET':
                return self._page([{'name': r, **repo} for (o, r), repo in self.repos.items() if o == match[1]],
                                  query)
            repo = {'private': body['private'], 'has_actions': True, 'auto_init': body.get('auto_init', False),
                    'default_branch': body.get('default_branch')}
            self.repos[(match[1], body['name'])] = repo
            return repo
        if match := re.fullmatch(r'/repos/([^/]+)/([^/]+)/contents/(.+)', p):
            key = (match[1], match[2], match[3])
            if m == 'GET':
                if key not in self.files:
                    return KeyError
                return {'sha': str(hash(self.files[key])), 'content': base64.b64encode(self.files[key]).decode()}
            assert (m == 'PUT') == (key in self.files), (m, key)
            assert body['branch'] == 'main'
            self.files[key] = base64.b64decode(body['content'])
            return {}
        if match := re.fullmatch(r'/repos/([^/]+)/([^/]+)/git/trees/main', p):
            return {'tree': [{'path': f, 'type': 'blob'} for (o, r, f) in self.files
                             if (o, r) == (match[1], match[2])], 'truncated': False}
        if match := re.fullmatch(r'/repos/([^/]+)/([^/]+)', p):
            repo = self.repos.get((match[1], match[2]))
            if repo is None:
                return KeyError
            if m == 'PATCH':
                repo.update(body)
            return repo
        if match := re.fullmatch(r'/users/([^/]+)', p):
            return self.users.get(match[1], KeyError)
        if match := re.fullmatch(r'/users/([^/]+)/tokens', p):
            assert sudo == match[1], 'token operations must use sudo'
            if match[1] not in self.users:
                return KeyError
            if m == 'GET':
                return self._page([{'id': t['id'], 'name': t['name']} for t in self.tokens_of(match[1])], query)
            token_id = next(self.ids)
            token = {'id': token_id, 'name': body['name'], 'scopes': body['scopes'], 'sha1': f'tok-{token_id}'}
            self.users[match[1]]['tokens'][token_id] = token
            return token
        if match := re.fullmatch(r'/users/([^/]+)/tokens/(\d+)', p):
            assert sudo == match[1]
            del self.users[match[1]]['tokens'][int(match[2])]
            return None
        if match := re.fullmatch(r'/orgs/([^/]+)/actions/secrets', p):
            return self._page([{'name': n} for (o, n) in self.secrets if o == match[1]], query)
        if match := re.fullmatch(r'/orgs/([^/]+)/actions/secrets/(\w+)', p):
            self.secrets[(match[1], match[2])] = body['data']
            return None
        if match := re.fullmatch(r'/orgs/([^/]+)/actions/variables/(\w+)', p):
            key = (match[1], match[2])
            if m == 'GET':
                return {'data': self.variables[key]} if key in self.variables else KeyError
            assert (m == 'PUT') == (key in self.variables)
            self.variables[key] = body['value']
            return None
        if match := re.fullmatch(r'/orgs/([^/]+)/actions/runners', p):
            runners = self.runners.setdefault(match[1], {})
            if m == 'GET':
                return {'runners': [{'id': r['id'], 'name': r['name'], 'uuid': r['uuid']}
                                    for r in runners.values()]}
            runner_id = next(self.ids)
            runner = {'id': runner_id, 'name': body['name'], 'uuid': f'uuid-{runner_id}', 'token': f'reg-{runner_id}'}
            runners[runner_id] = runner
            return runner
        if match := re.fullmatch(r'/orgs/([^/]+)/actions/runners/(\d+)', p):
            if self.runner_delete_status:
                raise reconcile.ForgejoError(f'DELETE {p}: HTTP {self.runner_delete_status}',
                                             self.runner_delete_status)
            del self.runners[match[1]][int(match[2])]
            return None
        if match := re.fullmatch(r'/orgs/([^/]+)/hooks', p):
            if m == 'GET':
                return self._page([{k: v for k, v in h.items() if k != 'secret'}
                                   for h in self.hooks.get(match[1], {}).values()], query)
            hook = self.add_hook(match[1], body['config']['url'])
            hook['secret'] = body['config'].get('secret')
            return hook
        if match := re.fullmatch(r'/orgs/([^/]+)/hooks/(\d+)', p):
            hooks = self.hooks[match[1]]
            if m == 'DELETE':
                del hooks[int(match[2])]
            else:
                hook = hooks[int(match[2])]
                hook['secret'] = body['config'].get('secret')
                hook.update({k: v for k, v in body.items() if k != 'config'})
                hook['config'] = {k: v for k, v in body['config'].items() if k != 'secret'}
            return None
        raise AssertionError(f'unexpected request {m} {p}')


class FakeKube(reconcile.Kube):
    """In-memory Kubernetes API for Secrets and Leases."""

    def __init__(self):
        super().__init__('ns', sa_dir=Path('/nonexistent'))
        self.available = True
        self.secrets, self.leases = {}, {}
        self.log = []
        self.version = 0
        self.fail = None

    def secret(self, name):
        stored = self.secrets.get(name)
        if stored is None:
            return None
        return {key: base64.b64decode(value).decode() for key, value in stored['data'].items()}

    def request(self, method, path, body=None, content_type='application/json'):
        if method != 'GET':
            self.log.append((method, path + (f"/{body['metadata']['name']}" if method == 'POST' else '')))
        if self.fail and self.fail(method, path):
            raise reconcile.KubeError(f'{method} {path}: HTTP 500 injected', 500)
        body = copy.deepcopy(body)
        if match := re.fullmatch(r'/api/v1/namespaces/ns/secrets(?:/([^/]+))?', path):
            return copy.deepcopy(self._secret(method, unquote(match[1] or ''), body, content_type))
        if match := re.fullmatch(r'/apis/coordination.k8s.io/v1/namespaces/ns/leases(?:/([^/]+))?', path):
            return copy.deepcopy(self._lease(method, unquote(match[1] or ''), body))
        raise AssertionError(f'unexpected request {method} {path}')

    def _bump(self, obj):
        self.version += 1
        obj['metadata']['resourceVersion'] = str(self.version)
        return obj

    def _secret(self, method, name, body, content_type):
        if method == 'POST':
            name = body['metadata']['name']
            if name in self.secrets:
                raise reconcile.KubeError('conflict', 409)
            assert body['type'] == 'Opaque'
            self.secrets[name] = self._bump(body)
            return body
        if name not in self.secrets:
            raise reconcile.KubeError('not found', 404)
        if method == 'PATCH':
            assert content_type == 'application/merge-patch+json'
            secret = self.secrets[name]
            secret['metadata'].setdefault('labels', {}).update(body.get('metadata', {}).get('labels', {}))
            secret['data'].update(body.get('data', {}))
            self._bump(secret)
        return self.secrets[name]

    def _lease(self, method, name, body):
        if method == 'POST':
            name = body['metadata']['name']
            if name in self.leases:
                raise reconcile.KubeError('conflict', 409)
            self.leases[name] = self._bump(body)
            return body
        if name not in self.leases:
            raise reconcile.KubeError('not found', 404)
        if method == 'PUT':
            if body['metadata'].get('resourceVersion') != self.leases[name]['metadata']['resourceVersion']:
                raise reconcile.KubeError('conflict', 409)
            self.leases[name] = self._bump(body)
        return self.leases[name]

    def secret_writes(self):
        return [entry for entry in self.log if '/secrets' in entry[1]]


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeForgejo()
        self.kube = FakeKube()
        self.registry, self.known = make_registry()

    def run_reconciler(self, api=None, kube=None, reg=None, template=None, wait=0.0, **overrides):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reconcile.run(reg or self.registry, api or self.api, kube or self.kube, config(**overrides),
                                 TEMPLATE if template is None else template, self.known, 'pod-1',
                                 wait_seconds=wait, poll=0.01)
        self.output = out.getvalue()
        return code

    def enrollment(self, site):
        return self.kube.secret(f'lfx-enrollment-{site}')

    def test_first_run_creates_everything(self):
        self.assertEqual(self.run_reconciler(), 0, self.output)
        api = self.api
        self.assertEqual(api.orgs['lfx-alpha']['visibility'], 'private')
        self.assertEqual(api.orgs['lfx-alpha']['full_name'], 'LORIS site ALPHA')
        self.assertEqual(api.orgs['lfx-beta']['visibility'], 'limited')
        self.assertNotIn('lfx-gamma', api.orgs)

        teams = {(t['org'], t['name']): t for t in api.teams.values()}
        publication, export = teams[('lfx-alpha', 'datasets')], teams[('lfx-alpha', 'loris-export')]
        self.assertEqual((publication['permission'], publication['units'], publication['includes_all_repositories']),
                         ('write', ['repo.code'], False))
        self.assertEqual(publication['repos'], {'lfx-alpha/Study_A', 'lfx-alpha/Study_B'})
        self.assertEqual(publication['members'], {'lfx-alpha-worker'})
        self.assertEqual((export['permission'], sorted(export['units']), export['includes_all_repositories']),
                         ('write', ['repo.actions', 'repo.code'], True))
        self.assertEqual(export['members'], {'lfx-loris-export'})

        for name in ('Study_A', 'Study_B'):
            self.assertEqual((api.repos[('lfx-alpha', name)]['private'], api.repos[('lfx-alpha', name)]['has_actions']),
                             (True, False))
        self.assertFalse(api.repos[('lfx-beta', 'Study_C')]['private'])
        worker = api.repos[('lfx-alpha', '.worker')]
        self.assertEqual((worker['private'], worker['has_actions'], worker['auto_init'], worker['default_branch']),
                         (True, True, True, 'main'))
        self.assertEqual({f: c for (o, r, f), c in api.files.items() if o == 'lfx-alpha'}, TEMPLATE)

        for user in ('lfx-loris-export', 'lfx-alpha-worker', 'lfx-beta-worker'):
            self.assertIs(api.users[user]['restricted'], True)
            self.assertEqual(api.users[user]['visibility'], 'private')
        [bot_token] = api.tokens_of('lfx-loris-export', 'loris-forgejo-export')
        self.assertEqual(bot_token['scopes'], ['write:repository'])
        self.assertEqual(self.kube.secret('lfx-bot'), {'username': 'lfx-loris-export', 'token': bot_token['sha1']})
        self.assertEqual(self.kube.secrets['lfx-bot']['metadata']['labels']['app.kubernetes.io/component'], 'bot')
        [worker_token] = api.tokens_of('lfx-alpha-worker', 'loris-forgejo-worker')
        self.assertEqual(api.secrets[('lfx-alpha', 'LORIS_FORGEJO_WORKER_TOKEN')], worker_token['sha1'])
        self.assertEqual({n: v for (o, n), v in api.variables.items() if o == 'lfx-alpha'},
                         {'LORIS_FORGEJO_FORGE_URL': 'https://forge.example', 'LORIS_FORGEJO_FORGE_IP': '10.0.0.1'})
        [hook] = api.hooks['lfx-alpha'].values()
        self.assertEqual((hook['config'], hook['events'], hook['active'], hook['secret']),
                         ({'url': WEBHOOK, 'content_type': 'json'}, ['push'], True, 'hmac-secret'))

        [runner] = api.runners['lfx-alpha'].values()
        [publication_token] = api.tokens_of('lfx-alpha-worker', 'loris-forgejo-publication')
        self.assertEqual(self.enrollment('alpha'), {
            'forgeUrl': 'https://forge.example', 'forgeIP': '10.0.0.1', 'organization': 'lfx-alpha',
            'workerRepository': '.worker', 'runnerLabel': 'loris-forgejo-worker',
            'requestCommand': 'loris-forgejo-request', 'runnerName': 'alpha-worker',
            'runnerUUID': runner['uuid'], 'runnerToken': runner['token'],
            'publicationUser': 'lfx-alpha-worker', 'publicationToken': publication_token['sha1'],
            'repositories': '["Study_A", "Study_B"]'})
        self.assertEqual(runner['name'], 'alpha-worker')
        self.assertEqual(self.kube.secrets['lfx-enrollment-alpha']['metadata']['labels'], {
            'app.kubernetes.io/name': 'loris-forgejo-export', 'app.kubernetes.io/instance': 'rel',
            'app.kubernetes.io/component': 'enrollment', 'loris-forgejo-export/site': 'alpha'})
        # Written right after each identity: create with the runner keys, then patch the publication keys.
        alpha_writes = [m for m, p in self.kube.log if p.endswith('/lfx-enrollment-alpha')]
        self.assertEqual(alpha_writes, ['POST', 'PATCH'])
        self.assertIsNotNone(self.enrollment('beta'))
        self.assertIsNone(self.kube.leases['lfx-reconcile']['spec'].get('holderIdentity'))

    def test_second_run_writes_nothing_and_keeps_secrets(self):
        self.run_reconciler()
        secrets = copy.deepcopy(self.kube.secrets)
        self.api.log.clear()
        self.kube.log.clear()
        writes = self.api.writes
        self.assertEqual(self.run_reconciler(), 0, self.output)
        self.assertEqual(self.api.writes, writes)
        self.assertEqual(self.api.log, [('PATCH', f'/orgs/lfx-{s}/hooks/{next(iter(self.api.hooks[f"lfx-{s}"]))}')
                                        for s in ('alpha', 'beta')])
        self.assertEqual(self.kube.secret_writes(), [])
        self.assertEqual(self.kube.secrets, secrets)
        self.assertIn('done: 0 Forgejo writes, 0 Secret writes, 2 webhook secret refreshes', self.output)

    def test_registry_change_refreshes_repositories_but_keeps_enrollment_keys(self):
        self.run_reconciler()
        before = self.enrollment('beta')
        reg, _ = make_registry(beta_datasets=('Study_C', 'Study_D'))
        self.assertEqual(self.run_reconciler(reg=reg), 0, self.output)
        after = self.enrollment('beta')
        self.assertEqual(after['repositories'], '["Study_C", "Study_D"]')
        self.assertEqual({k: v for k, v in after.items() if k != 'repositories'},
                         {k: v for k, v in before.items() if k != 'repositories'})
        self.assertEqual(len(self.api.runners['lfx-beta']), 1)

    def test_rotation_after_secret_deletion(self):
        self.run_reconciler()
        old = self.enrollment('alpha')
        [old_runner] = self.api.runners['lfx-alpha']
        beta = self.enrollment('beta')
        del self.kube.secrets['lfx-enrollment-alpha']
        self.assertEqual(self.run_reconciler(), 0, self.output)
        new = self.enrollment('alpha')
        [runner] = self.api.runners['lfx-alpha'].values()
        self.assertNotEqual(runner['id'], old_runner)
        self.assertEqual((new['runnerUUID'], new['runnerToken']), (runner['uuid'], runner['token']))
        [token] = self.api.tokens_of('lfx-alpha-worker', 'loris-forgejo-publication')
        self.assertEqual(new['publicationToken'], token['sha1'])
        self.assertNotEqual(new['publicationToken'], old['publicationToken'])
        self.assertEqual(self.enrollment('beta'), beta)

    def test_rotation_of_one_key_pair(self):
        self.run_reconciler()
        old = self.enrollment('alpha')
        stored = self.kube.secrets['lfx-enrollment-alpha']['data']
        del stored['publicationToken']
        self.assertEqual(self.run_reconciler(), 0, self.output)
        new = self.enrollment('alpha')
        self.assertEqual((new['runnerUUID'], new['runnerToken']), (old['runnerUUID'], old['runnerToken']))
        self.assertNotEqual(new['publicationToken'], old['publicationToken'])
        self.assertEqual(len(self.api.tokens_of('lfx-alpha-worker', 'loris-forgejo-publication')), 1)

    def test_runner_rotation_fails_with_instructions_when_deletion_is_unsupported(self):
        self.run_reconciler()
        del self.kube.secrets['lfx-enrollment-alpha']
        self.api.runner_delete_status = 405
        self.assertEqual(self.run_reconciler(), 1)
        self.assertIn('alpha: FAILED: lfx-alpha: runner alpha-worker exists', self.output)
        self.assertIn('Actions > Runners', self.output)
        self.assertNotIn('lfx-enrollment-alpha', self.kube.secrets)

    def test_missing_bot_secret_replaces_token(self):
        self.run_reconciler()
        [old] = self.api.tokens_of('lfx-loris-export')
        del self.kube.secrets['lfx-bot']
        self.assertEqual(self.run_reconciler(), 0, self.output)
        [new] = self.api.tokens_of('lfx-loris-export')
        self.assertNotEqual(new['id'], old['id'])
        self.assertEqual(self.kube.secret('lfx-bot')['token'], new['sha1'])

    def test_empty_bot_secret_replaces_token(self):
        self.run_reconciler()
        self.kube.secrets['lfx-bot']['data']['token'] = ''
        self.assertEqual(self.run_reconciler(), 0, self.output)
        [new] = self.api.tokens_of('lfx-loris-export')
        self.assertEqual(self.kube.secret('lfx-bot')['token'], new['sha1'])

    def test_bot_secret_write_failure_deletes_new_token(self):
        self.kube.fail = lambda method, path: method != 'GET' and 'secrets' in path
        self.assertEqual(self.run_reconciler(enrollment=False), 1)
        self.assertEqual(self.api.tokens_of('lfx-loris-export'), [])
        self.assertIn('export bot: FAILED', self.output)

    def test_enrollment_secret_write_failure_deletes_new_identity(self):
        creations = []

        def fail(method, path):
            if method == 'POST' and path.endswith('/secrets'):
                creations.append(path)
                return len(creations) == 2  # the bot Secret, then alpha's enrollment Secret
            return False

        self.kube.fail = fail
        self.assertEqual(self.run_reconciler(), 1)
        self.assertEqual(self.api.runners['lfx-alpha'], {})
        self.assertEqual(self.api.tokens_of('lfx-alpha-worker', 'loris-forgejo-publication'), [])
        self.assertNotIn('lfx-enrollment-alpha', self.kube.secrets)
        self.assertIn('lfx-enrollment-beta', self.kube.secrets)

    def test_publication_secret_write_failure_deletes_new_token(self):
        self.kube.fail = lambda method, path: method == 'PATCH' and path.endswith('lfx-enrollment-alpha')
        self.assertEqual(self.run_reconciler(), 1)
        self.assertEqual(self.api.tokens_of('lfx-alpha-worker', 'loris-forgejo-publication'), [])
        self.assertNotIn('publicationToken', self.enrollment('alpha'))
        self.assertIn('runnerToken', self.enrollment('alpha'))

    def test_visibility_is_refused_when_unexpected(self):
        self.api.orgs['lfx-alpha'] = {'username': 'lfx-alpha', 'visibility': 'public'}
        self.assertEqual(self.run_reconciler(), 1)
        self.assertIn("unexpected organisation visibility 'public'", self.output)
        self.assertFalse([entry for entry in self.api.log if 'lfx-alpha' in entry[1]])
        self.assertEqual(self.api.orgs['lfx-beta']['visibility'], 'limited')

    def test_widening_to_limited_refuses_unmanaged_public_repository(self):
        self.api.orgs['lfx-beta'] = {'username': 'lfx-beta', 'visibility': 'private', 'full_name': 'LORIS site BETA'}
        self.api.repos[('lfx-beta', 'stray')] = {'private': False, 'has_actions': True}
        self.assertEqual(self.run_reconciler(), 1)
        self.assertIn('non-private unmanaged repository', self.output)
        self.assertEqual(self.api.orgs['lfx-beta']['visibility'], 'private')

    def test_visibility_narrows_when_no_dataset_is_signed_in(self):
        self.run_reconciler()
        reg, _ = make_registry(beta_datasets=('Study_D',))
        self.api.repos[('lfx-beta', 'Study_C')]['private'] = True
        self.assertEqual(self.run_reconciler(reg=reg), 0, self.output)
        self.assertEqual(self.api.orgs['lfx-beta']['visibility'], 'private')
        self.assertTrue(self.api.repos[('lfx-beta', 'Study_D')]['private'])
        self.assertIn('drift: repository lfx-beta/Study_C is not in the registry', self.output)

    def test_hook_dedupe_fix_and_secret_patch(self):
        self.run_reconciler()
        hooks = self.api.hooks['lfx-alpha']
        [first] = hooks
        hooks[first]['active'] = False
        hooks[first]['secret'] = 'stale'
        self.api.add_hook('lfx-alpha', WEBHOOK)
        self.api.add_hook('lfx-alpha', 'http://other.invalid')
        self.assertEqual(self.run_reconciler(), 0, self.output)
        self.assertEqual(sorted(h['config']['url'] for h in hooks.values()), sorted([WEBHOOK, 'http://other.invalid']))
        self.assertTrue(hooks[first]['active'])
        self.assertEqual(hooks[first]['secret'], 'hmac-secret')

    def test_no_hook_mode_only_reports(self):
        self.api.orgs['lfx-alpha'] = {'username': 'lfx-alpha', 'visibility': 'private', 'full_name': 'LORIS site ALPHA'}
        self.api.add_hook('lfx-alpha', 'http://old.invalid/push')
        self.assertEqual(self.run_reconciler(webhook_url='', webhook_secret=''), 0, self.output)
        self.assertEqual([h['config']['url'] for h in self.api.hooks['lfx-alpha'].values()],
                         ['http://old.invalid/push'])
        self.assertNotIn('lfx-beta', self.api.hooks)
        self.assertIn('lfx-alpha: webhook management disabled (scheduled sync only); existing hooks: '
                      'http://old.invalid/push', self.output)

    def test_no_secret_mode_does_not_refresh(self):
        self.run_reconciler(webhook_secret='')
        self.api.log.clear()
        self.run_reconciler(webhook_secret='')
        self.assertEqual(self.api.log, [])

    def test_worker_files_are_restored_and_extras_reported(self):
        self.run_reconciler()
        self.api.files[('lfx-alpha', '.worker', 'README.md')] = b'edited'
        self.api.files[('lfx-alpha', '.worker', 'extra.txt')] = b'kept'
        self.assertEqual(self.run_reconciler(), 0, self.output)
        self.assertEqual(self.api.files[('lfx-alpha', '.worker', 'README.md')], b'managed\n')
        self.assertEqual(self.api.files[('lfx-alpha', '.worker', 'extra.txt')], b'kept')
        self.assertIn('drift: file lfx-alpha/.worker/extra.txt is not in the worker template', self.output)

    def test_worker_repository_in_publication_team_fails_site(self):
        self.run_reconciler()
        team = next(t for t in self.api.teams.values() if (t['org'], t['name']) == ('lfx-alpha', 'datasets'))
        team['repos'].add('lfx-alpha/.worker')
        self.assertEqual(self.run_reconciler(), 1)
        self.assertIn('alpha: FAILED: lfx-alpha/.worker is in team datasets', self.output)

    def test_placeholders_are_substituted(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            (root / '..data').mkdir()
            (root / '.forgejo__workflows__x.yml').write_text(
                'runs-on: @RUNNER_LABEL@\nrun: @REQUEST_COMMAND@ stage "$DATASET" ${{ inputs.keys }}\n'
                'var: @VARIABLE_PREFIX@FORGE_URL @WORKER_REPOSITORY@\n')
            template = reconcile.read_template(root, self.registry.settings)
        self.assertEqual(template, {'.forgejo/workflows/x.yml': (
            b'runs-on: loris-forgejo-worker\nrun: loris-forgejo-request stage "$DATASET" ${{ inputs.keys }}\n'
            b'var: LORIS_FORGEJO_FORGE_URL .worker\n')})
        self.assertEqual(reconcile.read_template(Path(directory) / 'missing', self.registry.settings), {})

    def test_bundled_template_follows_the_contract(self):
        bundled = FILES / 'worker-template'
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            for path in bundled.rglob('*'):
                if path.is_file():
                    (Path(directory) / str(path.relative_to(bundled)).replace('/', '__')).write_bytes(path.read_bytes())
            template = {k: v.decode() for k, v in reconcile.read_template(directory, self.registry.settings).items()}
        self.assertEqual(set(template), {'README.md', '.forgejo/workflows/probe.yml', '.forgejo/workflows/refresh.yml',
                                         '.forgejo/workflows/stage-content.yml'})
        for path, content in template.items():
            self.assertNotRegex(content, r'@[A-Z_]+@', path)
            self.assertNotIn('re-move', content.lower(), path)
            if path.endswith('.yml'):
                self.assertIn('runs-on: loris-forgejo-worker\n', content)
        self.assertIn('loris-forgejo-request status', template['.forgejo/workflows/probe.yml'])
        self.assertIn('loris-forgejo-request probe "$dataset"', template['.forgejo/workflows/probe.yml'])
        self.assertIn('loris-forgejo-request refresh "$DATASET"', template['.forgejo/workflows/refresh.yml'])
        stage = template['.forgejo/workflows/stage-content.yml']
        self.assertIn('loris-forgejo-request stage "$DATASET"', stage)
        for needle in ('COMMIT: ${{ inputs.commit }}', 'KEYS: ${{ inputs.keys }}', 'DATASET: ${{ inputs.dataset }}'):
            self.assertIn(needle, stage)

    def test_lease_held_by_another_waits_then_fails_without_writes(self):
        self.kube.leases['lfx-reconcile'] = {'metadata': {'name': 'lfx-reconcile', 'resourceVersion': '1'}, 'spec': {
            'holderIdentity': 'other-pod', 'leaseDurationSeconds': 300, 'renewTime': reconcile.now_micro()}}
        start = time.monotonic()
        self.assertEqual(self.run_reconciler(wait=0.1), 1)
        self.assertGreaterEqual(time.monotonic() - start, 0.1)
        self.assertIn('lease lfx-reconcile is held by other-pod; waiting', self.output)
        self.assertEqual((self.api.log, self.kube.log, self.api.orgs), ([], [], {}))

    def test_expired_lease_is_taken_over(self):
        self.kube.leases['lfx-reconcile'] = {'metadata': {'name': 'lfx-reconcile', 'resourceVersion': '1'}, 'spec': {
            'holderIdentity': 'other-pod', 'leaseDurationSeconds': 300, 'renewTime': '2020-01-01T00:00:00.000000Z'}}
        self.assertEqual(self.run_reconciler(), 0, self.output)
        self.assertEqual(self.kube.leases['lfx-reconcile']['spec']['leaseTransitions'], 1)

    def test_lost_lease_aborts_before_next_write(self):
        lease = reconcile.Lease(self.kube, 'lfx-reconcile', 'pod-1', renew_every=0.01)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(lease.acquire(0))
            self.kube.fail = lambda method, path: 'leases' in path
            deadline = time.monotonic() + 5
            while lease.lost is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.api.guard = lease.check
            with self.assertRaises(reconcile.LeaseLost):
                self.api.write('x', 'POST', '/orgs', {})
            lease.release()
        self.assertEqual(self.api.log, [])

    def test_enrollment_disabled(self):
        self.assertEqual(self.run_reconciler(enrollment=False), 0, self.output)
        self.assertEqual(self.api.runners, {})
        self.assertEqual(self.api.tokens_of('lfx-alpha-worker', 'loris-forgejo-publication'), [])
        self.assertEqual(set(self.kube.secrets), {'lfx-bot'})

    def test_failure_in_one_site_does_not_stop_the_other(self):
        self.api.fail = lambda method, path, body: path == '/orgs' and body['username'] == 'lfx-alpha'
        self.assertEqual(self.run_reconciler(), 1)
        self.assertNotIn('lfx-alpha', self.api.orgs)
        self.assertIn('lfx-beta', self.api.orgs)
        self.assertIsNotNone(self.enrollment('beta'))
        self.assertIn('failed: alpha', self.output)

    def test_dry_run_writes_nothing(self):
        api = FakeForgejo(dry_run=True)
        kube = FakeKube()
        kube.available = False
        self.assertEqual(self.run_reconciler(api=api, kube=kube, dry_run=True), 0, self.output)
        self.assertEqual((api.log, kube.log, api.orgs), ([], [], {}))
        self.assertIn('would create organisation lfx-alpha', self.output)
        self.assertIn('would register runner lfx-alpha/alpha-worker', self.output)

    def test_drift_report(self):
        self.run_reconciler()
        self.api.orgs['lfx-legacy'] = {'username': 'lfx-legacy'}
        self.api.orgs['lfx-gamma'] = {'username': 'lfx-gamma'}
        self.api.orgs['unrelated'] = {'username': 'unrelated'}
        self.api.repos[('lfx-alpha', 'stray')] = {'private': True, 'has_actions': False}
        self.run_reconciler()
        self.assertIn('drift: organisation lfx-legacy is not managed', self.output)
        self.assertIn('drift: organisation lfx-gamma is not managed', self.output)
        self.assertIn('drift: repository lfx-alpha/stray is not in the registry', self.output)
        self.assertNotIn('unrelated', self.output)
        self.assertIn('lfx-legacy', self.api.orgs)

    def test_hook_helpers(self):
        hooks = [{'id': 9, 'config': {'url': WEBHOOK}}, {'id': 3, 'config': {'url': WEBHOOK}},
                 {'id': 1, 'config': {'url': 'http://other.invalid'}}]
        keep, delete = reconcile.plan_hooks(hooks, WEBHOOK)
        self.assertEqual((keep['id'], delete), (3, [9]))
        good = {'type': 'forgejo', 'active': True, 'events': ['push'],
                'config': {'url': WEBHOOK, 'content_type': 'json'}}
        self.assertTrue(reconcile.hook_matches(good, WEBHOOK))
        for change in ({'active': False}, {'events': ['push', 'issues']}, {'config': {'url': WEBHOOK}}):
            self.assertFalse(reconcile.hook_matches({**good, **change}, WEBHOOK))

    def test_release_name(self):
        self.assertEqual(reconcile.release_name('prod-loris-forgejo-export'), 'prod')
        self.assertEqual(reconcile.release_name('loris-forgejo-export'), 'loris-forgejo-export')


if __name__ == '__main__':
    unittest.main()
