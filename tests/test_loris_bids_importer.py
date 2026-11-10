import json
import os
from pathlib import Path
import re
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
        identity = self.settings.get('subjectPrefix', '') + self.subject
        with self.engine.connect() as connection:
            files = connection.scalar(text('SELECT COUNT(*) FROM files WHERE File LIKE :prefix'), {'prefix': prefix})
            candidates = connection.scalar(text('SELECT COUNT(*) FROM candidate WHERE PSCID = :subject'),
                                           {'subject': identity})
            sessions = connection.scalar(text(
                'SELECT COUNT(*) FROM session JOIN candidate ON session.CandidateID = candidate.ID '
                'WHERE candidate.PSCID = :subject'), {'subject': identity})
        return files, candidates, sessions

    def numeric_subject(self):
        subject = self.source / ('sub-' + self.subject)
        for path in subject.rglob('*'):
            if path.is_file():
                path.rename(path.with_name(path.name.replace('sub-' + self.subject, 'sub-001')))
        subject.rename(self.source / 'sub-001')
        self.subject = '001'
        self.image = self.source / 'sub-001/anat/sub-001_T1w.nii.gz'
        (self.source / 'participants.tsv').write_text('participant_id\tsex\nsub-001\tM\n')
        self.settings['subjectPrefix'] = 'mapped' + self.site
        self.view = self.view.with_name(self.view.name + '-numeric')
        runtime.helper('bids-view').prepare_view(
            self.source, self.view, self.settings['project'], self.site, self.name,
            subject_prefix=self.settings['subjectPrefix'])

    def test_numeric_subject_mapping_imports_and_retries_without_relabelling_source(self):
        self.numeric_subject()
        original = (self.source / 'participants.tsv').read_bytes()
        runtime.verify_ownership(self.engine, self.source, self.site, self.settings)
        self.assertEqual(runtime.import_dataset(self.view, self.settings)['imported'], 1)
        runtime.verify_ownership(self.engine, self.source, self.site, self.settings)
        self.assertEqual(runtime.import_dataset(self.view, self.settings)['ignored'], 1)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual((self.source / 'participants.tsv').read_bytes(), original)
        self.assertTrue(self.image.is_file())
        self.assertTrue((self.view / 'sub-001/anat/sub-001_T1w.nii.gz').is_symlink())
        with self.assertRaisesRegex(ValueError, 'different site/project'):
            runtime.verify_ownership(self.engine, self.source, 'another-site', self.settings)

    def test_numeric_subject_eeg_mapping_resumes_without_duplicate_registrations(self):
        self.numeric_subject()
        self.test_eeg_partial_registration_resumes_archives_without_duplicate_files()

    def test_numeric_subject_worker_completes_preflight_and_records_success(self):
        self.numeric_subject()
        self.test_worker_defers_under_lock_then_records_verified_success()

    def brainvision(self):
        """Add a numeric-subject BrainVision recording and return its source directory."""
        self.numeric_subject()
        directory = self.source / 'sub-001/eeg'
        directory.mkdir()
        name = 'sub-001_task-rest_eeg'
        header = directory / (name + '.vhdr')
        header.write_text(
            'Brain Vision Data Exchange Header File Version 1.0\n; ' + self.site + '\n'
            '[Common Infos]\nCodepage=UTF-8\nDataFile=' + name + '.eeg\n'
            'MarkerFile=' + name + '.vmrk\nDataFormat=BINARY\n'
            'DataOrientation=MULTIPLEXED\nNumberOfChannels=1\nSamplingInterval=31250\n'
            '[Binary Infos]\nBinaryFormat=INT_16\n'
            '[Channel Infos]\nCh1=Cz,,1,uV\n')
        (directory / (name + '.vmrk')).write_text(
            'Brain Vision Data Exchange Marker File, Version 1.0\n; ' + self.site + '\n'
            '[Common Infos]\nCodepage=UTF-8\nDataFile=' + name + '.eeg\n'
            '[Marker Infos]\nMk1=New Segment,,1,1,0,20200101000000000000\n')
        (directory / (name + '.eeg')).write_bytes(bytes(range(62)) + self.site[-2:].encode().rjust(2, b'0'))
        (directory / (name + '.json')).write_text(json.dumps({
            'TaskName': 'rest', 'SamplingFrequency': 32, 'PowerLineFrequency': 60,
            'EEGReference': 'Cz', 'SoftwareFilters': 'n/a',
        }))
        (directory / 'sub-001_task-rest_channels.tsv').write_text(
            'name\ttype\tunits\tsampling_frequency\tstatus\nCz\tEEG\tuV\t32\tgood\n')
        return directory

    def brainvision_view(self):
        self.view = self.view.with_name(self.view.name + '-brainvision')
        runtime.helper('bids-view').prepare_view(
            self.source, self.view, self.settings['project'], self.site, self.name,
            subject_prefix=self.settings['subjectPrefix'])

    def physiological(self):
        """Return (FilePath, chunk path or None) of the view's physiological files."""
        with self.engine.connect() as connection:
            return connection.execute(text(
                'SELECT f.FilePath, p.Value FROM physiological_file f '
                'LEFT JOIN (physiological_parameter_file p JOIN parameter_type t '
                "ON t.ParameterTypeID = p.ParameterTypeID AND t.Name = 'electrophysiology_chunked_dataset_path') "
                'ON p.PhysiologicalFileID = f.PhysiologicalFileID '
                'WHERE f.FilePath LIKE :prefix ORDER BY f.FilePath'
            ), {'prefix': str(self.view.relative_to(runtime.ROOT)) + '/%'}).all()

    def file_reader_lookup(self, chunks):
        """Run the patched file_reader chunk lookup as LORIS pselectOne does (it appends LIMIT 2)."""
        source = Path('/var/www/loris/modules/electrophysiology_browser/php/file_reader.class.inc').read_text()
        query = re.search(r'pselectOne\(\s*"([^"]+)"', source).group(1)
        with self.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(query + ' LIMIT 2'), {'chunks': chunks}).all()]

    def test_numeric_brainvision_mapping_keeps_header_references_on_retry(self):
        directory = self.brainvision()
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        self.brainvision_view()
        registrations = []
        for _ in range(2):
            self.assertEqual(runtime.import_dataset(self.view, self.settings)['errors'], 0)
            with self.engine.connect() as connection:
                registrations.append(connection.execute(text(
                    'SELECT f.FilePath, a.FilePath FROM physiological_file f '
                    'JOIN physiological_archive a ON a.PhysiologicalFileID = f.PhysiologicalFileID '
                    'WHERE f.FilePath LIKE :prefix ORDER BY f.FilePath'
                ), {'prefix': str(self.view.relative_to(runtime.ROOT)) + '/%'}).all())
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)
        self.assertEqual(registrations[0], registrations[1])
        # The .vhdr header stands for the recording; its .vmrk and .eeg are not registered apart.
        self.assertEqual([Path(row[0]).suffix for row in registrations[1]], ['.vhdr'])
        self.assertTrue(all((runtime.ROOT / row[1]).is_file() for row in registrations[1]))

    def test_eeg_repair_keeps_the_header_and_records_the_cache_chunk_path(self):
        directory = self.brainvision()
        stored = self.annex(directory / 'sub-001_task-rest_eeg.vhdr')
        self.brainvision_view()
        original_helper = runtime.helper

        def legacy(name):
            module = original_helper(name)
            if name == 'bids_ephys':
                module.brainvision_primary = lambda files: files
                module.lazy_chunk_path = lambda data_dir, path: None
            return module

        with patch.object(runtime, 'helper', side_effect=legacy):
            self.assertEqual(runtime.import_dataset(self.view, self.settings)['errors'], 0)
        legacy_rows = self.physiological()
        self.assertEqual([(Path(row[0]).suffix, row[1]) for row in legacy_rows],
                         [('.eeg', None), ('.vhdr', None), ('.vmrk', None)])

        repair = runtime.helper('bids_eeg_chunks')
        with self.assertRaisesRegex(ValueError, 'Expected 2'):
            repair.repair(self.engine, self.view, runtime.ROOT, 2, apply=True)
        repair.repair(self.engine, self.view, runtime.ROOT, 3)
        self.assertEqual(self.physiological(), legacy_rows)
        repair.repair(self.engine, self.view, runtime.ROOT, 3, apply=True)
        key = stored.name
        chunks = 're-move-chunks/%s/dataset/%s/sub-001_task-rest_eeg.chunks' % (self.site, key)
        header = [row for row in legacy_rows if row[0].endswith('.vhdr')][0][0]
        self.assertEqual([tuple(row) for row in self.physiological()], [(header, chunks)])
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text(
                'SELECT c.Value FROM Config c JOIN ConfigSettings s ON s.ID = c.ConfigID '
                "WHERE s.Name = 'useEEGBrowserVisualizationComponents'")), 'true')
            self.assertEqual(connection.scalar(text(
                'SELECT COUNT(*) FROM physiological_channel ch JOIN physiological_file f '
                'ON f.PhysiologicalFileID = ch.PhysiologicalFileID WHERE f.FilePath = :path'), {'path': header}), 1)
            self.assertTrue(connection.scalar(text(
                'SELECT COUNT(*) FROM physiological_archive a JOIN physiological_file f '
                'ON f.PhysiologicalFileID = a.PhysiologicalFileID WHERE f.FilePath = :path'), {'path': header}))
            self.assertEqual(connection.scalar(text(
                'SELECT c.Name FROM parameter_type t JOIN parameter_type_category_rel r USING (ParameterTypeID) '
                'JOIN parameter_type_category c USING (ParameterTypeCategoryID) '
                "WHERE t.Name = 'electrophysiology_chunked_dataset_path'")), 'Electrophysiology Variables')
        self.assertEqual(self.file_reader_lookup(chunks), [(header,)])
        self.assertEqual(self.file_reader_lookup(chunks + 'x'), [(None,)])
        repair.repair(self.engine, self.view, runtime.ROOT, 1, apply=True)
        self.assertEqual([tuple(row) for row in self.physiological()], [(header, chunks)])
        self.assertEqual(runtime.import_dataset(self.view, self.settings)['errors'], 0)
        self.assertEqual([tuple(row) for row in self.physiological()], [(header, chunks)])

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
        key = 'MD5E-s%d--%s%s' % (len(data), hashlib.md5(data).hexdigest(), ''.join(path.suffixes))
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

    def test_preview_only_repair_fetches_missing_content_and_preserves_registrations(self):
        repair = runtime.helper('bids_previews')
        runtime.import_dataset(self.view, self.settings)
        with self.engine.connect() as connection:
            relative = connection.scalar(text(
                "SELECT p.Value FROM parameter_file p JOIN parameter_type t ON p.ParameterTypeID=t.ParameterTypeID "
                "JOIN files f ON p.FileID=f.FileID WHERE t.Name='check_pic_filename' AND f.File LIKE :prefix"
            ), {'prefix': str(self.view.relative_to(runtime.ROOT)) + '/%'})
        preview = runtime.ROOT / 'pic' / relative
        preview.unlink()
        content = self.image.read_bytes()
        self.image.unlink()
        def fetch(path, root, site, dataset, url):
            self.assertFalse(path.exists())
            self.image.write_bytes(content)
        with patch.object(repair, 'cache_content', side_effect=fetch) as cache:
            self.assertEqual(repair.repair(
                self.engine, self.view, runtime.ROOT, self.site, 'dataset', 'http://cache', 1), 1)
            cache.assert_not_called()
            self.assertEqual(repair.repair(
                self.engine, self.view, runtime.ROOT, self.site, 'dataset', 'http://cache', 1, True), 0)
            self.assertEqual(cache.call_count, 1)
            self.assertEqual(repair.repair(
                self.engine, self.view, runtime.ROOT, self.site, 'dataset', 'http://cache', 1, True), 0)
            self.assertEqual(cache.call_count, 1)
        self.assertTrue(preview.is_file())
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.assertRaisesRegex(ValueError, 'Expected 2'):
            repair.repair(self.engine, self.view, runtime.ROOT, self.site, 'dataset', 'http://cache', 2, True)

    def test_preview_only_repair_propagates_fetch_failure_without_importing(self):
        repair = runtime.helper('bids_previews')
        runtime.import_dataset(self.view, self.settings)
        with patch.object(repair, 'preview_valid', return_value=False), patch.object(
                repair, 'cache_content', side_effect=ValueError('site offline')):
            with self.assertRaisesRegex(ValueError, 'site offline'):
                repair.repair(self.engine, self.view, runtime.ROOT, self.site, 'dataset', 'http://cache', 1, True)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_import_cannot_silently_ignore_missing_preview_with_dropped_image(self):
        runtime.import_dataset(self.view, self.settings)
        with self.engine.connect() as connection:
            relative = connection.scalar(text(
                "SELECT p.Value FROM parameter_file p JOIN parameter_type t ON p.ParameterTypeID=t.ParameterTypeID "
                "JOIN files f ON p.FileID=f.FileID WHERE t.Name='check_pic_filename' AND f.File LIKE :prefix"
            ), {'prefix': str(self.view.relative_to(runtime.ROOT)) + '/%'})
        (runtime.ROOT / 'pic' / relative).unlink()
        self.image.unlink()
        with self.assertRaisesRegex(ValueError, 'acquisition verification'):
            runtime.import_dataset(self.view, self.settings)
        self.assertEqual(self.counts(), (1, 1, 1))

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
            ('0', 8), ('synthetic ' + self.site, 80), ('synthetic', 80), ('01.01.01', 8), ('00.00.00', 8),
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
            self.source, self.view, self.settings['project'], self.site, self.name,
            subject_prefix=self.settings.get('subjectPrefix', ''))
        with patch.object(eeg, 'import_physio_file_archive', side_effect=RuntimeError('synthetic archive failure')):
            with self.assertRaisesRegex(ValueError, 'acquisition verification'):
                runtime.import_dataset(self.view, self.settings)
        runtime.helper('bids-view').prepare_view(
            self.source, self.view, self.settings['project'], self.site, self.name, reuse=True,
            subject_prefix=self.settings.get('subjectPrefix', ''))
        result = runtime.import_dataset(self.view, self.settings)
        self.assertEqual(result['errors'], 0)
        runtime.helper('bids-view').prepare_view(
            self.source, self.view, self.settings['project'], self.site, self.name, reuse=True,
            subject_prefix=self.settings.get('subjectPrefix', ''))
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
