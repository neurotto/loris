"""Move an imported NFS-sourced dataset to a central git-annex clone without re-importing it.

A site moving from ``source: nfs`` to ``source: clone`` keeps its LORIS mount path
(``assembly_bids/<site>/<dataset>``), but views of clone sources link annexed files
to their annex object paths (bids-view.py), which do not depend on content
presence. Existing views link to the plain NFS file paths, so the importer's view
check would report every image as changed. LORIS registrations reference view
paths and are not changed.

prepare   while the NFS mount still serves the view: hash every view link target
          and save a private journal (sizes and md5/sha1/sha256/sha512 digests).
apply     after the switch, once the clone is checked out at the same path:
          verify each file against the clone, then retarget the view links. An
          annexed file is verified by its git-annex key, which embeds the content
          size and hash, so no content is fetched; other files are hashed.
          With --clone (the writable clone directory), the clone is also marked
          imported (refs/re-move/imported = HEAD) when every annexed file of the
          clone was verified, so the sync job does not stage the whole dataset.
rollback  restore the original links; mount the NFS export again first.

Without --write, apply and rollback only report what they would do. Nothing in
the source, the clone content or the database is changed.
"""

import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ALGORITHMS = ('md5', 'sha1', 'sha256', 'sha512')
KEY = re.compile(r'^(?P<backend>[A-Z0-9]+?)(?P<extension>E?)(?:-s(?P<size>[0-9]+))?(?:-[a-zA-Z][0-9]+)*--(?P<name>.+)$')
BACKENDS = {'MD5': 'md5', 'SHA1': 'sha1', 'SHA256': 'sha256', 'SHA512': 'sha512'}
IMPORTED_REF = 'refs/re-move/imported'
SKIPPED_PREFIXES = ('derivatives/', '.datalad/', '.git/')


