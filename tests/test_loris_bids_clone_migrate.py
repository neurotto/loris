import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_loris_bids_runtime import runtime

spec = importlib.util.spec_from_file_location(
    'bids_clone_migrate', Path(runtime.__file__).with_name('bids_clone_migrate.py'))
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)
view_helper = runtime.helper('bids-view')
SCRIPT = Path(runtime.__file__).with_name('bids_clone_migrate.py')

pytestmark = pytest.mark.skipif(shutil.which('git-annex') is None, reason='git-annex is required')

FILES = {
    'dataset_description.json': b'{"Name": "Penthera", "BIDSVersion": "1.8.0"}',
    'participants.tsv': b'participant_id\nsub-PT01\n',
    'sub-PT01/anat/sub-PT01_T1w.nii.gz': b'T1w image bytes',
    'sub-PT01/anat/sub-PT01_T1w.json': b'{"EchoTime": 0.003}',
    'sub-PT01/dwi/sub-PT01_dwi.nii.gz': b'dwi image bytes',
}


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, text=True).stdout


def write(root, files):
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)


def annex_clone(source, files, backend):
    """Turn source into a git-annex repository with *.nii.gz annexed (locked) and their content dropped."""
    source.mkdir(parents=True)
    write(source, files)
    git(source, 'init', '-q')
    git(source, 'config', 'user.email', 't@example.org')
    git(source, 'config', 'user.name', 'T')
    git(source, 'annex', 'init', '-q')
    (source / '.gitattributes').write_text(f'* annex.backend={backend}\n* annex.largefiles=nothing\n'
                                           '*.nii.gz annex.largefiles=anything\n')
    git(source, 'add', '.gitattributes')
    git(source, 'annex', 'add', '-q', '.')
    git(source, 'commit', '-q', '-m', 'dataset')
    git(source, 'annex', 'drop', '-q', '--force', '.')


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


@pytest.fixture
def world(tmp_path):
    """An imported NFS-style view of <site>/<dataset>, plus its prepared journal."""
    source = tmp_path / 'assembly_bids/nandou/Penthera_3T'
    source.mkdir(parents=True)
    write(source, FILES)
    view = tmp_path / 'assembly_bids/import-penthera-raw-001'
    view_helper.prepare_view(source, view, 'Penthera_3T', 'nandou', 'Penthera-3T')
    journal = tmp_path / 'private/clone-migration.json'
    result = run('prepare', '--journal', journal, '--view', view, '--source', source)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'action': 'prepare', 'links': 4}
    assert journal.stat().st_mode & 0o777 == 0o600
    return {'root': tmp_path, 'source': source, 'view': view, 'journal': journal}


def switch_to_clone(world, files=FILES, backend='SHA256E'):
    """Unmount the NFS export (moved aside) and check out the clone at the same path."""
    nfs = world['root'] / 'nfs-export'
    world['source'].rename(nfs)
    annex_clone(world['source'], files, backend)
    return nfs


def view_links(view):
    return {str(path.relative_to(view)): os.readlink(path) for path in sorted(view.rglob('*')) if path.is_symlink()}


@pytest.mark.parametrize('backend', ['SHA256E', 'MD5E'])
def test_apply_retargets_the_view_to_annex_objects_without_content(world, backend):
    switch_to_clone(world, backend=backend)
    before = view_links(world['view'])

    dry = run('apply', '--journal', world['journal'])
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert json.loads(dry.stdout) == {'action': 'apply', 'write': False, 'verified': 4, 'annexed': 2,
                                      'links_to_change': 2}
    assert view_links(world['view']) == before

    applied = run('apply', '--journal', world['journal'], '--write')
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)['links_to_change'] == 2
    links = view_links(world['view'])
    assert '/.git/annex/objects/' in links['sub-PT01/anat/sub-PT01_T1w.nii.gz']
    assert links['sub-PT01/anat/sub-PT01_T1w.json'] == before['sub-PT01/anat/sub-PT01_T1w.json']
    assert not (world['source'] / 'sub-PT01/anat/sub-PT01_T1w.nii.gz').exists()      # content still absent

    # The importer's own view check for clone sources now accepts the view (only additions).
    view_helper.prepare_view(world['source'], world['view'], 'Penthera_3T', 'nandou', 'Penthera-3T',
                             reuse=True, extend=True)
    assert view_links(world['view'])['sub-PT01/anat/sub-PT01_T1w.nii.gz'] == links['sub-PT01/anat/sub-PT01_T1w.nii.gz']

    again = run('apply', '--journal', world['journal'], '--write')
    assert json.loads(again.stdout)['links_to_change'] == 0


