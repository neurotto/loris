from contextlib import contextmanager
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'charts/loris/files/bids_ephys.py'
spec = importlib.util.spec_from_file_location('bids_ephys', SCRIPT)
bids_ephys = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bids_ephys)

def bids_file(path):
    return SimpleNamespace(path=str(path))


class Layout:
    def __init__(self, files):
        self.files = files
        self.queries = []

    def get(self, **kwargs):
        self.queries.append(kwargs)
        return list(self.files)

    def get_nearest(self, *args, **kwargs):
        return None


class EphysTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        source = self.data / 'assembly_bids/nandou/sigmo/sub-01/eeg'
        source.mkdir(parents=True)
        (source / 'sub-01_task-rest_eeg.vhdr').write_text('header')
        self.eeg = self.data / 'assembly_bids/import-1/sub-01/eeg'
        self.eeg.mkdir(parents=True)
        (self.eeg / 'sub-01_task-rest_eeg.vhdr').symlink_to(
            source / 'sub-01_task-rest_eeg.vhdr')
        (self.eeg / 'sub-01_task-rest_eeg.edf').write_bytes(b'nfs copy')
        (self.eeg / 'sub-01_task-rest_eeg.eeg').symlink_to('/etc/hostname')

    def test_brainvision_recordings_are_listed_by_their_header(self):
        files = [bids_file(self.eeg / name) for name in (
            'a_eeg.vhdr', 'a_eeg.vmrk', 'a_eeg.eeg', 'b_eeg.eeg', 'c_eeg.edf')]
        self.assertEqual([Path(file.path).name for file in bids_ephys.brainvision_primary(files)],
                         ['a_eeg.vhdr', 'b_eeg.eeg', 'c_eeg.edf'])

    def test_layout_filters_only_eeg_queries(self):
        files = [bids_file(self.eeg / 'a_eeg.vhdr'), bids_file(self.eeg / 'a_eeg.eeg')]
        layout = bids_ephys.BrainVisionLayout(Layout(files))
        self.assertEqual(len(layout.get(extension=['vhdr', 'eeg'])), 1)
        self.assertEqual(len(layout.get(extension='json')), 2)
        self.assertIsNone(layout.get_nearest('x'))

    def test_provider_recordings_get_a_deterministic_path(self):
        view_path = Path(
            'assembly_bids/import-fixture/sub-01/eeg/sub-01_task-rest_eeg.vhdr'
        )
        import_view = self.data / view_path
        import_view.parent.mkdir(parents=True)
        provider_file = self.data / 'assembly_bids/nandou/sigmo/sub-01/eeg/sub-01_task-rest_eeg.vhdr'
        import_view.symlink_to(provider_file)

        with patch.dict(os.environ, {'LORIS_CONTENT_PROVIDER_URL': 'http://provider'}):
            chunk_path = bids_ephys.lazy_chunk_path(
                self.data,
                view_path,
            )

        digest = hashlib.sha256('sub-01/eeg/sub-01_task-rest_eeg.vhdr'.encode()).hexdigest()
        self.assertEqual(
            chunk_path,
            Path('content-provider-chunks/nandou/sigmo', digest + '.chunks'),
        )

    def test_provider_chunk_paths_support_long_unicode_dataset_names(self):
        dataset = 'study with ' + ('x' * 100) + '\u00e9'
        provider_file = (
            self.data / 'assembly_bids/nandou' / dataset / 'sub-01/eeg'
            / 'sub-01_task-rest_eeg.vhdr'
        )
        provider_file.parent.mkdir(parents=True)
        provider_file.write_text('header')
        view_path = Path(
            'assembly_bids/import-fixture/sub-01/eeg/sub-01_task-rest_eeg.vhdr'
        )
        import_view = self.data / view_path
        import_view.parent.mkdir(parents=True)
        import_view.symlink_to(provider_file)

        with patch.dict(os.environ, {'LORIS_CONTENT_PROVIDER_URL': 'http://provider'}):
            chunk_path = bids_ephys.lazy_chunk_path(self.data, view_path)

        digest = hashlib.sha256('sub-01/eeg/sub-01_task-rest_eeg.vhdr'.encode()).hexdigest()
        self.assertEqual(
            chunk_path,
            Path('content-provider-chunks/nandou', dataset, digest + '.chunks'),
        )

    def test_provider_chunk_paths_require_managed_recordings(self):
        base = Path('assembly_bids/import-1/sub-01/eeg')
        with patch.dict(os.environ, {'LORIS_CONTENT_PROVIDER_URL': 'http://provider'}):
            self.assertIsNone(bids_ephys.lazy_chunk_path(self.data, base / 'sub-01_task-rest_eeg.edf'))
            self.assertIsNone(bids_ephys.lazy_chunk_path(self.data, base / 'sub-01_task-rest_eeg.eeg'))
        (self.eeg / 'x_eeg.vhdr').symlink_to('../../../nandou/sigmo/.git/config')
        (self.eeg / 'y_eeg.vhdr').symlink_to('../../../../../etc/passwd')
        with patch.dict(os.environ, {'LORIS_CONTENT_PROVIDER_URL': 'http://provider'}):
            self.assertIsNone(bids_ephys.lazy_chunk_path(self.data, base / 'x_eeg.vhdr'))
            self.assertIsNone(bids_ephys.lazy_chunk_path(self.data, base / 'y_eeg.vhdr'))

    def upstream(self, parameters, chunked):
        """Fake the LORIS-MRI modules resumable_ephys imports."""
        eeg = ModuleType('loris_bids_importer.eeg.main')
        eeg.Eeg = type('Eeg', (), {'fetch_and_insert_eeg_files': lambda instance, derivatives, detect: [
            {'file': Path(file.path).name} for file in instance.bids_layout.get(extension=['vhdr', 'vmrk', 'eeg'])]})
        eeg.import_physio_file_archive = eeg.import_physio_event_archive = lambda *args: None
        eeg.create_physio_channels_chunks = lambda env, file: chunked.append(str(file.path))

        def module(name, **attributes):
            value = ModuleType(name)
            value.__dict__.update(attributes)
            return value

        modules = {
            'lib.db.models.physio_event_archive': module('x', DbPhysioEventArchive=None),
            'lib.db.models.physio_file_archive': module('x', DbPhysioFileArchive=None),
            'lib.db.queries.physio_file': module('x', try_get_physio_file_with_path=lambda db, path: None),
            'loris_bids_importer': module('loris_bids_importer', archive=None),
            'loris_bids_importer.eeg': module('x'),
            'loris_bids_importer.eeg.main': eeg,
            'loris_utils.path': module('x', remove_path_extension=None),
            'loris_utils.crypto': module('x', compute_file_blake2b_hash=None),
            'lib.config': module('x', get_data_dir_path_config=lambda env: self.data),
            'lib.physio.parameters': module('x', insert_physio_file_parameter=(
                lambda env, file, name, value: parameters.__setitem__((file.id, name), value))),
        }
        modules['loris_bids_importer.eeg'].main = eeg

        @contextmanager
        def active():
            with patch.dict(sys.modules, modules), patch.object(
                    bids_ephys, 'has_chunk_parameter',
                    lambda db, file_id: any(key[0] == file_id for key in parameters)):
                yield

        return eeg, active()

    def test_import_without_provider_chunks_brainvision_and_bdf_locally(self):
        parameters, chunked = {}, []
        eeg, modules = self.upstream(parameters, chunked)
        env = SimpleNamespace(db=SimpleNamespace(commit=lambda: None))
        base = Path('assembly_bids/import-1/sub-01/eeg')
        @contextmanager
        def generated(*args):
            yield Path('local.chunks')

        with modules, patch.object(bids_ephys, 'local_chunks', side_effect=generated) as local:
            original = eeg.create_physio_channels_chunks
            with bids_ephys.resumable_ephys(self.data / 'assembly_bids/import-1'):
                eeg.create_physio_channels_chunks(env, SimpleNamespace(id=2, path=base / 'sub-01_task-rest_eeg.vhdr'))
                eeg.create_physio_channels_chunks(env, SimpleNamespace(id=3, path=base / 'sub-01_task-rest_eeg.bdf'))
                eeg.create_physio_channels_chunks(env, SimpleNamespace(id=4, path=base / 'sub-01_task-rest_eeg.edf'))
            self.assertIs(eeg.create_physio_channels_chunks, original)

        self.assertEqual(parameters, {
            (2, 'electrophysiology_chunked_dataset_path'): Path('local.chunks'),
            (3, 'electrophysiology_chunked_dataset_path'): Path('local.chunks'),
        })
        self.assertEqual(local.call_count, 2)
        self.assertEqual(chunked, [
            'assembly_bids/import-1/sub-01/eeg/sub-01_task-rest_eeg.edf',
        ])

    def test_import_registers_provider_chunks(self):
        source = self.data / 'assembly_bids/nandou/sigmo/sub-01/eeg'
        provider_file = source / 'sub-01_task-rest_eeg.vhdr'
        provider_file.write_text('header')
        (self.eeg / provider_file.name).unlink()
        (self.eeg / provider_file.name).symlink_to(provider_file)

        parameters, chunked = {}, []
        eeg, modules = self.upstream(parameters, chunked)
        env = SimpleNamespace(db=SimpleNamespace(commit=lambda: None))
        base = Path('assembly_bids/import-1/sub-01/eeg')
        with patch.dict(os.environ, {'LORIS_CONTENT_PROVIDER_URL': 'http://provider'}), modules:
            with bids_ephys.resumable_ephys(self.data / 'assembly_bids/import-1'):
                eeg.create_physio_channels_chunks(
                    env,
                    SimpleNamespace(id=6, path=base / provider_file.name),
                )

        digest = hashlib.sha256('sub-01/eeg/sub-01_task-rest_eeg.vhdr'.encode()).hexdigest()
        self.assertEqual(
            parameters,
            {(6, 'electrophysiology_chunked_dataset_path'):
                Path('content-provider-chunks/nandou/sigmo', digest + '.chunks')},
        )
        self.assertEqual(chunked, [])

    def test_import_registers_brainvision_once(self):
        eeg, modules = self.upstream({}, [])
        files = [bids_file(self.eeg / name) for name in ('a_eeg.vhdr', 'a_eeg.vmrk', 'a_eeg.eeg')]
        instance = SimpleNamespace(
            bids_layout=Layout(files), bids_info=SimpleNamespace(subject='01', session=None, data_type='eeg'),
            data_dir=self.data, env=SimpleNamespace(db=None), session=SimpleNamespace(id=1))
        with modules, bids_ephys.resumable_ephys(self.data / 'assembly_bids/import-1'):
            fetched = eeg.Eeg.fetch_and_insert_eeg_files(instance)
        self.assertEqual(fetched, [{'file': 'a_eeg.vhdr'}])
        self.assertIsInstance(instance.bids_layout, Layout)


if __name__ == '__main__':
    unittest.main()
