"""Adapt pinned LORIS-MRI EEG imports for safe reuse of registered file paths.

Upstream commits physiological files before all auxiliary processing finishes,
then skips them on retry. Scoped hooks include those files again and reconcile
their archives. A per-view artifact manifest records owned archive paths so
recovery never treats arbitrary existing files as disposable importer outputs.
Filesystem arguments are absolute pathlib.Path objects.

On-demand EEG viewing registers a BrainVision recording once, by its .vhdr
header (upstream registers the .vhdr, .vmrk and .eeg as three recordings).
Provider-backed recordings use a stable path derived from the logical source
path. Local BrainVision and BDF recordings use the upstream chunk writer;
other local formats retain upstream chunk generation.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from sqlalchemy import select, text


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


CONTENT_PROVIDER_CHUNKS_DIR = 'content-provider-chunks'
CHUNKED_EXTENSIONS = ('.edf', '.set', '.vhdr', '.bdf')
NAME_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$')


def brainvision_primary(files):
    """Drop BrainVision .eeg/.vmrk files whose .vhdr header is also listed.

    files are pybids BIDSFile-like objects with a path attribute. The header
    names its data and marker files, so it alone stands for the recording.
    """
    headers = {Path(file.path).with_suffix('') for file in files if Path(file.path).suffix == '.vhdr'}
    return [file for file in files
            if Path(file.path).suffix not in ('.eeg', '.vmrk') or Path(file.path).with_suffix('') not in headers]


class BrainVisionLayout:
    """A BIDS layout whose EEG file queries list BrainVision recordings once."""

    def __init__(self, layout):
        self._layout = layout

    def get(self, *args, **kwargs):
        files = self._layout.get(*args, **kwargs)
        extension = kwargs.get('extension')
        if isinstance(extension, (list, tuple)) and 'vhdr' in extension:
            return brainvision_primary(files)

        return files

    def __getattr__(self, name):
        return getattr(self._layout, name)


def lazy_chunk_path(data_dir, path):
    """Return the on-demand chunk directory for a managed recording, else None.

    path is the recording's data-root-relative FilePath. Its view links resolve
    to a file below <data_dir>/assembly_bids/<site>/<dataset>. The chunk path
    hashes the logical source path and contains no provider-specific identifier.
    """
    if path.suffix not in CHUNKED_EXTENSIONS or not os.getenv('LORIS_CONTENT_PROVIDER_URL'):
        return None

    root = (data_dir / 'assembly_bids').resolve()
    target = data_dir / path
    for _ in range(8):
        resource = provider_resource_path(root, target)
        if resource is not None:
            site, dataset, relative_path = resource
            digest = hashlib.sha256(relative_path.encode()).hexdigest()
            return Path(CONTENT_PROVIDER_CHUNKS_DIR, site, dataset, digest + '.chunks')

        if not target.is_symlink():
            break

        target = Path(os.path.normpath(target.parent.resolve() / os.readlink(target)))

    return None


def provider_resource_path(root, target):
    """Return a provider-neutral site/dataset/path identity for a source target."""
    if not target.is_relative_to(root):
        return None

    parts = target.relative_to(root).parts
    try:
        parts[1].encode('utf-8')
    except (IndexError, UnicodeEncodeError):
        return None

    if (
        len(parts) < 3
        or not NAME_RE.fullmatch(parts[0])
        or not parts[1]
        or parts[1].startswith('.')
        or re.fullmatch(r'import-.+', parts[0])
        or any(part in ('', '.', '..', '.git') for part in parts[2:])
    ):
        return None

    return parts[0], parts[1], Path(*parts[2:]).as_posix()


def has_chunk_parameter(db, file_id):
    """Return whether a physiological file has a chunk path, matching it by name like LORIS.

    Upstream try_get_physio_file_parameter_with_file_id_name looks the type up with source
    'physiological_file' while insert_physio_file_parameter creates it with
    'physiological_parameter_file', so it never finds what the importer recorded.
    """
    return db.execute(text(
        'SELECT 1 FROM physiological_parameter_file ppf JOIN parameter_type pt USING (ParameterTypeID) '
        "WHERE ppf.PhysiologicalFileID = :id AND pt.Name = 'electrophysiology_chunked_dataset_path' LIMIT 1"
    ), {'id': file_id}).first() is not None


@contextmanager
def local_chunks(env, file):
    """Generate BrainVision/BDF chunks using the pinned upstream chunk writer."""
    import mne
    from lib.config import get_data_dir_path_config
    from lib.physio.chunking import get_dataset_chunks_dir_path
    from loris_ephys_chunker.chunking import chunk_dir_path, write_chunk_directory

    root = get_data_dir_path_config(env)
    source = root / file.path
    destination = get_dataset_chunks_dir_path(env, file)
    generation = Path(tempfile.mkdtemp(prefix='.loris-chunks-', dir=destination))
    committed = False
    reader = {'.vhdr': mne.io.read_raw_brainvision, '.bdf': mne.io.read_raw_bdf}[source.suffix]
    try:
        with reader(source, preload=False) as recording:
            names = recording.ch_names
        for index, name in enumerate(names):
            write_chunk_directory(
                path=source, chunk_size=5000, destination=generation,
                loader=lambda path, name=name: reader(path, preload=False).pick([name]),
                from_channel_index=index, from_channel_name=name, channel_count=1,
            )
        chunks = chunk_dir_path(source, destination=generation)
        index_path = chunks / 'index.json'
        metadata = json.loads(index_path.read_text())
        metadata['shapes'] = [[len(names), *shape[1:]] for shape in metadata['shapes']]
        index_path.write_text(json.dumps(metadata, indent=2))
        yield chunks.relative_to(root)
        committed = True
    finally:
        if not committed:
            shutil.rmtree(generation)


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
    original_chunks = eeg.create_physio_channels_chunks

    def fetch(instance, derivatives=False, detect=True):
        """Include registered EEG files so upstream resumes their auxiliary work.

        Preserve upstream fetching for new files and append same-session existing
        files with their sidecar references rather than silently skipping them.
        BrainVision recordings are fetched by their .vhdr header only.
        """
        candidates = brainvision_primary(instance.bids_layout.get(
            subject=instance.bids_info.subject, session=instance.bids_info.session,
            suffix=instance.bids_info.data_type, extension=['set', 'edf', 'vhdr', 'vmrk', 'eeg', 'bdf'],
            **({'scope': 'derivatives' if derivatives else 'raw'} if detect else {}),
        ))

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

        layout = instance.bids_layout
        instance.bids_layout = BrainVisionLayout(layout)
        try:
            fetched = original_fetch(instance, derivatives, detect) or []
        finally:
            instance.bids_layout = layout

        return fetched + existing

    def chunks(env, file):
        """Register provider chunks or generate supported local recording chunks."""
        from lib.config import get_data_dir_path_config
        from lib.physio.parameters import insert_physio_file_parameter

        if has_chunk_parameter(env.db, file.id):
            return

        path = lazy_chunk_path(get_data_dir_path_config(env), Path(file.path))
        if path is None:
            if Path(file.path).suffix not in ('.vhdr', '.bdf'):
                return original_chunks(env, file)
            with local_chunks(env, file) as path:
                insert_physio_file_parameter(env, file, 'electrophysiology_chunked_dataset_path', path)
                env.db.commit()
            return

        insert_physio_file_parameter(env, file, 'electrophysiology_chunked_dataset_path', path)
        env.db.commit()

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
    eeg.create_physio_channels_chunks = chunks

    try:
        yield
    finally:
        eeg.Eeg.fetch_and_insert_eeg_files = original_fetch
        eeg.import_physio_file_archive = original_file_archive
        eeg.import_physio_event_archive = original_event_archive
        eeg.create_physio_channels_chunks = original_chunks
