import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))

import registry  # noqa: E402

SETTINGS = {
    'organizationPrefix': 'lfx-',
    'botUser': 'lfx-loris-export',
    'publicationBotSuffix': '-worker',
    'publicationTeam': 'datasets',
    'exportTeam': 'loris-export',
    'workerRepository': '.worker',
    'runnerLabel': 'loris-forgejo-worker',
    'requestCommand': 'loris-forgejo-request',
    'variablePrefix': 'LORIS_FORGEJO_',
    'dataClaim': 'loris-data',
    'clonesSubPath': 'forgejo/clones',
    'importCronJobPrefix': 'loris-bids-',
}


def site(name='alpha', **overrides):
    entry = {
        'name': name, 'enabled': True, 'source': 'pvc', 'claimName': 'loris-data',
        'subPath': f'forgejo/clones/{name}', 'siteAlias': name.upper(),
        'bidsImport': {'enabled': True, 'symlinkedContent': 'identity', 'updates': 'additive'},
        'forgejo': {'enabled': True, 'datasets': ['Study_A', 'Study_B']},
        'neurobagel': {'ignored': True},
    }
    entry.update(overrides)
    return entry


def data(*sites, enabled=True):
    return {'settings': copy.deepcopy(SETTINGS), 'bidsImport': {'enabled': enabled}, 'sites': list(sites)}


class RegistryTest(unittest.TestCase):
    def assertError(self, payload, fragment):
        errors = registry.validate(payload)
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_parses_managed_sites_and_ignores_others(self):
        nfs = {'name': 'legacy', 'source': 'nfs', 'server': 'x', 'enabled': True}
        parsed = registry.parse(data(site(forgejo={'enabled': True, 'datasets': ['Study_A'],
                                                   'visibility': {'Study_A': 'signed-in'}}),
                                     nfs, site('beta', forgejo={'enabled': False})))
        self.assertEqual([s.name for s in parsed.sites], ['alpha'])
        alpha = parsed.sites[0]
        self.assertEqual((alpha.org, alpha.bot), ('lfx-alpha', 'lfx-alpha-worker'))
        self.assertEqual(alpha.repos, ('Study_A', '.worker'))
        self.assertEqual(alpha.signed_in_datasets, ('Study_A',))
        self.assertEqual(alpha.import_cronjob, 'loris-bids-alpha')
        self.assertTrue(alpha.import_enabled)
        self.assertEqual(parsed.by_repository('lfx-alpha/Study_A'), (alpha, 'Study_A'))
        self.assertIsNone(parsed.by_repository('lfx-alpha/.worker'))
        self.assertIsNone(parsed.by_repository('other/Study_A'))

    def test_import_requires_common_switch(self):
        self.assertFalse(registry.parse(data(site(), enabled=False)).sites[0].import_enabled)

    def test_storage_contract(self):
        self.assertError(data(site(source='nfs')), 'require source pvc')
        self.assertError(data(site(server='nfs.example')), 'server or nfsPort')
        self.assertError(data(site(claimName='other')), "claimName must be 'loris-data'")
        self.assertError(data(site(subPath='forgejo/clones/beta')), "subPath must be 'forgejo/clones/alpha'")

    def test_dataset_names(self):
        for bad in (['A.git'], ['.worker'], ['../x'], ['a', 'A'], [3]):
            with self.subTest(bad=bad):
                self.assertTrue(registry.validate(data(site(forgejo={'enabled': True, 'datasets': bad}))))

    def test_empty_site_must_be_inactive(self):
        empty = {'enabled': True, 'datasets': []}
        self.assertError(data(site(forgejo=empty)), 'without datasets')
        self.assertEqual(registry.validate(data(site(forgejo=empty, enabled=False,
                                                     bidsImport={'enabled': False}))), [])

    def test_import_requirements(self):
        self.assertError(data(site(bidsImport={'enabled': True, 'updates': 'additive'})), 'symlinkedContent')
        self.assertError(data(site(bidsImport={'enabled': True, 'symlinkedContent': 'identity'})), 'updates')
        overrides = {'enabled': True, 'symlinkedContent': 'identity', 'updates': 'additive',
                     'datasets': {'Unknown': {}}}
        self.assertError(data(site(bidsImport=overrides)), 'not listed')

    def test_forgejo_keys_strict_and_visibility(self):
        self.assertError(data(site(forgejo={'enabled': True, 'datasets': ['A'], 'datasetAccess': {}})),
                         'unknown key')
        self.assertError(data(site(forgejo={'enabled': True, 'datasets': ['A'], 'visibility': {'A': 'public'}})),
                         'must be one of')
        self.assertError(data(site(forgejo={'enabled': True, 'datasets': ['A'], 'visibility': {'B': 'private'}})),
                         'not listed')

    def test_site_name_limits(self):
        self.assertEqual(registry.site_name_max(SETTINGS), 40 - 4 - 7)
        long = 'a' * 30
        self.assertError(data(site(long, subPath=f'forgejo/clones/{long}')), 'at most 29')
        self.assertError(data(site('Alpha', subPath='forgejo/clones/Alpha')), 'DNS label')
        self.assertError(data(site(), site()), 'duplicated')

    def test_identity_collisions(self):
        payload = data(site())
        payload['settings']['botUser'] = 'LFX-Alpha-Worker'
        self.assertError(payload, 'collides with settings.botUser')
        payload = data(site())
        payload['settings']['exportTeam'] = 'Datasets'
        self.assertError(payload, 'must differ')
        self.assertError(data(site('a'), site('a-worker', subPath='forgejo/clones/a-worker')), 'collides')
        self.assertError(data(site(forgejo={'enabled': True, 'datasets': ['.WORKER']})), 'not a valid')

    def test_settings_are_strict(self):
        payload = data(site())
        payload['settings']['variablePrefix'] = 'bad'
        self.assertError(payload, 'settings.variablePrefix')
        payload = data(site())
        payload['settings']['extra'] = 1
        self.assertError(payload, 'unknown key')

    def test_cli(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as stream:
            json.dump(data(site()), stream)
        self.assertEqual(registry.main(['registry.py', stream.name]), 0)
        Path(stream.name).unlink()


if __name__ == '__main__':
    unittest.main()
