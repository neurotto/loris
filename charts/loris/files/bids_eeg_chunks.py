"""Audit or repair EEG registrations of a clone-sourced import without reimporting.

Imports made before on-demand EEG chunks registered a BrainVision recording three
times (.vhdr, .vmrk and .eeg) and recorded no chunk path. The repair keeps the
.vhdr registration, deletes the .vmrk/.eeg ones (database rows only, like
upstream delete_physiological_file.py without --deleteondisk: the three shared
one archive path), records the on-demand chunk path of each recording and turns
useEEGBrowserVisualizationComponents on, in one transaction.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from sqlalchemy import text
from sqlalchemy.orm import Session

import bids_runtime as runtime

PARAMETER = 'electrophysiology_chunked_dataset_path'

# Upstream delete_physiological_file.py, in its order, for one PhysiologicalFileID, except that
# coordinate systems and electrodes another file still uses are kept.
DELETE = [
    """DELETE FROM physiological_event_parameter_category_level WHERE EventParameterID IN (
         SELECT EventParameterID FROM physiological_event_parameter
         JOIN physiological_event_file USING (EventFileID) WHERE PhysiologicalFileID = :id)""",
    """DELETE FROM physiological_event_parameter WHERE EventFileID IN (
         SELECT EventFileID FROM physiological_event_file WHERE PhysiologicalFileID = :id)""",
    'DELETE FROM physiological_channel WHERE PhysiologicalFileID = :id',
    """DELETE FROM physiological_coord_system_point_3d_rel WHERE PhysiologicalCoordSystemID IN (
         SELECT PhysiologicalCoordSystemID FROM physiological_coord_system_electrode_rel
         WHERE PhysiologicalFileID = :id)
       AND PhysiologicalCoordSystemID NOT IN (
         SELECT PhysiologicalCoordSystemID FROM physiological_coord_system_electrode_rel
         WHERE PhysiologicalFileID <> :id)""",
    """DELETE FROM point_3d WHERE Point3DID NOT IN (
         SELECT Point3DID FROM physiological_coord_system_point_3d_rel)
       AND Point3DID NOT IN (SELECT Point3DID FROM physiological_electrode)""",
    """DELETE FROM physiological_coord_system WHERE PhysiologicalCoordSystemID NOT IN (
         SELECT PhysiologicalCoordSystemID FROM physiological_coord_system_point_3d_rel)
       AND PhysiologicalCoordSystemID NOT IN (
         SELECT PhysiologicalCoordSystemID FROM physiological_coord_system_electrode_rel)""",
    'CREATE TEMPORARY TABLE repair_electrodes AS SELECT PhysiologicalElectrodeID '
    'FROM physiological_coord_system_electrode_rel WHERE PhysiologicalFileID = :id '
    'AND PhysiologicalElectrodeID NOT IN (SELECT PhysiologicalElectrodeID '
    'FROM physiological_coord_system_electrode_rel WHERE PhysiologicalFileID <> :id)',
    'DELETE FROM physiological_coord_system_electrode_rel WHERE PhysiologicalFileID = :id',
    'DELETE FROM physiological_electrode WHERE PhysiologicalElectrodeID IN '
    '(SELECT PhysiologicalElectrodeID FROM repair_electrodes)',
    'DROP TEMPORARY TABLE repair_electrodes',
    'DELETE FROM physiological_parameter_file WHERE PhysiologicalFileID = :id',
    'DELETE FROM physiological_archive WHERE PhysiologicalFileID = :id',
    'DELETE FROM physiological_event_archive WHERE PhysiologicalFileID = :id',
    """DELETE FROM physiological_task_event_opt WHERE PhysiologicalTaskEventID IN (
         SELECT PhysiologicalTaskEventID FROM physiological_task_event WHERE PhysiologicalFileID = :id)""",
    """DELETE FROM physiological_task_event_hed_rel WHERE PhysiologicalTaskEventID IN (
         SELECT PhysiologicalTaskEventID FROM physiological_task_event WHERE PhysiologicalFileID = :id)""",
    'DELETE FROM physiological_task_event WHERE PhysiologicalFileID = :id',
    'DELETE FROM physiological_event_file WHERE PhysiologicalFileID = :id',
    'DELETE FROM physiological_file WHERE PhysiologicalFileID = :id',
]


def report(event, **fields):
    print(json.dumps({'event': event, **fields}, sort_keys=True, default=str), flush=True)


def registered(db, view, root):
    """Return (id, path) of the physiological files registered under the view."""
    prefix = str(view.relative_to(root)) + '/'
    rows = db.execute(text('SELECT PhysiologicalFileID, FilePath FROM physiological_file '
                           'ORDER BY PhysiologicalFileID')).all()
    return [(row[0], Path(row[1])) for row in rows if row[1].startswith(prefix)]


def chunk_paths(db, ids):
    rows = db.execute(text(
        'SELECT ppf.PhysiologicalFileID, ppf.Value FROM physiological_parameter_file ppf '
        'JOIN parameter_type pt USING (ParameterTypeID) WHERE pt.Name = :name'), {'name': PARAMETER}).all()
    return {row[0]: row[1] for row in rows if row[0] in ids}


def plan(files, current, root):
    """Return the registrations to delete and the chunk paths to record.

    files are (id, path) pairs; current maps file IDs to recorded chunk paths.
    """
    ephys = runtime.helper('bids_ephys')
    headers = {path.with_suffix('') for _, path in files if path.suffix == '.vhdr'}
    duplicates = [(file_id, path) for file_id, path in files
                  if path.suffix in ('.eeg', '.vmrk') and path.with_suffix('') in headers]
    chunks = {}
    for file_id, path in files:
        if (file_id, path) in duplicates:
            continue
        expected = ephys.lazy_chunk_path(root, path)
        if expected is not None and current.get(file_id) != str(expected):
            chunks[file_id] = str(expected)
    return duplicates, chunks


def require_idle_import(row):
    if row is None or row['mode'] != 'import' or row['status'] not in ('succeeded', 'failed', 'updated'):
        raise ValueError('Dataset must have an idle existing LORIS import')


def parameter_type(db):
    """Return the chunk path parameter type, creating it as upstream get_or_create_parameter_type does."""
    query = text('SELECT ParameterTypeID FROM parameter_type WHERE Name = :name '
                 'ORDER BY ParameterTypeID LIMIT 1')
    found = db.execute(query, {'name': PARAMETER}).scalar_one_or_none()
    if found is not None:
        return found
    db.execute(text("INSERT INTO parameter_type (Name, Type, Description, SourceFrom, Queryable) "
                    "VALUES (:name, 'text', :description, 'physiological_parameter_file', 0)"),
               {'name': PARAMETER, 'description': PARAMETER + ' created by the lib.imaging.parameter Python module'})
    found = db.execute(query, {'name': PARAMETER}).scalar_one()
    db.execute(text('INSERT INTO parameter_type_category_rel (ParameterTypeID, ParameterTypeCategoryID) '
                    'SELECT :type, ParameterTypeCategoryID FROM parameter_type_category '
                    "WHERE Name = 'Electrophysiology Variables'"), {'type': found})
    return found


def visualization_on(db):
    setting = db.execute(text("SELECT ID FROM ConfigSettings WHERE Name = 'useEEGBrowserVisualizationComponents'")
                         ).scalar_one()
    value = db.execute(text('SELECT Value FROM Config WHERE ConfigID = :id'), {'id': setting}).scalar_one_or_none()
    return setting, value == 'true'


def repair(engine, view, root, expected_count, apply=False):
    if not view.is_relative_to(root / 'assembly_bids') or view.is_symlink():
        raise ValueError('Registered import view must be inside assembly_bids')
    with Session(engine) as db:
        files = registered(db, view, root)
        if len(files) != expected_count or not files:
            raise ValueError(f'Expected {expected_count} registered physiological files; found {len(files)}')
        duplicates, chunks = plan(files, chunk_paths(db, {file_id for file_id, _ in files}), root)
        setting, enabled = visualization_on(db)
        report('eeg-plan', registered=len(files), apply=apply, visualization=enabled,
               delete=[{'id': file_id, 'path': str(path)} for file_id, path in duplicates],
               chunks=[{'id': file_id, 'path': path} for file_id, path in sorted(chunks.items())])
        if not apply:
            return
        if not duplicates and not chunks and enabled:
            report('eeg-complete', registered=len(files), deleted=0, recorded=0)
            return

        type_id = parameter_type(db) if chunks else None
        for file_id, path in duplicates:
            for statement in DELETE:
                db.execute(text(statement), {'id': file_id})
            report('eeg-deleted', id=file_id, path=str(path))
        for file_id, path in chunks.items():
            db.execute(text('DELETE FROM physiological_parameter_file '
                            'WHERE PhysiologicalFileID = :id AND ParameterTypeID = :type'),
                       {'id': file_id, 'type': type_id})
            db.execute(text(
                'INSERT INTO physiological_parameter_file (PhysiologicalFileID, ProjectID, ParameterTypeID, Value) '
                'SELECT pf.PhysiologicalFileID, s.ProjectID, :type, :value FROM physiological_file pf '
                'JOIN session s ON s.ID = pf.SessionID WHERE pf.PhysiologicalFileID = :id'),
                {'id': file_id, 'type': type_id, 'value': path})
            report('eeg-chunks', id=file_id, path=path)
        if not enabled:
            db.execute(text('DELETE FROM Config WHERE ConfigID = :id'), {'id': setting})
            db.execute(text("INSERT INTO Config (ConfigID, Value) VALUES (:id, 'true')"), {'id': setting})
        db.commit()

        remaining = registered(db, view, root)
        left, missing = plan(remaining, chunk_paths(db, {file_id for file_id, _ in remaining}), root)
        if left or missing or not visualization_on(db)[1]:
            raise ValueError('EEG registrations still need repair after commit')
        report('eeg-complete', registered=len(remaining), deleted=len(duplicates), recorded=len(chunks))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--expected-count', required=True, type=int)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from lib.config_file import load_config
    from lib.db.connect import get_database_engine

    config = json.loads(args.config.read_text())
    site = config['site']['name']
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        raise ValueError('A full approved commit is required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', args.dataset):
        raise ValueError('Invalid dataset identifier')
    engine = get_database_engine(load_config('config.py').mysql)
    try:
        with runtime.database_lock(engine, 'import') as acquired:
            if not acquired:
                raise ValueError('An import or repair is active; wait before retrying')
            row = runtime.get_dataset(engine, runtime.dataset_id(site, args.dataset))
            require_idle_import(row)
            clone = runtime.ROOT / 'assembly_bids' / site / args.dataset
            imported = subprocess.run(
                ['git', '-C', str(clone), 'rev-parse', 'refs/re-move/imported'],
                check=True, capture_output=True, text=True).stdout.strip()
            if imported != args.commit:
                raise ValueError('Imported marker does not match the approved repair commit')
            report('eeg-import-verified', commit=imported, ledger_status=row['status'])
            repair(engine, Path(row['view_path']), runtime.ROOT, args.expected_count, args.apply)
    finally:
        engine.dispose()


if __name__ == '__main__':
    os.umask(0o022)
    main()