def test_the_unmigrated_view_is_rejected_by_the_clone_import(world):
    switch_to_clone(world)
    with pytest.raises(ValueError, match='removed or changed source files need review'):
        view_helper.prepare_view(world['source'], world['view'], 'Penthera_3T', 'nandou', 'Penthera-3T',
                                 reuse=True, extend=True)


def test_changed_or_missing_clone_files_stop_apply(world):
    files = dict(FILES)
    files['sub-PT01/anat/sub-PT01_T1w.nii.gz'] = b'other T1w bytes'
    del files['sub-PT01/dwi/sub-PT01_dwi.nii.gz']
    switch_to_clone(world, files)
    before = view_links(world['view'])
    result = run('apply', '--journal', world['journal'], '--write')
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report['problem_count'] == 2
    assert any('differs from the imported file' in problem for problem in report['problems'])
    assert any('missing in the clone' in problem for problem in report['problems'])
    assert view_links(world['view']) == before


def test_mark_imported_only_when_every_annexed_file_was_verified(world):
    switch_to_clone(world)
    result = run('apply', '--journal', world['journal'], '--write', '--clone', world['source'])
    assert json.loads(result.stdout)['imported']['marked'] is True
    assert git(world['source'], 'rev-parse', 'refs/re-move/imported') == git(world['source'], 'rev-parse', 'HEAD')


def test_mark_imported_refuses_unverified_additions_and_a_held_lock(world):
    switch_to_clone(world, {**FILES, 'sub-PT02/anat/sub-PT02_T1w.nii.gz': b'new subject'})
    result = run('apply', '--journal', world['journal'], '--write', '--clone', world['source'])
    imported = json.loads(result.stdout)['imported']
    assert imported == {'marked': False, 'unverified_annexed_files': 1,
                        'examples': ['sub-PT02/anat/sub-PT02_T1w.nii.gz']}
    assert subprocess.run(['git', '-C', str(world['source']), 'rev-parse', '--verify', '-q',
                           'refs/re-move/imported'], capture_output=True).returncode == 1

    os.remove(world['source'] / 'sub-PT02/anat/sub-PT02_T1w.nii.gz')
    with open(world['source'].parent / '.Penthera_3T.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        held = run('apply', '--journal', world['journal'], '--write', '--clone', world['source'])
    assert held.returncode == 1 and 'holds the dataset lock' in held.stderr


def test_rollback_restores_the_nfs_links(world):
    nfs = switch_to_clone(world)
    run('apply', '--journal', world['journal'], '--write')
    migrated = view_links(world['view'])

    refused = run('rollback', '--journal', world['journal'], '--write')
    assert refused.returncode == 1 and 'Mount the NFS export again' in refused.stderr

    shutil.rmtree(world['source'])
    nfs.rename(world['source'])
    result = run('rollback', '--journal', world['journal'], '--write')
    assert json.loads(result.stdout) == {'action': 'rollback', 'write': True, 'links_to_restore': 2}
    original = view_links(world['view'])
    assert original != migrated
    assert all(not target.count('/.git/') for target in original.values())
    view_helper.prepare_view(world['source'], world['view'], 'Penthera_3T', 'nandou', 'Penthera-3T', reuse=True)


def test_prepare_refuses_to_overwrite_a_journal(world):
    result = run('prepare', '--journal', world['journal'], '--view', world['view'], '--source', world['source'])
    assert result.returncode == 1 and 'Journal already exists' in result.stderr


def test_key_matching():
    recorded = {'size': 3, 'md5': 'a' * 32, 'sha1': 'b' * 40, 'sha256': 'c' * 64, 'sha512': 'd' * 128}
    assert migrate.key_matches('SHA256E-s3--' + 'c' * 64 + '.nii.gz', recorded)
    assert migrate.key_matches('MD5-s3--' + 'a' * 32, recorded)
    assert not migrate.key_matches('SHA256E-s4--' + 'c' * 64 + '.nii.gz', recorded)
    assert not migrate.key_matches('MD5E-s3--' + 'e' * 32 + '.gz', recorded)
    assert migrate.key_matches('WORM-s3-m1--name.nii.gz', recorded) is None
