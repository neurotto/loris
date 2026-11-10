from contextlib import contextmanager
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

KEY = 'SHA256E-s1781--165ef982dedf.vhdr'


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
        objects = self.data / 'assembly_bids/nandou/sigmo/.git/annex/objects/Ab/Cd' / KEY
        objects.mkdir(parents=True)
        self.eeg = self.data / 'assembly_bids/import-1/sub-01/eeg'
        self.eeg.mkdir(parents=True)
        # Content dropped: the link target does not exist.
        (self.eeg / 'sub-01_task-rest_eeg.vhdr').symlink_to(objects / KEY)
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

    def test_clone_recordings_get_the_cache_chunk_path(self):
        self.assertEqual(
            bids_ephys.lazy_chunk_path(self.data, Path('assembly_bids/import-1/sub-01/eeg/sub-01_task-rest_eeg.vhdr')),
            Path('re-move-chunks/nandou/sigmo', KEY, 'sub-01_task-rest_eeg.chunks'))

    def test_other_recordings_have_no_cache_chunk_path(self):
        base = Path('assembly_bids/import-1/sub-01/eeg')
        self.assertIsNone(bids_ephys.lazy_chunk_path(self.data, base / 'sub-01_task-rest_eeg.edf'))
        self.assertIsNone(bids_ephys.lazy_chunk_path(self.data, base / 'sub-01_task-rest_eeg.eeg'))
        (self.eeg / 'x_eeg.vhdr').symlink_to('../../../nandou/sigmo/.git/annex/objects/Ab/Cd/bad key/bad key')
        self.assertIsNone(bids_ephys.lazy_chunk_path(self.data, base / 'x_eeg.vhdr'))
        (self.eeg / 'y_eeg.vhdr').symlink_to('../../../../../etc/passwd')
        self.assertIsNone(bids_ephys.lazy_chunk_path(self.data, base / 'y_eeg.vhdr'))

    def upstream(self, parameters, chunked, logged):
        """Fake the LORIS-MRI modules resumable_ephys imports."""
        eeg = ModuleType('loris_bids_importer.eeg.main')
        eeg.Eeg = type('Eeg', (), {'fetch_and_insert_eeg_files': lambda instance, derivatives, detect: [
            {'file': Path(file.path).name} for file in instance.bids_layout.get(extension=['vhdr', 'vmrk', 'eeg'])]})
        eeg.import_physio_file_archive = eeg.import_physio_event_archive = lambda *args: None
        eeg.create_physio_channels_chunks = lambda env, file: chunked.append(file.path)

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
            'lib.logging': module('x', log=lambda env, message: logged.append(message)),
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

    def test_import_registers_cache_chunks_and_skips_other_recordings(self):
        parameters, chunked, logged = {}, [], []
        eeg, modules = self.upstream(parameters, chunked, logged)
        env = SimpleNamespace(db=SimpleNamespace(commit=lambda: None))
        base = Path('assembly_bids/import-1/sub-01/eeg')
        with modules:
            original = eeg.create_physio_channels_chunks
            with bids_ephys.resumable_ephys(self.data / 'assembly_bids/import-1'):
                eeg.create_physio_channels_chunks(env, SimpleNamespace(id=2, path=base / 'sub-01_task-rest_eeg.vhdr'))
                eeg.create_physio_channels_chunks(env, SimpleNamespace(id=2, path=base / 'sub-01_task-rest_eeg.vhdr'))
                eeg.create_physio_channels_chunks(env, SimpleNamespace(id=4, path=base / 'sub-01_task-rest_eeg.edf'))
                eeg.create_physio_channels_chunks(env, SimpleNamespace(id=5, path=base / 'local_eeg.vhdr'))
            self.assertIs(eeg.create_physio_channels_chunks, original)

        self.assertEqual(parameters, {(2, 'electrophysiology_chunked_dataset_path'):
                                      Path('re-move-chunks/nandou/sigmo', KEY, 'sub-01_task-rest_eeg.chunks')})
        self.assertEqual(chunked, [])
        self.assertEqual(len(logged), 2)

    def test_import_registers_brainvision_once(self):
        eeg, modules = self.upstream({}, [], [])
        files = [bids_file(self.eeg / name) for name in ('a_eeg.vhdr', 'a_eeg.vmrk', 'a_eeg.eeg')]
        instance = SimpleNamespace(
            bids_layout=Layout(files), bids_info=SimpleNamespace(subject='01', session=None, data_type='eeg'),
            data_dir=self.data, env=SimpleNamespace(db=None), session=SimpleNamespace(id=1))
        with modules, bids_ephys.resumable_ephys(self.data / 'assembly_bids/import-1'):
            fetched = eeg.Eeg.fetch_and_insert_eeg_files(instance)
        self.assertEqual(fetched, [{'file': 'a_eeg.vhdr'}])
        self.assertIsInstance(instance.bids_layout, Layout)


    def test_repair_plan_keeps_headers_and_records_missing_chunk_paths(self):
        sys.path.insert(0, str(SCRIPT.parent))
        self.addCleanup(sys.path.remove, str(SCRIPT.parent))
        repair_spec = importlib.util.spec_from_file_location('bids_eeg_chunks', SCRIPT.with_name('bids_eeg_chunks.py'))
        repair = importlib.util.module_from_spec(repair_spec)
        repair_spec.loader.exec_module(repair)
        base = Path('assembly_bids/import-1/sub-01/eeg')
        files = [(1, base / 'sub-01_task-rest_eeg.eeg'), (2, base / 'sub-01_task-rest_eeg.vhdr'),
                 (3, base / 'sub-01_task-rest_eeg.vmrk'), (4, base / 'sub-01_task-rest_eeg.edf')]
        chunks = str(Path('re-move-chunks/nandou/sigmo', KEY, 'sub-01_task-rest_eeg.chunks'))
        self.assertEqual(repair.plan(files, {}, self.data), (files[0:1] + files[2:3], {2: chunks}))
        self.assertEqual(repair.plan(files[1:2], {2: chunks}, self.data), ([], {}))
        self.assertEqual(repair.plan(files[1:2], {2: 'old.chunks'}, self.data), ([], {2: chunks}))


if __name__ == '__main__':
    unittest.main()
