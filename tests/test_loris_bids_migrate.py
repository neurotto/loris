import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert, select

from test_loris_bids_runtime import configuration, runtime


sys.path.insert(0, str(Path(runtime.__file__).parent))
spec = importlib.util.spec_from_file_location('bids_migrate', Path(runtime.__file__).with_name('bids_migrate.py'))
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / 'assembly_bids/nandou'
        self.old.mkdir(parents=True)
        (self.old / 'dataset_description.json').write_text('{"Name":"Dataset","BIDSVersion":"1.8.0"}')
        (self.old / 'participants.tsv').write_text('participant_id\nsub-example\n')
        (self.old / 'sub-example').mkdir()
        (self.old / 'sub-example/file.nii.gz').write_bytes(b'fixture')
        self.view = self.root / 'assembly_bids/import-legacy'
        runtime.helper('bids-view').prepare_view(self.old, self.view, 'Project', 'scilus', 'Old cohort')
        root_patch = patch.object(runtime, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        metadata = MetaData()
        self.schema = {
            'psc': Table('psc', metadata, Column('CenterID', Integer, primary_key=True), Column('Name', String)),
            'Project': Table('Project', metadata, Column('ProjectID', Integer, primary_key=True), Column('Name', String)),
            'cohort': Table('cohort', metadata, Column('CohortID', Integer, primary_key=True), Column('title', String)),
            'candidate': Table('candidate', metadata, Column('ID', Integer, primary_key=True),
                               Column('CandID', Integer),
                               Column('RegistrationCenterID', Integer), Column('RegistrationProjectID', Integer)),
            'session': Table('session', metadata, Column('ID', Integer, primary_key=True), Column('CandidateID', Integer),
                             Column('CenterID', Integer), Column('ProjectID', Integer), Column('CohortID', Integer),
                             Column('Visit_label', String)),
            'files': Table('files', metadata, Column('FileID', Integer, primary_key=True),
                           Column('SessionID', Integer), Column('File', String)),
        }
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(insert(self.schema['psc']), [{'CenterID': 1, 'Name': 'scilus'},
                                                            {'CenterID': 2, 'Name': 'nandou'}])
            connection.execute(insert(self.schema['Project']), {'ProjectID': 1, 'Name': 'Project'})
            connection.execute(insert(self.schema['cohort']), [{'CohortID': 1, 'title': 'Old cohort'},
                                                              {'CohortID': 2, 'title': 'Dataset'}])
            connection.execute(insert(self.schema['candidate']), [
                {'ID': 1, 'CandID': 1001, 'RegistrationCenterID': 1, 'RegistrationProjectID': 1},
                {'ID': 2, 'CandID': 1002, 'RegistrationCenterID': 1, 'RegistrationProjectID': 1},
            ])
            connection.execute(insert(self.schema['session']), [
                {'ID': 1, 'CandidateID': 1, 'CenterID': 1, 'ProjectID': 1, 'CohortID': 1, 'Visit_label': '000'},
                {'ID': 2, 'CandidateID': 2, 'CenterID': 1, 'ProjectID': 1, 'CohortID': 1, 'Visit_label': '000'},
            ])
            connection.execute(insert(self.schema['files']), [
                {'FileID': 1, 'SessionID': 1, 'File': 'assembly_bids/import-legacy/sub-example/file.nii.gz'},
                {'FileID': 2, 'SessionID': 2, 'File': 'assembly_bids/unrelated/file.nii.gz'},
            ])
        self.config = configuration()

    def plan(self):
        return migration.plan_migration(self.engine, self.config, 'dataset', self.view,
                                         self.old, 'scilus', 'Old cohort')

    def move_source(self):
        temporary = self.root / 'preserved-source'
        self.old.rename(temporary)
        self.old.mkdir()
        temporary.rename(self.old / 'dataset')

    def test_dry_run_does_not_write_files_or_records(self):
        before = (self.view / 'participants.tsv').read_bytes()
        journal = self.plan()
        self.assertEqual(len(journal['selection']['candidates']), 1)
        self.assertEqual(journal['phase'], 'prepared')
        self.assertEqual((self.view / 'participants.tsv').read_bytes(), before)
        self.assertFalse((self.root / 'journal.json').exists())
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(self.schema['session'].c.CenterID).where(
                self.schema['session'].c.ID == 1)), 1)

    def test_shared_candidates_are_rejected(self):
        with self.engine.begin() as connection:
            connection.execute(insert(self.schema['session']), {
                'ID': 3, 'CandidateID': 1, 'CenterID': 1, 'ProjectID': 1, 'CohortID': 1, 'Visit_label': '001',
            })
        with self.assertRaisesRegex(ValueError, 'outside the dataset'):
            self.plan()

    def test_sessions_with_unrelated_files_are_rejected(self):
        with self.engine.begin() as connection:
            connection.execute(insert(self.schema['files']), {
                'FileID': 3, 'SessionID': 1, 'File': 'assembly_bids/other/file.nii.gz',
            })
        with self.assertRaisesRegex(ValueError, 'another dataset'):
            self.plan()

    def test_retarget_and_database_remap_are_repeatable_and_preserve_unrelated_records(self):
        journal = self.plan()
        self.move_source()
        for _ in range(2):
            migration.relink(journal)
            migration.remap_database(self.engine, journal)
        self.assertEqual((self.view / 'sub-example/file.nii.gz').read_bytes(), b'fixture')
        self.assertIn('\tnandou\tDataset', (self.view / 'participants.tsv').read_text())
        runtime.helper('bids-view').prepare_view(self.old / 'dataset', self.view,
                                                'Project', 'nandou', 'Dataset', reuse=True)
        with self.engine.connect() as connection:
            rows = connection.execute(select(self.schema['session'].c.CenterID).order_by(
                self.schema['session'].c.ID)).scalars().all()
            self.assertEqual(rows, [2, 1])

    def test_changed_destination_bytes_are_rejected_before_relink(self):
        journal = self.plan()
        self.move_source()
        (self.old / 'dataset/sub-example/file.nii.gz').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'differs'):
            migration.relink(journal)

    def test_pre_adoption_rollback_restores_originals_after_old_mount_is_restored(self):
        journal = self.plan()
        self.move_source()
        migration.relink(journal)
        migration.remap_database(self.engine, journal)
        source = self.old / 'dataset'
        temporary = self.root / 'old-restore'
        source.rename(temporary)
        self.old.rmdir()
        temporary.rename(self.old)
        migration.relink(journal, reverse=True)
        migration.remap_database(self.engine, journal, reverse=True)
        self.assertEqual((self.view / 'sub-example/file.nii.gz').read_bytes(), b'fixture')
        self.assertIn('\tscilus\tOld cohort', (self.view / 'participants.tsv').read_text())

    def test_journal_is_private_and_atomic(self):
        path = self.root / 'journal.json'
        journal = self.plan()
        migration.save_journal(path, journal)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), journal)
        self.assertEqual(list(self.root.glob('.bids-migration-*')), [])

    def test_new_unrelated_session_after_preparation_blocks_apply(self):
        journal = self.plan()
        with self.engine.begin() as connection:
            connection.execute(insert(self.schema['session']), {
                'ID': 3, 'CandidateID': 1, 'CenterID': 1, 'ProjectID': 1, 'CohortID': 1, 'Visit_label': '001',
            })
        with self.assertRaisesRegex(ValueError, 'unrelated sessions'):
            migration.remap_database(self.engine, journal)


if __name__ == '__main__':
    unittest.main()
