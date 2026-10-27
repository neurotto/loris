import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text

from test_loris_bids_runtime import configuration, runtime


@unittest.skipUnless(os.environ.get('BIDS_TEST_DATABASE_URL'), 'isolated LORIS database not provided')
class ImporterIntegrationTests(unittest.TestCase):
    def setUp(self):
        import nibabel as nib
        import numpy as np
        self.engine = create_engine(os.environ['BIDS_TEST_DATABASE_URL'])
        self.addCleanup(self.engine.dispose)
        self.profile = tempfile.TemporaryDirectory()
        self.addCleanup(self.profile.cleanup)
        Path(self.profile.name, 'config.py').write_text(
            'import os\nfrom lib.config_file import DatabaseConfig\n'
            "mysql = DatabaseConfig(host=os.environ['LORIS_DB_HOST'], username=os.environ['LORIS_DB_USER'], "
            "password=os.environ['LORIS_DB_PASSWORD'], database=os.environ['LORIS_DB_NAME'])\n")
        environment = patch.dict(os.environ, {'LORIS_CONFIG': self.profile.name})
        environment.start()
        self.addCleanup(environment.stop)
        suffix = str(list(type(self).__dict__).index(self._testMethodName))
        self.site = 'fixture' + suffix
        self.source = runtime.ROOT / 'assembly_bids' / self.site / 'dataset'
        self.source.mkdir(parents=True)
        self.subject = 'example' + suffix
        self.name = 'Synthetic ' + suffix
        (self.source / 'dataset_description.json').write_text(json.dumps({
            'Name': self.name, 'BIDSVersion': '1.8.0', 'DatasetType': 'raw',
        }))
        (self.source / 'participants.tsv').write_text('participant_id\tsex\nsub-' + self.subject + '\tM\n')
        anat = self.source / ('sub-' + self.subject) / 'anat'
        anat.mkdir(parents=True)
        self.image = anat / ('sub-' + self.subject + '_T1w.nii.gz')
        nib.save(nib.Nifti1Image(np.random.default_rng(int(suffix)).random(
            (16, 16, 16), dtype=np.float32), np.eye(4)), self.image)
        config = configuration()
        config['site']['siteAlias'] = 'T' + suffix
        self.settings = runtime.resolve_settings(config, 'dataset', self.name)
        self.settings['project'] = 'Fixture project ' + suffix
        self.settings['projectAlias'] = 'P' + suffix
        setup = runtime.helper('bids-setup')
        setup.activate_modules = lambda *args: None
        original_helper = runtime.helper
        with patch.object(runtime, 'helper', side_effect=lambda name: setup if name == 'bids-setup' else original_helper(name)):
            runtime.setup_dataset(self.engine, self.source, self.site, self.settings)
        self.view = runtime.ROOT / 'assembly_bids' / ('import-fixture-' + suffix)
        runtime.helper('bids-view').prepare_view(
            self.source, self.view, self.settings['project'], self.site, self.name)

    def counts(self):
        prefix = str(self.view.relative_to(runtime.ROOT)) + '/%'
        with self.engine.connect() as connection:
            files = connection.scalar(text('SELECT COUNT(*) FROM files WHERE File LIKE :prefix'), {'prefix': prefix})
            candidates = connection.scalar(text('SELECT COUNT(*) FROM candidate WHERE PSCID = :subject'),
                                           {'subject': self.subject})
            sessions = connection.scalar(text(
                'SELECT COUNT(*) FROM session JOIN candidate ON session.CandidateID = candidate.ID '
                'WHERE candidate.PSCID = :subject'), {'subject': self.subject})
        return files, candidates, sessions

    def test_real_import_and_repeat_keep_one_candidate_session_and_file(self):
        runtime.verify_ownership(self.engine, self.source, self.site, self.settings)
        first = runtime.import_dataset(self.view, self.settings)
        self.assertEqual(first['errors'], 0)
        self.assertEqual(first['imported'], 1)
        self.assertEqual(self.counts(), (1, 1, 1))
        second = runtime.import_dataset(self.view, self.settings)
        self.assertEqual(second['ignored'], 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def annex(self, path):
        """Move a file into the dataset annex as a locked git-annex link; return its object."""
        import hashlib
        data = path.read_bytes()
        key = 'MD5E-s%d--%s.nii.gz' % (len(data), hashlib.md5(data).hexdigest())
        stored = self.source / '.git/annex/objects/Aa/Bb' / key / key
        stored.parent.mkdir(parents=True)
        path.rename(stored)
        path.symlink_to(os.path.relpath(stored, path.parent))
        return stored

    def test_clone_update_imports_additions_after_registered_content_was_dropped(self):
        import nibabel as nib
        import numpy as np
        first = self.annex(self.image)
        self.view = runtime.ROOT / 'assembly_bids' / (self.view.name + '-annex')
        view = runtime.helper('bids-view')
        view.prepare_view(self.source, self.view, self.settings['project'], self.site, self.name)
        self.assertEqual(runtime.import_dataset(self.view, self.settings)['imported'], 1)
        first.unlink()
        added = self.image.with_name('sub-' + self.subject + '_T2w.nii.gz')
        nib.save(nib.Nifti1Image(np.ones((8, 8, 8), dtype=np.float32), np.eye(4)), added)
        self.annex(added)
        view.prepare_view(self.source, self.view, self.settings['project'], self.site, self.name,
                          reuse=True, extend=True)
        result = runtime.import_dataset(self.view, self.settings)
        self.assertEqual((result['errors'], result['imported'], result['ignored']), (0, 1, 1))
        self.assertEqual(self.counts(), (2, 1, 1))

    def test_partial_file_commit_repairs_preview_on_retry(self):
        from loris_bids_importer.mri import main as mri
        with patch.object(mri, 'create_nifti_preview_picture', side_effect=RuntimeError('synthetic preview failure')):
            with self.assertRaisesRegex(ValueError, 'acquisition verification'):
                runtime.import_dataset(self.view, self.settings)
        self.assertEqual(self.counts(), (1, 1, 1))
        result = runtime.import_dataset(self.view, self.settings)
        self.assertEqual(result['errors'], 0)
        self.assertEqual(result['ignored'], 1)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.engine.connect() as connection:
            preview = connection.scalar(text(
                "SELECT p.Value FROM parameter_file p JOIN parameter_type t ON p.ParameterTypeID = t.ParameterTypeID "
                "JOIN files f ON p.FileID = f.FileID WHERE t.Name = 'check_pic_filename' AND f.File LIKE :prefix"
            ), {'prefix': str(self.view.relative_to(runtime.ROOT)) + '/%'})
        self.assertTrue((runtime.ROOT / 'pic' / preview).is_file())

    def test_existing_candidate_cannot_be_silently_reused_for_another_site(self):
        runtime.import_dataset(self.view, self.settings)
        with self.assertRaisesRegex(ValueError, 'different site/project'):
            runtime.verify_ownership(self.engine, self.source, 'unrelated', self.settings)

    def test_worker_defers_under_lock_then_records_verified_success(self):
        admin = create_engine(os.environ['BIDS_TEST_ADMIN_DATABASE_URL'])
        self.addCleanup(admin.dispose)
        runtime.initialize(admin)
        identity = runtime.enroll(self.engine, self.site, 'dataset', self.settings, self.name)
        first = runtime.reserve(self.engine, identity, self.settings)
        config = configuration()
        config['site']['name'] = self.site
        with runtime.database_lock(self.engine, 'import') as acquired:
            self.assertTrue(acquired)
            runtime.worker(self.engine, config, identity, first['attempt'])
        self.assertEqual(runtime.get_dataset(self.engine, identity)['status'], 'deferred')
        second = runtime.reserve(self.engine, identity, self.settings)
        setup = runtime.helper('bids-setup')
        setup.activate_modules = lambda *args: None
        original_helper = runtime.helper
        with patch.object(runtime, 'helper', side_effect=lambda name: setup if name == 'bids-setup' else original_helper(name)):
            with patch.object(runtime.os, 'statvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                runtime.worker(self.engine, config, identity, second['attempt'])
        row = runtime.get_dataset(self.engine, identity)
        self.assertEqual(row['status'], 'succeeded')
        self.assertIn('import', json.loads(row['completed_modes']))
        self.view = Path(row['view_path'])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_failure_after_candidate_session_commit_resumes_without_duplicates(self):
        from loris_bids_importer.mri import main as mri
        with patch.object(mri, 'get_check_bids_nifti_file_hash', side_effect=RuntimeError('synthetic hash failure')):
            with self.assertRaisesRegex(ValueError, 'acquisition verification'):
                runtime.import_dataset(self.view, self.settings)
        self.assertEqual(self.counts(), (0, 1, 1))
        result = runtime.import_dataset(self.view, self.settings)
        self.assertEqual(result['imported'], 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_eeg_partial_registration_resumes_archives_without_duplicate_files(self):
        from loris_bids_importer.eeg import main as eeg
        directory = self.source / ('sub-' + self.subject) / 'eeg'
        directory.mkdir()
        name = 'sub-' + self.subject + '_task-rest_eeg'
        def field(value, width):
            return str(value).ljust(width).encode('ascii')
        header = b''.join(field(value, width) for value, width in [
            ('0', 8), ('synthetic', 80), ('synthetic', 80), ('01.01.01', 8), ('00.00.00', 8),
            (512, 8), ('', 44), (1, 8), (1, 8), (1, 4),
            ('Cz', 16), ('', 80), ('uV', 8), (-1, 8), (1, 8), (-32768, 8), (32767, 8),
            ('', 80), (32, 8), ('', 32),
        ])
        (directory / (name + '.edf')).write_bytes(header + bytes(64))
        (directory / (name + '.json')).write_text(json.dumps({
            'TaskName': 'rest', 'SamplingFrequency': 32, 'PowerLineFrequency': 60,
            'EEGReference': 'Cz', 'SoftwareFilters': 'n/a',
        }))
        (directory / ('sub-' + self.subject + '_task-rest_channels.tsv')).write_text(
            'name\ttype\tunits\tsampling_frequency\tstatus\nCz\tEEG\tuV\t32\tgood\n')
        self.view = self.view.with_name(self.view.name + '-eeg')
        runtime.helper('bids-view').prepare_view(
            self.source, self.view, self.settings['project'], self.site, self.name)
        with patch.object(eeg, 'import_physio_file_archive', side_effect=RuntimeError('synthetic archive failure')):
            with self.assertRaisesRegex(ValueError, 'acquisition verification'):
                runtime.import_dataset(self.view, self.settings)
        runtime.helper('bids-view').prepare_view(
            self.source, self.view, self.settings['project'], self.site, self.name, reuse=True)
        result = runtime.import_dataset(self.view, self.settings)
        self.assertEqual(result['errors'], 0)
        runtime.helper('bids-view').prepare_view(
            self.source, self.view, self.settings['project'], self.site, self.name, reuse=True)
        runtime.import_dataset(self.view, self.settings)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                'SELECT f.PhysiologicalFileID, a.FilePath FROM physiological_file f '
                'JOIN physiological_archive a ON a.PhysiologicalFileID = f.PhysiologicalFileID '
                'WHERE f.FilePath LIKE :prefix'
            ), {'prefix': str(self.view.relative_to(runtime.ROOT)) + '/%'}).all()
        self.assertEqual(len(rows), 1)
        self.assertTrue((runtime.ROOT / rows[0].FilePath).is_file())

    def test_real_legacy_migration_preserves_paths_and_adopts_without_reimport(self):
        import shutil
        migration = runtime.helper('bids_migrate')
        runtime.import_dataset(self.view, self.settings)
        config = configuration()
        destination_site = 'migrated-' + self.site
        config['site']['name'] = destination_site
        config['site']['siteAlias'] = 'M' + self.site.removeprefix('fixture')
        config['site']['bidsImport'].update(
            project=self.settings['project'], projectAlias=self.settings['projectAlias'])
        config['site']['bidsImport']['datasets'] = {'dataset': {'cohort': 'Migrated ' + self.name}}
        journal = migration.plan_migration(
            self.engine, config, 'dataset', self.view, self.source, self.site, self.name)
        new_source = runtime.ROOT / 'assembly_bids' / destination_site / 'dataset'
        shutil.copytree(self.source, new_source)
        admin = create_engine(os.environ['BIDS_TEST_ADMIN_DATABASE_URL'])
        self.addCleanup(admin.dispose)
        runtime.initialize(admin)
        journal_path = Path(self.profile.name) / 'migration.json'
        backup = Path(self.profile.name) / 'consistent-fixture-backup'
        backup.write_text('isolated database is discarded after this test suite')
        migration.save_journal(journal_path, journal)
        with patch.object(migration.os, 'statvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            with patch.object(runtime, 'adopt_dataset', side_effect=RuntimeError('synthetic adoption interruption')):
                with self.assertRaisesRegex(RuntimeError, 'adoption interruption'):
                    migration.execute(self.engine, journal, journal_path, backup)
            self.assertEqual(json.loads(journal_path.read_text())['phase'], 'verified')
            migration.execute(self.engine, journal, journal_path, backup)
        self.assertEqual(journal['phase'], 'adopted')
        row = runtime.get_dataset(self.engine, runtime.dataset_id(destination_site, 'dataset'))
        self.assertEqual(row['view_path'], str(self.view))
        self.assertEqual(row['status'], 'succeeded')
        self.assertIsNone(runtime.reserve(self.engine, row['id'], journal['settings']))
        runtime.verify_ownership(self.engine, new_source, destination_site, journal['settings'])
        self.assertEqual(self.counts(), (1, 1, 1))


if __name__ == '__main__':
    unittest.main()