def bids_view():
    """Load the sibling bids-view.py helper."""
    spec = importlib.util.spec_from_file_location('bids_view', Path(__file__).with_name('bids-view.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digests(path):
    """Return the size and all supported digests of a file, following symlinks."""
    hashes = {name: hashlib.new(name) for name in ALGORITHMS}
    size = 0
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            size += len(block)
            for digest in hashes.values():
                digest.update(block)
    return {'size': size, **{name: digest.hexdigest() for name, digest in hashes.items()}}


def key_matches(key, recorded):
    """Tell whether a git-annex key names content with the recorded size and digest.

    Returns None for backends that do not embed a supported hash (WORM, URL, ...).
    """
    match = KEY.match(key)
    if not match or match['backend'] not in BACKENDS:
        return None
    name = match['name'].split('.', 1)[0] if match['extension'] else match['name']
    if match['size'] is not None and int(match['size']) != recorded['size']:
        return False
    return name == recorded[BACKENDS[match['backend']]]


def save_journal(path, journal):
    """Atomically write a mode-0600 journal; it lists participant file paths."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.bids-clone-migration-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(journal, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(view, source, view_helper):
    """Record every view link below source with the digests of its current target."""
    if view.is_symlink() or not view.is_dir():
        raise ValueError('Select an existing generated import view')
    if not (source / 'dataset_description.json').is_file():
        raise ValueError('The source must be the mounted dataset directory')
    links = []
    for path in sorted(view.rglob('*')):
        if not path.is_symlink():
            if path.is_file() and path.name not in ('participants.tsv', view_helper.ARTIFACTS):
                raise ValueError('Unexpected regular file in the import view: ' + str(path))
            continue
        target = Path(os.readlink(path))
        if not target.is_absolute() or not target.is_relative_to(source):
            raise ValueError('View link outside the selected source: ' + str(path))
        links.append({'path': str(path), 'before': str(target), **digests(path)})
    if not links:
        raise ValueError('The view has no source links')
    return {'version': 1, 'view': str(view), 'source': str(source), 'links': links}


def plan(journal, view_helper):
    """Compute the clone target of every journaled link and verify it.

    Returns (changes, problems, verified_keys): changes maps view paths to
    (before, after); problems lists files that could not be verified.
    """
    source = Path(journal['source'])
    objects = source / '.git' / 'annex' / 'objects'
    changes, problems, keys = {}, [], set()
    for link in journal['links']:
        before = Path(link['before'])
        if before.is_relative_to(objects):
            # The NFS dataset was already a git-annex repository: same object path in the clone.
            after, key = before, before.name
        elif before.is_symlink() and (annexed := view_helper.annex_object(source, before)) is not None:
            after, key = annexed, annexed.name
        elif before.is_file() and not before.is_symlink():
            after, key = before.resolve(strict=True), None
        else:
            problems.append(f"{link['path']}: {before} is missing in the clone")
            continue
        if key is not None:
            verdict = key_matches(key, link)
            if verdict is None and after.is_file():
                verdict = all(digests(after)[name] == link[name] for name in ALGORITHMS)
            if verdict is None:
                problems.append(f"{link['path']}: key {key} has no content hash; get its content first")
                continue
            if not verdict:
                problems.append(f"{link['path']}: clone content differs from the imported file")
                continue
            keys.add(key)
        elif digests(after) != {name: link[name] for name in ('size', *ALGORITHMS)}:
            problems.append(f"{link['path']}: clone file differs from the imported file")
            continue
        changes[link['path']] = (link['before'], str(after))
    return changes, problems, keys


def relink(changes, reverse=False, write=False):
    """Retarget view links atomically; accept either state so interrupted runs resume."""
    pending = []
    for path, (before, after) in sorted(changes.items()):
        old, new = (after, before) if reverse else (before, after)
        current = os.readlink(path) if os.path.islink(path) else None
        if current not in (old, new):
            raise ValueError('View link changed outside the migration: ' + path)
        if current != new:
            pending.append((Path(path), new))
    if write:
        for path, target in pending:
            temporary = path.with_name('.' + path.name + '.loris-clone-migrate')
            if os.path.lexists(temporary):
                temporary.unlink()
            temporary.symlink_to(target)
            temporary.replace(path)
    return len(pending)


def annexed_keys(clone, view_helper):
    """Annex keys of the clone work tree, outside the root directories the sync job never imports."""
    keys = {}
    for current, directories, files in os.walk(clone):
        relative = Path(current).relative_to(clone)
        directories[:] = [name for name in directories if name != '.git'
                          and not (relative == Path('.') and name + '/' in SKIPPED_PREFIXES)]
        for name in files:
            annexed = view_helper.annex_object(clone, Path(current) / name)
            if annexed is not None:
                keys[annexed.name] = str(relative / name)
    return keys


def mark_imported(clone, source, verified, view_helper, write=False):
    """Set refs/re-move/imported to HEAD when every annexed clone file was verified."""
    if not (clone / '.git').is_dir() or not os.path.samefile(clone / '.git', source / '.git'):
        raise ValueError('--clone must be the writable directory of the clone mounted at the source path')
    missing = sorted(path for key, path in annexed_keys(clone, view_helper).items() if key not in verified)
    if missing:
        return {'marked': False, 'unverified_annexed_files': len(missing), 'examples': missing[:5]}
    with ExitStack() as stack:
        lock = stack.enter_context(open(clone.parent / f'.{clone.name}.lock', 'w'))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A re-move-sync run holds the dataset lock; retry when it ends')
        head = subprocess.run(['git', '-C', str(clone), 'rev-parse', 'HEAD'], check=True,
                              capture_output=True, text=True).stdout.strip()
        if write:
            subprocess.run(['git', '-C', str(clone), 'update-ref', IMPORTED_REF, head], check=True)
    return {'marked': write, 'commit': head}


def main():
    parser = argparse.ArgumentParser(description='Retarget an NFS import view to a central git-annex clone')
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--journal', required=True, type=Path)
    parser.add_argument('--view', type=Path, help='prepare: the generated import view')
    parser.add_argument('--source', type=Path, help='prepare: the mounted dataset directory')
    parser.add_argument('--clone', type=Path, help='apply: writable clone directory, to mark it imported')
    parser.add_argument('--write', action='store_true', help='apply/rollback: change the view (default: report)')
    args = parser.parse_args()
    view_helper = bids_view()

    try:
        if args.action == 'prepare':
            if args.view is None or args.source is None:
                parser.error('prepare requires --view and --source')
            if args.journal.exists():
                raise ValueError('Journal already exists; it holds the only record of the imported content')
            journal = prepare(args.view.absolute(), args.source.absolute(), view_helper)
            save_journal(args.journal, journal)
            print(json.dumps({'action': 'prepare', 'links': len(journal['links'])}))
            return

        journal = json.loads(args.journal.read_text())
        if args.action == 'rollback':
            changes = {link['path']: (link['before'], link['after'])
                       for link in journal['links'] if 'after' in link}
            for path, (before, _) in changes.items():
                if not Path(before).exists():
                    raise ValueError('Mount the NFS export again before rolling back: ' + before)
            count = relink(changes, reverse=True, write=args.write)
            print(json.dumps({'action': 'rollback', 'write': args.write, 'links_to_restore': count}))
            return

        changes, problems, verified = plan(journal, view_helper)
        if problems:
            print(json.dumps({'action': 'apply', 'verified': len(changes), 'problems': problems[:20],
                              'problem_count': len(problems)}))
            raise SystemExit(1)
        count = relink(changes, write=args.write)
        if args.write:
            for link in journal['links']:
                link['after'] = changes[link['path']][1]
            save_journal(args.journal, journal)
        result = {'action': 'apply', 'write': args.write, 'verified': len(changes),
                  'annexed': len(verified), 'links_to_change': count}
        if args.clone is not None:
            result['imported'] = mark_imported(args.clone.absolute(), Path(journal['source']), verified,
                                               view_helper, write=args.write)
        print(json.dumps(result))
    except (ValueError, OSError) as error:
        raise SystemExit('Clone migration failed: ' + str(error))


if __name__ == '__main__':
    main()
