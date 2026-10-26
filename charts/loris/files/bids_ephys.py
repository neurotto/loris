"""Adapt pinned LORIS-MRI EEG imports for safe reuse of registered file paths.

Upstream commits physiological files before all auxiliary processing finishes,
then skips them on retry. Scoped hooks include those files again and reconcile
their archives. A per-view artifact manifest records owned archive paths so
recovery never treats arbitrary existing files as disposable importer outputs.
Filesystem arguments are absolute pathlib.Path objects.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

from sqlalchemy import select


def hash_file(path):
    """Return a streaming SHA-256 digest for the generated-artifact manifest."""
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)

    return digest.hexdigest()


def remember(view, path, checksum):
    """Atomically record ownership and completion of one generated .tgz artifact.

    A null checksum records creation in progress; a SHA-256 string records the
    completed contents. In-view paths are relative; external output paths use an
    external: prefix. The private manifest is written under view, and archive
    symlinks or a symlinked manifest are rejected.
    """
    marker = view / '.loris-import-artifacts.json'
    if marker.is_symlink():
        raise ValueError('Importer artifact metadata must not be a symlink')

    artifacts = json.loads(marker.read_text()) if marker.exists() else {}
    relative = str(path.relative_to(view)) if path.is_relative_to(view) else 'external:' + str(path)
    if path.suffix != '.tgz' or path.is_symlink():
        raise ValueError('Unexpected electrophysiology artifact path')

    artifacts[relative] = checksum
    descriptor, temporary = tempfile.mkstemp(prefix='.bids-artifact-', dir=view)

    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(artifacts, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, marker)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def resumable_ephys(view):
    """Temporarily install EEG file-reuse and archive-reconciliation hooks.

    view must be the stable no-copy import view. Reused physiological files must
    belong to the current session. Valid database-backed archives are retained;
    incomplete/replaced archives can be rebuilt only when the manifest establishes
    ownership. Filesystem and database effects remain incrementally committed.

    Hooks are process-global and restored on every context exit. Use this only
    inside the version-checked, serialized importer adapter, not concurrently
    with another import in the same Python process.
    """
    from lib.db.models.physio_event_archive import DbPhysioEventArchive
    from lib.db.models.physio_file_archive import DbPhysioFileArchive
    from lib.db.queries.physio_file import try_get_physio_file_with_path
    from loris_bids_importer import archive
    from loris_bids_importer.eeg import main as eeg
    from loris_utils.path import remove_path_extension
    from loris_utils.crypto import compute_file_blake2b_hash

    original_fetch = eeg.Eeg.fetch_and_insert_eeg_files
    original_file_archive = eeg.import_physio_file_archive
    original_event_archive = eeg.import_physio_event_archive

    def fetch(instance, derivatives=False, detect=True):
        """Include registered EEG files so upstream resumes their auxiliary work.

        Preserve upstream fetching for new files and append same-session existing
        files with their sidecar references rather than silently skipping them.
        """
        candidates = instance.bids_layout.get(
            subject=instance.bids_info.subject, session=instance.bids_info.session,
            suffix=instance.bids_info.data_type, extension=['set', 'edf', 'vhdr', 'vmrk', 'eeg', 'bdf'],
            **({'scope': 'derivatives' if derivatives else 'raw'} if detect else {}),
        )

        existing = []
        for candidate in candidates:
            row = try_get_physio_file_with_path(instance.env.db, Path(candidate.path).relative_to(instance.data_dir))

            if row is None:
                continue

            if row.session_id != instance.session.id:
                raise ValueError('Registered electrophysiology file belongs to another session')

            def nearest(extension):
                """Return a matching sidecar's data-root-relative path, or None."""
                found = instance.bids_layout.get_nearest(
                    candidate.path, return_type='tuple', strict=False, extension=extension,
                    all_=False, full_search=False,
                    **({'suffix': instance.bids_info.data_type} if extension == 'json' else {}),
                )

                return Path(found.path).relative_to(instance.data_dir) if found else None

            existing.append({
                'file': row,
                'eegjson_file_path': nearest('json'),
                'fdt_file_path': nearest('fdt'),
                'original_file_data': candidate}
            )

        return (original_fetch(instance, derivatives, detect) or []) + existing

    def archive_checked(env, file, paths, event=False):
        """Verify or rebuild one owned file/event archive for a physiological file.

        Registered archives are verified with upstream BLAKE2b hashes; manifest
        completion uses SHA-256. Journal ownership before creation so interrupted
        output can be retried. Remove stale owned output/rows as needed, but leave
        transaction commit decisions to the surrounding upstream import.
        """
        from lib.config import get_data_dir_path_config

        base = get_data_dir_path_config(env)
        relative = (remove_path_extension(paths[0].relative_to(base)).with_suffix('.tgz') if event
                    else archive.get_archive_path(env, file.path))

        path = base / relative
        model = DbPhysioEventArchive if event else DbPhysioFileArchive
        row = env.db.scalars(select(model).where(model.physio_file_id == file.id, model.path == relative)).one_or_none()
        if row is not None and path.is_file() and compute_file_blake2b_hash(path) == row.blake2b_hash:
            remember(view, path, hash_file(path))
            return

        marker = view / '.loris-import-artifacts.json'
        artifacts = json.loads(marker.read_text()) if marker.exists() else {}
        key = str(path.relative_to(view)) if path.is_relative_to(view) else 'external:' + str(path)
        if path.exists() or path.is_symlink():
            if key not in artifacts or path.is_symlink():
                raise ValueError('Unmanaged archive exists; refusing to overwrite it')

            path.unlink()

        if row is not None:
            env.db.delete(row)
            env.db.flush()

        remember(view, path, None)
        (original_event_archive if event else original_file_archive)(env, file, paths)
        remember(view, path, hash_file(path))

    eeg.Eeg.fetch_and_insert_eeg_files = fetch
    eeg.import_physio_file_archive = archive_checked
    eeg.import_physio_event_archive = lambda env, file, paths: archive_checked(env, file, paths, event=True)

    try:
        yield
    finally:
        eeg.Eeg.fetch_and_insert_eeg_files = original_fetch
        eeg.import_physio_file_archive = original_file_archive
        eeg.import_physio_event_archive = original_event_archive
