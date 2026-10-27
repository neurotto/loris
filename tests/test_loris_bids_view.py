import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'charts/loris/files/bids-view.py'
spec = importlib.util.spec_from_file_location('bids_view', SCRIPT)
bids_view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bids_view)


class BidsViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'dataset_description.json').write_text('{"Name":"synthetic","BIDSVersion":"1.8.0"}')
        anat = self.source / 'sub-example' / 'ses-000' / 'anat'
        anat.mkdir(parents=True)
        (anat / 'sub-example_ses-000_T1w.nii.gz').write_bytes(b'synthetic NIfTI placeholder')
        (self.source / 'participants.tsv').write_text('participant_id\tsex\nsub-example\tM\n')

    def test_view_enriches_metadata_without_changing_source(self):
        original = (self.source / 'participants.tsv').read_bytes()
        destination = self.root / 'view'
        bids_view.prepare_view(self.source, destination, 'Penthera_3T', 'scilus', 'Penthera-3T')
        self.assertEqual((self.source / 'participants.tsv').read_bytes(), original)
        self.assertFalse((destination / 'sub-example').is_symlink())
        self.assertFalse((destination / 'sub-example' / 'ses-000' / 'anat').is_symlink())
        self.assertTrue((destination / 'sub-example' / 'ses-000' / 'anat' / 'sub-example_ses-000_T1w.nii.gz').is_symlink())
        self.assertIn('Penthera-3T', (destination / 'participants.tsv').read_text())
        with self.assertRaises(ValueError):
            bids_view.prepare_view(self.source, destination, 'Penthera_3T', 'scilus', 'Penthera-3T')

    def test_numeric_subjects_are_not_silently_registered(self):
        (self.source / 'sub-example').rename(self.source / 'sub-001')
        with self.assertRaisesRegex(ValueError, 'Numeric-only'):
            bids_view.inspect_source(self.source)

    def test_conflicting_metadata_is_not_overwritten(self):
        (self.source / 'participants.tsv').write_text('participant_id\tproject\nsub-example\tOther\n')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            bids_view.prepare_view(self.source, self.root / 'view', 'Penthera_3T', 'scilus', 'Penthera-3T')

    def test_retry_reuses_only_a_verified_identical_view(self):
        destination = self.root / 'view'
        for _ in range(2):
            self.assertEqual(bids_view.prepare_view(
                self.source, destination, 'Project', 'nandou', 'Dataset', reuse=True), 1)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            bids_view.prepare_view(self.source, destination, 'Project', 'another', 'Dataset', reuse=True)
        (destination / 'unexpected').write_text('unregistered')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            bids_view.prepare_view(self.source, destination, 'Project', 'nandou', 'Dataset', reuse=True)

    def test_failed_view_creation_does_not_leave_a_partial_destination(self):
        (self.source / 'escape').symlink_to(self.root / 'outside')
        (self.root / 'outside').write_text('outside')
        with self.assertRaisesRegex(ValueError, 'escapes'):
            bids_view.prepare_view(self.source, self.root / 'view', 'Project', 'nandou', 'Dataset')
        self.assertFalse((self.root / 'view').exists())
        self.assertEqual(list(self.root.glob('.bids-view-*')), [])

    def test_retry_removes_only_journaled_incomplete_generated_archives(self):
        destination = self.root / 'view'
        bids_view.prepare_view(self.source, destination, 'Project', 'nandou', 'Dataset')
        archive = destination / 'pending.tgz'
        archive.write_bytes(b'incomplete')
        (destination / bids_view.ARTIFACTS).write_text(json.dumps({'pending.tgz': None}))
        bids_view.prepare_view(self.source, destination, 'Project', 'nandou', 'Dataset', reuse=True)
        self.assertFalse(archive.exists())

    def test_modified_generated_archive_is_not_silently_reused(self):
        destination = self.root / 'view'
        bids_view.prepare_view(self.source, destination, 'Project', 'nandou', 'Dataset')
        (destination / 'modified.tgz').write_bytes(b'unexpected')
        (destination / bids_view.ARTIFACTS).write_text(json.dumps({'modified.tgz': '0' * 64}))
        with self.assertRaisesRegex(ValueError, 'artifact was modified'):
            bids_view.prepare_view(self.source, destination, 'Project', 'nandou', 'Dataset', reuse=True)


    def annex(self, relative, key, content=None):
        """Replace a source file with a locked git-annex link, optionally without content."""
        path = self.source / relative
        objects = self.source / '.git/annex/objects/Xx/Yy' / key
        objects.mkdir(parents=True, exist_ok=True)
        if content is not None:
            (objects / key).write_bytes(content)
        if path.exists() or path.is_symlink():
            path.unlink()
        path.symlink_to(os.path.relpath(objects / key, path.parent))
        return objects / key

    def test_annexed_files_link_by_key_without_content_and_skip_git(self):
        nifti = 'sub-example/ses-000/anat/sub-example_ses-000_T1w.nii.gz'
        target = self.annex(nifti, 'MD5E-s4--a.nii.gz')
        (self.source / '.git/config').write_text('[core]\n')
        destination = self.root / 'view'
        bids_view.prepare_view(self.source, destination, 'Project', 'nandou', 'Dataset')
        self.assertEqual(os.readlink(destination / nifti), str(target.resolve(strict=False)))
        self.assertFalse((destination / '.git').exists())
        self.assertEqual(bids_view.prepare_view(
            self.source, destination, 'Project', 'nandou', 'Dataset', reuse=True), 1)

    def test_extension_adds_new_files_but_rejects_changed_or_removed_ones(self):
        destination = self.root / 'view'
        self.annex('sub-example/ses-000/anat/sub-example_ses-000_T1w.nii.gz', 'MD5E-s4--a.nii.gz')
        bids_view.prepare_view(self.source, destination, 'Project', 'nandou', 'Dataset')
        anat = self.source / 'sub-other/ses-000/anat'
        anat.mkdir(parents=True)
        self.annex('sub-other/ses-000/anat/sub-other_ses-000_T1w.nii.gz', 'MD5E-s4--b.nii.gz')
        (self.source / 'participants.tsv').write_text('participant_id\tsex\nsub-example\tM\nsub-other\tF\n')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            bids_view.prepare_view(self.source, destination, 'Project', 'nandou', 'Dataset', reuse=True)
        self.assertEqual(bids_view.prepare_view(
            self.source, destination, 'Project', 'nandou', 'Dataset', reuse=True, extend=True), 2)
        self.assertTrue((destination / 'sub-other/ses-000/anat/sub-other_ses-000_T1w.nii.gz').is_symlink())
        self.assertIn('sub-other', (destination / 'participants.tsv').read_text())
        self.assertEqual([entry.name for entry in destination.parent.iterdir() if entry.name.startswith('.')], [])
        self.annex('sub-example/ses-000/anat/sub-example_ses-000_T1w.nii.gz', 'MD5E-s5--c.nii.gz')
        with self.assertRaisesRegex(ValueError, 'need review: sub-example'):
            bids_view.prepare_view(self.source, destination, 'Project', 'nandou', 'Dataset', reuse=True, extend=True)


if __name__ == '__main__':
    unittest.main()
