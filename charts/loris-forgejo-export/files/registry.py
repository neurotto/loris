#!/usr/bin/env python3
"""Load and validate the loris-forgejo-export site registry.

The chart renders ``registry.json`` from its values::

    {"settings": {...}, "bidsImport": {"enabled": bool}, "sites": [<LORIS sites[] entries>]}

``sites`` uses the LORIS chart format so one values file can feed both charts.
Only entries with ``forgejo.enabled`` are managed; other entries are ignored
apart from unique names. This module never contacts Forgejo or Kubernetes.

Run ``python3 registry.py registry.json`` to validate a file; it prints a JSON
summary and exits 1 on errors.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

FORGEJO_NAME_MAX = 40
DNS_LABEL_RE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')
PREFIX_RE = re.compile(r'^([a-z0-9][a-z0-9-]*-)?$')
USER_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$')
REPOSITORY_RE = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
DATASET_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
SUBPATH_RE = re.compile(r'^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$')
VARIABLE_PREFIX_RE = re.compile(r'^[A-Z][A-Z0-9_]*_$')
COMMAND_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
RESERVED_REPOSITORY_SUFFIXES = ('.git', '.wiki', '.rss', '.atom')
FORGEJO_KEYS = {'enabled', 'datasets', 'visibility'}
VISIBILITIES = ('private', 'signed-in')

SETTINGS = {
    'organizationPrefix': (str, PREFIX_RE),
    'botUser': (str, USER_RE),
    'publicationBotSuffix': (str, re.compile(r'^[a-z0-9-]{1,10}$')),
    'publicationTeam': (str, USER_RE),
    'exportTeam': (str, USER_RE),
    'workerRepository': (str, REPOSITORY_RE),
    'runnerLabel': (str, re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')),
    'requestCommand': (str, COMMAND_RE),
    'variablePrefix': (str, VARIABLE_PREFIX_RE),
    'dataClaim': (str, DNS_LABEL_RE),
    'clonesSubPath': (str, SUBPATH_RE),
    'importCronJobPrefix': (str, re.compile(r'^[a-z0-9][a-z0-9-]*$')),
}


class RegistryError(ValueError):
    """Raised when the registry is invalid; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__('invalid site registry:\n  ' + '\n  '.join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Settings:
    organizationPrefix: str
    botUser: str
    publicationBotSuffix: str
    publicationTeam: str
    exportTeam: str
    workerRepository: str
    runnerLabel: str
    requestCommand: str
    variablePrefix: str
    dataClaim: str
    clonesSubPath: str
    importCronJobPrefix: str


@dataclass(frozen=True)
class Site:
    """One managed site: a Forgejo organization with one repository per dataset."""

    name: str
    alias: str | None
    enabled: bool
    datasets: tuple[str, ...]
    import_enabled: bool
    signed_in_datasets: tuple[str, ...]
    settings: Settings

    @property
    def org(self) -> str:
        return self.settings.organizationPrefix + self.name

    @property
    def bot(self) -> str:
        """Per-site publication bot whose token the site worker pushes with."""
        return self.org + self.settings.publicationBotSuffix

    @property
    def worker_repository(self) -> str:
        return self.settings.workerRepository

    @property
    def repos(self) -> tuple[str, ...]:
        return self.datasets + (self.settings.workerRepository,)

    @property
    def import_cronjob(self) -> str:
        return self.settings.importCronJobPrefix + self.name

    @property
    def subpath(self) -> str:
        return f'{self.settings.clonesSubPath}/{self.name}'


@dataclass(frozen=True)
class Registry:
    settings: Settings
    sites: tuple[Site, ...]

    def site(self, name: str) -> Site | None:
        return next((site for site in self.sites if site.name == name), None)

    def by_repository(self, repository: str) -> tuple[Site, str] | None:
        """Return the site and dataset of ``<org>/<dataset>``, or None if it is not a managed dataset."""
        org, _, dataset = repository.partition('/')
        for site in self.sites:
            if site.org == org and dataset in site.datasets:
                return site, dataset
        return None


def _validate_settings(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        return ['settings must be a mapping']
    errors = [f'settings: unknown key {key!r}' for key in sorted(set(raw) - set(SETTINGS))]
    for key, (kind, pattern) in SETTINGS.items():
        value = raw.get(key)
        if not isinstance(value, kind) or not pattern.match(value):
            errors.append(f'settings.{key} is invalid: {value!r}')
    if not errors:
        if len(raw['botUser']) > FORGEJO_NAME_MAX:
            errors.append(f'settings.botUser must have at most {FORGEJO_NAME_MAX} characters')
        if raw['workerRepository'].lower().endswith(RESERVED_REPOSITORY_SUFFIXES):
            errors.append('settings.workerRepository uses a reserved suffix')
        if raw['workerRepository'] in ('.', '..'):
            errors.append('settings.workerRepository is invalid')
        if raw['publicationTeam'].casefold() == raw['exportTeam'].casefold():
            errors.append('settings.publicationTeam and settings.exportTeam must differ')
    return errors


def _identity_collisions(settings: dict, names: list[str]) -> list[str]:
    """Reject Forgejo account names shared between the export bot, site organizations and publication bots.

    Forgejo users and organizations share one case-insensitive namespace; a collision could hand site-held
    publication credentials the export bot's access to every site.
    """
    owners: dict[str, str] = {settings['botUser'].casefold(): 'settings.botUser'}
    errors = []
    for name in names:
        for kind, account in (('organization', settings['organizationPrefix'] + name),
                              ('publication bot', settings['organizationPrefix'] + name
                               + settings['publicationBotSuffix'])):
            key = account.casefold()
            if key in owners:
                errors.append(f'site {name!r}: {kind} {account!r} collides with {owners[key]}')
            else:
                owners[key] = f'the {kind} of site {name!r}'
    return errors


def site_name_max(settings: dict) -> int:
    """Longest site name whose organization and publication bot fit Forgejo's name limit."""
    return FORGEJO_NAME_MAX - len(settings['organizationPrefix']) - len(settings['publicationBotSuffix'])


def validate(data: object) -> list[str]:
    """Return every problem in a parsed registry; an empty list means valid."""
    if not isinstance(data, dict):
        return ['registry must be a mapping']
    errors = [f'unknown top-level key {key!r}' for key in sorted(set(data) - {'settings', 'bidsImport', 'sites'})]
    settings_errors = _validate_settings(data.get('settings'))
    if settings_errors:
        return errors + settings_errors
    settings = data['settings']
    common = data.get('bidsImport') or {}
    if not isinstance(common, dict):
        errors.append('bidsImport must be a mapping')
    sites = data.get('sites') or []
    if not isinstance(sites, list):
        return errors + ['sites must be a list']

    names: set[str] = set()
    managed: list[str] = []
    limit = site_name_max(settings)
    for index, site in enumerate(sites):
        where = f'sites[{index}]'
        if not isinstance(site, dict):
            errors.append(f'{where} must be a mapping')
            continue
        name = site.get('name')
        if not isinstance(name, str) or not name:
            errors.append(f'{where}.name is required')
            continue
        if name in names:
            errors.append(f'{where}.name {name!r} is duplicated')
            continue
        names.add(name)
        forgejo = site.get('forgejo')
        if forgejo is None:
            continue
        where = f'site {name!r}'
        if not isinstance(forgejo, dict):
            errors.append(f'{where}.forgejo must be a mapping')
            continue
        errors += [f'{where}.forgejo: unknown key {key!r}' for key in sorted(set(forgejo) - FORGEJO_KEYS)]
        if not isinstance(forgejo.get('enabled', False), bool):
            errors.append(f'{where}.forgejo.enabled must be boolean')
        if forgejo.get('enabled') is not True:
            continue

        if len(name) > limit or not DNS_LABEL_RE.match(name):
            errors.append(f'{where}.name must be a lowercase DNS label of at most {limit} characters')
        managed.append(name)
        if not isinstance(site.get('enabled', False), bool):
            errors.append(f'{where}.enabled must be boolean')
        if site.get('source') != 'pvc':
            errors.append(f'{where}: Forgejo sites require source pvc')
        if 'server' in site or 'nfsPort' in site:
            errors.append(f'{where}: Forgejo sites must not set server or nfsPort')
        if site.get('claimName') != settings['dataClaim']:
            errors.append(f'{where}.claimName must be {settings["dataClaim"]!r}')
        expected = f'{settings["clonesSubPath"]}/{name}'
        if site.get('subPath') != expected:
            errors.append(f'{where}.subPath must be {expected!r}')

        datasets = forgejo.get('datasets', [])
        if not isinstance(datasets, list):
            errors.append(f'{where}.forgejo.datasets must be a list')
            datasets = []
        seen: set[str] = set()
        for dataset in datasets:
            if (not isinstance(dataset, str) or not DATASET_RE.match(dataset)
                    or dataset.lower().endswith(RESERVED_REPOSITORY_SUFFIXES)
                    or dataset.casefold() == settings['workerRepository'].casefold()):
                errors.append(f'{where}.forgejo.datasets: {dataset!r} is not a valid repository/folder name')
            elif dataset.casefold() in seen:
                errors.append(f'{where}.forgejo.datasets: {dataset!r} is duplicated')
            else:
                seen.add(dataset.casefold())
        visibility = forgejo.get('visibility', {})
        if not isinstance(visibility, dict):
            errors.append(f'{where}.forgejo.visibility must be a mapping')
        else:
            for dataset, mode in visibility.items():
                if dataset not in datasets:
                    errors.append(f'{where}.forgejo.visibility: {dataset!r} is not listed in forgejo.datasets')
                if mode not in VISIBILITIES:
                    errors.append(f'{where}.forgejo.visibility.{dataset} must be one of {", ".join(VISIBILITIES)}')

        imports = site.get('bidsImport') or {}
        if not isinstance(imports, dict):
            errors.append(f'{where}.bidsImport must be a mapping')
            imports = {}
        importing = bool(isinstance(common, dict) and common.get('enabled') and site.get('enabled')
                         and imports.get('enabled'))
        if not datasets and (site.get('enabled') or imports.get('enabled')):
            errors.append(f'{where}: sites without datasets must remain disabled and nonimporting')
        if importing:
            if imports.get('symlinkedContent') != 'identity':
                errors.append(f'{where}: importing Forgejo sites require bidsImport.symlinkedContent identity')
            if imports.get('updates') != 'additive':
                errors.append(f'{where}: importing Forgejo sites require bidsImport.updates additive')
        overrides = imports.get('datasets', {})
        if isinstance(overrides, dict):
            for folder in sorted(set(overrides) - set(datasets)):
                errors.append(f'{where}.bidsImport.datasets.{folder}: not listed in forgejo.datasets')
    return errors + _identity_collisions(settings, managed)


def parse(data: object) -> Registry:
    """Validate a parsed registry and return it; raise :class:`RegistryError` listing every problem."""
    errors = validate(data)
    if errors:
        raise RegistryError(errors)
    settings = Settings(**data['settings'])
    common = data.get('bidsImport') or {}
    sites = []
    for raw in data.get('sites') or []:
        forgejo = raw.get('forgejo') or {}
        if forgejo.get('enabled') is not True:
            continue
        imports = raw.get('bidsImport') or {}
        sites.append(Site(
            name=raw['name'],
            alias=raw.get('siteAlias'),
            enabled=raw.get('enabled', False),
            datasets=tuple(forgejo.get('datasets', [])),
            import_enabled=bool(common.get('enabled') and raw.get('enabled') and imports.get('enabled')),
            signed_in_datasets=tuple(d for d, mode in (forgejo.get('visibility') or {}).items()
                                     if mode == 'signed-in'),
            settings=settings,
        ))
    return Registry(settings, tuple(sites))


def load(path: str | Path) -> Registry:
    """Read, validate and return a registry JSON file."""
    with open(path, encoding='utf-8') as stream:
        return parse(json.load(stream))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f'usage: {argv[0]} registry.json', file=sys.stderr)
        return 2
    try:
        registry = load(argv[1])
    except RegistryError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps([{'name': s.name, 'org': s.org, 'bot': s.bot, 'repos': list(s.repos),
                       'import': s.import_enabled, 'signedIn': list(s.signed_in_datasets)}
                      for s in registry.sites], indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
