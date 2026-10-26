"""Migrate one verified legacy no-copy import without changing registered paths.

Preparation is read-only unless explicitly asked to save a private recovery
journal. Operators separately approve and roll out the new NFS export layout.
Apply then retargets generated view links, relinks selected registration records,
and adopts completion into the import ledger. It never moves remote source data.

Filesystem changes and database commits cannot form one transaction. The journal
supports replay and pre-adoption rollback; after adoption, recovery requires the
coordinated database backup and preserved source/path manifests. Journals contain
sensitive participant metadata and must not be committed or logged in full.
Public filesystem arguments are pathlib.Path objects; persisted journal paths
are JSON strings reconstructed as Paths when replayed.
"""

import argparse
import base64
import csv
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

from sqlalchemy import MetaData, Table, inspect, select, update

import bids_runtime as runtime


def save_journal(path, journal):
    """Atomically save a JSON recovery journal using a private mode-0600 file.

    Create parent directories if needed, fsync file contents before replacement,
    and remove the temporary file on failure. Existing journal contents are
    replaced; the CLI separately prevents accidental replacement during prepare.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.bids-migration-', dir=path.parent)

    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(journal, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def content_hash(path):
    """Return a streaming SHA-256 digest, following a file symlink if present."""
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)

    return digest.hexdigest()


def tables(engine):
    """Reflect required LORIS tables and the optional physiological-file table.

    Reflection reads the deployed schema; it does not create or alter tables.
    """
    metadata = MetaData()
    names = ['psc', 'Project', 'cohort', 'candidate', 'session', 'files']
    if inspect(engine).has_table('physiological_file'):
        names.append('physiological_file')

    return {name: Table(name, metadata, autoload_with=engine) for name in names}


def physiological_scope(connection, schema, session_ids, prefix):
    """Return physiological file IDs confined to the selected legacy view.

    Reject sessions containing physiological paths outside prefix. An absent
    physiological_file table yields an empty scope for schemas without it.
    """
    table = schema.get('physiological_file')
    if table is None:
        return []

    rows = connection.execute(select(table.c.PhysiologicalFileID, table.c.FilePath).where(
        table.c.SessionID.in_(session_ids),
    )).all()
    if any(not row.FilePath.startswith(prefix) for row in rows):
        raise ValueError('Selected sessions contain physiological data outside the legacy view')

    return sorted(row.PhysiologicalFileID for row in rows)


def identity(connection, table, name_column, name, id_column):
    """Resolve exactly one existing named record to its ID, or raise ValueError."""
    rows = connection.execute(select(table.c[id_column]).where(table.c[name_column] == name)).scalars().all()
    if len(rows) != 1:
        raise ValueError('Expected exactly one existing ' + table.name + ' identity for ' + name)

    return rows[0]


def inventory(engine, view, old_site, old_cohort, project):
    """Return a read-only, unambiguous record inventory rooted in a legacy view.

    Registered MRI files determine session scope; candidates are selected through
    session.CandidateID -> candidate.ID, not the external candidate.CandID.
    Require matching legacy identities and reject shared candidates/sessions or
    out-of-view MRI/physiological files. Return original registration columns and
    file IDs for later drift checks and rollback, without modifying any records.
    """
    schema = tables(engine)
    files, sessions, candidates = (schema[name] for name in ('files', 'session', 'candidate'))
    prefix = str(view.relative_to(runtime.ROOT)) + '/'

    with engine.connect() as connection:
        site_id = identity(connection, schema['psc'], 'Name', old_site, 'CenterID')
        project_id = identity(connection, schema['Project'], 'Name', project, 'ProjectID')
        cohort_id = identity(connection, schema['cohort'], 'title', old_cohort, 'CohortID')

        selected_files = connection.execute(select(files.c.FileID, files.c.SessionID).where(
            files.c.File.startswith(prefix, autoescape=True),
        )).all()
        if not selected_files:
            raise ValueError('No registered files under the selected legacy import view')

        session_ids = {row.SessionID for row in selected_files}
        selected_sessions = connection.execute(select(
            sessions.c.ID, sessions.c.CandidateID, sessions.c.CenterID, sessions.c.ProjectID,
            sessions.c.CohortID, sessions.c.Visit_label,
        ).where(sessions.c.ID.in_(session_ids))).mappings().all()
        if len(selected_sessions) != len(session_ids):
            raise ValueError('Files reference missing sessions')

        for row in selected_sessions:
            if (row['CenterID'], row['ProjectID'], row['CohortID']) != (site_id, project_id, cohort_id):
                raise ValueError('Selected sessions do not exclusively match the legacy site/project/cohort')

        outside = connection.execute(select(files.c.FileID).where(
            files.c.SessionID.in_(session_ids),
            ~files.c.File.startswith(prefix, autoescape=True),
        )).first()
        if outside is not None:
            raise ValueError('Selected sessions contain files from another dataset; refusing ambiguous migration')

        physio_ids = physiological_scope(connection, schema, session_ids, prefix)
        candidate_ids = {row['CandidateID'] for row in selected_sessions}

        shared = connection.execute(select(sessions.c.ID).where(
            sessions.c.CandidateID.in_(candidate_ids), ~sessions.c.ID.in_(session_ids),
        )).first()
        if shared is not None:
            raise ValueError('Selected candidates have sessions outside the dataset; explicit manual review required')
        
        selected_candidates = connection.execute(select(
            candidates.c.ID, candidates.c.RegistrationCenterID, candidates.c.RegistrationProjectID,
        ).where(candidates.c.ID.in_(candidate_ids))).mappings().all()
        if len(selected_candidates) != len(candidate_ids) or any(
            (row['RegistrationCenterID'], row['RegistrationProjectID']) != (site_id, project_id)
            for row in selected_candidates
        ):
            raise ValueError('Candidate registration identity does not match the selected legacy dataset')

    return {
        'candidates': [dict(row) for row in selected_candidates],
        'sessions': [dict(row) for row in selected_sessions],
        'file_ids': sorted(row.FileID for row in selected_files),
        'physio_file_ids': physio_ids
    }


def plan_migration(engine, config, folder, view, old_source, old_site, old_cohort):
    """Build an in-memory recovery journal without changing files or records.

    config is the destination site's generated runtime configuration; folder is
    its future immediate dataset directory. view is an existing generated view and
    old_source is its approved absolute source root. Capture original links,
    content hashes, scoped record identities, and before/after participant bytes.
    The future export need not exist yet. Ambiguous scopes or unmanaged view
    files raise ValueError; callers must protect the sensitive returned journal.
    """
    if view.is_symlink() or not view.is_dir() or not view.is_relative_to(runtime.ROOT / 'assembly_bids'):
        raise ValueError('Select an existing generated import view under assembly_bids')

    if folder in ('.', '..', '') or '/' in folder:
        raise ValueError('Destination dataset folder must be an immediate child')

    site = config['site']['name']
    new_source = runtime.ROOT / 'assembly_bids' / site / folder
    description = view / 'dataset_description.json'
    metadata_name = runtime.read_name(view)

    settings = runtime.resolve_settings(config, folder, metadata_name)
    if settings['mode'] != 'import':
        raise ValueError('Migration adoption requires mode import')

    selected = inventory(engine, view, old_site, old_cohort, settings['project'])
    links = []
    for path in sorted(view.rglob('*')):
        if path.is_symlink():
            target = Path(os.readlink(path))
            if not target.is_absolute() or not target.is_relative_to(old_source):
                raise ValueError('Import-view symlink is outside the explicitly selected old source')

            new_target = new_source / target.relative_to(old_source)
            if not target.resolve(strict=True).is_relative_to(old_source.resolve()):
                raise ValueError('Legacy source link escapes approved source')

            links.append({'path': str(path), 'before': str(target), 'after': str(new_target),
                          'sha256': content_hash(path)})
        elif path.is_file() and path.name != 'participants.tsv':
            raise ValueError('Legacy view contains unexpected regular files; refusing a source directory migration')

    if not links or not description.is_symlink():
        raise ValueError('Legacy dataset must be a generated symlink import view')

    participants = view / 'participants.tsv'
    if participants.is_symlink():
        raise ValueError('Generated participants.tsv must be a regular file')

    before = participants.read_bytes()
    reader = csv.DictReader(io.StringIO(before.decode()), delimiter='\t')
    rows = list(reader)
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError('Malformed legacy participant metadata')

    for row in rows:
        if (row.get('project'), row.get('site'), row.get('cohort')) != (settings['project'], old_site, old_cohort):
            raise ValueError('Legacy participant registration metadata does not match migration selection')

        row.update(site=site, cohort=settings['cohort'])

    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames, delimiter='\t')
    writer.writeheader()
    writer.writerows(rows)

    return {
        'version': 1,
        'phase': 'prepared',
        'site': site,
        'folder': folder,
        'view': str(view),
        'old_source': str(old_source),
        'new_source': str(new_source),
        'old_site': old_site,
        'old_cohort': old_cohort,
        'metadata_name': metadata_name,
        'settings': settings,
        'selection': selected,
        'links': links,
        'participants': {
            'path': str(participants),
            'before': base64.b64encode(before).decode(),
            'after': base64.b64encode(output.getvalue().encode()).decode()
        },
        'exporter_change': {
            'dataPath': 'Set the approved parent of the existing dataset directory; review all exposed siblings',
            'required_layout': 'shared-root/' + folder + '/dataset_description.json',
            'note': 'Tool does not change NFS exports or move source files; preserve registered view paths',
        },
    }


def atomic_bytes(path, content):
    """Replace an existing regular file atomically while preserving permission bits.

    Flush replacement contents to disk before renaming and clean up on failure.
    Callers are responsible for validating path and the expected prior contents.
    """
    descriptor, temporary = tempfile.mkstemp(prefix='.bids-relink-', dir=path.parent)

    try:
        os.fchmod(descriptor, path.stat().st_mode & 0o777)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def relink(journal, reverse=False):
    """Apply or reverse journaled source links and generated participant metadata.

    Validate every link, destination hash, and participant snapshot before writes.
    Individual replacements are atomic, but the whole view is not; replay accepts
    either journaled state to resume interrupted work. Reverse requires the old
    source mount to be restored first. Database records are handled separately.
    """
    for link in journal['links']:
        path = Path(link['path'])
        before, after = (link['after'], link['before']) if reverse else (link['before'], link['after'])
        if not path.is_symlink() or os.readlink(path) not in (before, after):
            raise ValueError('View symlink changed outside migration: ' + str(path))

        if content_hash(Path(after)) != link['sha256']:
            raise ValueError('Destination file differs from preserved legacy content: ' + str(path))

    participant = journal['participants']
    before, after = (participant['after'], participant['before']) if reverse else (
        participant['before'], participant['after'])

    path = Path(participant['path'])
    if path.is_symlink() or path.read_bytes() not in (base64.b64decode(before), base64.b64decode(after)):
        raise ValueError('Participant metadata changed outside migration')

    for link in journal['links']:
        path = Path(link['path'])
        target = link['before'] if reverse else link['after']

        temporary = path.with_name('.' + path.name + '.loris-migrate')
        if temporary.exists() or temporary.is_symlink():
            if not temporary.is_symlink() or os.readlink(temporary) != target:
                raise ValueError('Unexpected migration temporary path: ' + str(temporary))
        else:
            temporary.symlink_to(target)

        temporary.replace(path)

    atomic_bytes(Path(participant['path']), base64.b64decode(after))


def remap_database(engine, journal, reverse=False):
    """Relink only inventoried candidate/session registrations in one transaction.

    Recheck file scopes and unrelated sessions to detect drift since preparation.
    Each registration must match its original or intended destination state,
    allowing idempotent replay but rejecting third-party changes. reverse restores
    original registration values; registered file paths and unrelated records
    are left unchanged. Destination metadata must already exist.
    """
    schema = tables(engine)
    settings = journal['settings']
    with engine.begin() as connection:
        session_ids = {row['ID'] for row in journal['selection']['sessions']}
        candidate_ids = {row['ID'] for row in journal['selection']['candidates']}

        files = schema['files']
        current_files = connection.execute(select(files.c.FileID, files.c.File).where(
            files.c.SessionID.in_(session_ids),
        )).all()
        prefix = str(Path(journal['view']).relative_to(runtime.ROOT)) + '/'
        if sorted(row.FileID for row in current_files) != journal['selection']['file_ids'] or any(
            not row.File.startswith(prefix) for row in current_files
        ):
            raise ValueError('Registered dataset files changed since migration preparation')

        if physiological_scope(connection, schema, session_ids, prefix) != journal['selection']['physio_file_ids']:
            raise ValueError('Physiological registrations changed since migration preparation')
        
        sessions = schema['session']

        shared = connection.execute(select(sessions.c.ID).where(
            sessions.c.CandidateID.in_(candidate_ids), ~sessions.c.ID.in_(session_ids),
        )).first()
        if shared is not None:
            raise ValueError('Candidate gained unrelated sessions after preparation')

        target_site = identity(connection, schema['psc'], 'Name', journal['site'], 'CenterID')
        target_cohort = identity(connection, schema['cohort'], 'title', settings['cohort'], 'CohortID')
        target_project = identity(connection, schema['Project'], 'Name', settings['project'], 'ProjectID')
        for name, primary, originals, destination in (
            ('candidate', 'ID', journal['selection']['candidates'],
             {'RegistrationCenterID': target_site, 'RegistrationProjectID': target_project}),
            ('session', 'ID', journal['selection']['sessions'],
             {'CenterID': target_site, 'ProjectID': target_project, 'CohortID': target_cohort}),
        ):
            table = schema[name]
            for original in originals:
                old = {key: original[key] for key in destination}
                current = connection.execute(select(*(table.c[key] for key in destination)).where(
                    table.c[primary] == original[primary],
                )).mappings().one()
                if dict(current) not in (old, destination):
                    raise ValueError('Registration changed outside migration; refusing overwrite')

                connection.execute(update(table).where(table.c[primary] == original[primary]).values(
                    **(old if reverse else destination)))


def execute(engine, journal, journal_path, backup, reverse=False):
    """Apply/resume a prepared migration, or roll it back before ledger adoption.

    Hold global import and site discovery locks and refuse outstanding attempts.
    Apply validates a read-only destination, configures metadata, journals phases,
    retargets the view, remaps records, verifies view reuse, and adopts completion.
    Update journal in place and persist checkpoints to journal_path.

    backup must be a nonempty file; its consistency/restorability remains an
    operator responsibility. Errors propagate with journaled recovery inputs
    retained. Filesystem and database effects are not globally atomic, and newly
    created metadata is not removed by rollback. Post-adoption rollback is refused
    in favor of coordinated backup/path recovery.
    """
    if not backup.is_file() or backup.stat().st_size == 0:
        raise ValueError('A nonempty consistent database backup file is required')

    if journal['version'] != 1:
        raise ValueError('Unsupported migration journal version')

    with ExitStack() as stack:
        for name in ('import', 'discovery-' + journal['site']):
            if not stack.enter_context(runtime.database_lock(engine, name)):
                raise ValueError('Import/discovery is active; drain workers and suspend discovery first')

        with engine.connect() as connection:
            if connection.dialect.has_table(connection, runtime.DATASETS.name):
                active = connection.execute(select(runtime.DATASETS.c.id).where(
                    runtime.DATASETS.c.status.in_(('reserved', 'running')),
                )).first()
                if active is not None:
                    raise ValueError('Reserved or running import attempts must be drained before migration')

        if reverse and journal['phase'] == 'adopted':
            raise ValueError('Migration already adopted; restore the coordinated database backup and path manifest')

        if not reverse:
            source = Path(journal['new_source'])
            if not os.statvfs(source).f_flag & os.ST_RDONLY:
                raise ValueError('Destination source must be mounted read-only')

            runtime.helper('bids-view').inspect_source(source)
            if runtime.read_name(source) != journal['metadata_name']:
                raise ValueError('Destination dataset metadata differs')

            from sqlalchemy.orm import Session
            setup = runtime.helper('bids-setup')
            with Session(engine) as session, session.begin():
                setup.configure(
                    session, journal['settings']['project'],
                    journal['settings']['projectAlias'],
                    journal['site'],
                    journal['settings']['siteAlias'],
                    journal['settings']['cohort'],
                    journal['settings'].get('recruitmentTarget'),
                    {row['Visit_label'] for row in journal['selection']['sessions']},
                    journal['settings']['defaultVisit'],
                    os.environ.get('LORIS_ADMIN_USER', 'admin'),
                )

        journal['phase'] = 'rolling-back' if reverse else 'applying'
        save_journal(journal_path, journal)
        relink(journal, reverse)
        remap_database(engine, journal, reverse)
        journal['phase'] = 'rolled-back' if reverse else 'verified'
        save_journal(journal_path, journal)

        if not reverse:
            runtime.helper('bids-view').prepare_view(
                Path(journal['new_source']), Path(journal['view']), journal['settings']['project'],
                journal['site'], journal['settings']['cohort'], reuse=True)
            runtime.adopt_dataset(engine, journal['site'], journal['folder'], journal['settings'],
                                  journal['metadata_name'], journal['view'], locked=True)
            journal['phase'] = 'adopted'
            save_journal(journal_path, journal)


def main():
    """Run read-only planning by default, with explicit prepare/apply/rollback modes.

    --prepare saves a new journal; --apply resumes it; --rollback restores its
    pre-adoption scope. Only a count/path-guidance summary is printed, never the
    embedded participant backup or full record inventory.
    """
    parser = argparse.ArgumentParser(description='Dry-run-first migration of one legacy generated BIDS import view')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--folder')
    parser.add_argument('--view', type=Path)
    parser.add_argument('--old-source', type=Path)
    parser.add_argument('--old-site')
    parser.add_argument('--old-cohort')
    parser.add_argument('--journal', required=True, type=Path)
    parser.add_argument('--backup', type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--prepare', action='store_true')
    actions.add_argument('--apply', action='store_true')
    actions.add_argument('--rollback', action='store_true')
    args = parser.parse_args()

    from lib.config_file import load_config
    from lib.db.connect import get_database_engine

    engine = get_database_engine(load_config('config.py').mysql)

    if args.apply or args.rollback:
        if args.backup is None:
            parser.error('--apply/--rollback require --backup')

        journal = json.loads(args.journal.read_text())
        execute(engine, journal, args.journal, args.backup, args.rollback)
    else:
        if any(value is None for value in (args.config, args.folder, args.view, args.old_source,
                                           args.old_site, args.old_cohort)):
            parser.error('dry-run/--prepare require --config, --folder, --view, --old-source, --old-site, --old-cohort')

        journal = plan_migration(engine, json.loads(args.config.read_text()), args.folder,
                                 args.view, args.old_source, args.old_site, args.old_cohort)

        if args.prepare:
            if args.journal.exists():
                raise ValueError('Journal already exists; resume it rather than overwrite recovery inputs')

            save_journal(args.journal, journal)

    print(json.dumps({'phase': journal['phase'], 'site': journal['site'], 'folder': journal['folder'],
                      'sessions': len(journal['selection']['sessions']),
                      'candidates': len(journal['selection']['candidates']),
                      'links': len(journal['links']), 'exporter_change': journal['exporter_change']}))


if __name__ == '__main__':
    main()
