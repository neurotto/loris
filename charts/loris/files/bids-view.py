"""Validate BIDS participants and build stable, metadata-enriched no-copy views.

Source files remain unchanged. Views contain ordinary directories, contained
source-file symlinks, and a generated participants.tsv with LORIS registration
columns. Retry validation distinguishes journaled importer archives from source
content; these lightweight checks are not a full BIDS compliance validator.
Filesystem arguments are pathlib.Path objects, and registration labels are strings.
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


ARTIFACTS = '.loris-import-artifacts.json'


def inspect_source(source):
    """Return participant columns, row dictionaries, and subject directory Paths.

    Require readable/traversable source, dataset description presence, and an
    exact participant-to-subject match. Reject numeric-only subject labels that
    need explicit identity mapping and reserved importer artifact metadata.
    No files are changed. Validation raises ValueError; filesystem errors propagate.
    """
    if not source.is_dir() or not os.access(source, os.R_OK | os.X_OK):
        raise ValueError('BIDS source is not readable/traversable')

    if not (source / 'dataset_description.json').is_file():
        raise ValueError('BIDS dataset_description.json is missing')

    if (source / ARTIFACTS).exists():
        raise ValueError('Source contains reserved importer artifact metadata')

    subjects = [entry for entry in source.glob('sub-*') if entry.is_dir()]
    if not subjects:
        raise ValueError('No BIDS subject directories found')

    if any(subject.name[4:].isdigit() for subject in subjects):
        raise ValueError('Numeric-only subject labels need an explicit CandID/PSCID mapping')

    with (source / 'participants.tsv').open(newline='') as handle:
        reader = csv.DictReader(handle, delimiter='\t')
        fields = list(reader.fieldnames or [])
        if 'participant_id' not in fields or len(fields) != len(set(fields)):
            raise ValueError('participants.tsv needs unique columns including participant_id')

        rows = list(reader)

    labels = [row['participant_id'].removeprefix('sub-') for row in rows]
    if len(labels) != len(set(labels)) or any(None in row for row in rows):
        raise ValueError('participants.tsv contains duplicate labels or malformed rows')

    if set(labels) != {subject.name[4:] for subject in subjects}:
        raise ValueError('Participant rows must match the subject directories')

    return fields, rows, subjects


def prepare_view(source, destination, project, site, cohort, reuse=False):
    """Create or verify a no-copy registration view and return its subject count.

    Enrich generated participant rows without overwriting conflicting source
    registration metadata. Build new views in a temporary sibling and rename only
    after completion; exclude root derivatives and reject escaping source links.

    Existing destinations fail unless reuse=True. Reuse compares directory/link
    layout and generated participant bytes against a fresh expected view. It also
    verifies journaled in-view archives and removes only those marked incomplete;
    external archive reconciliation belongs to the EEG adapter. It never rewrites
    source files or blindly replaces existing registered metadata.
    """
    fields, rows, subjects = inspect_source(source)
    additions = {'project': project, 'site': site, 'cohort': cohort}
    for field, expected in additions.items():
        if not expected:
            raise ValueError('Project, site and cohort are required')

        if field in fields and any(row[field] not in ('', 'n/a', expected) for row in rows):
            raise ValueError('Existing participant metadata conflicts with configured ' + field)

        if field not in fields:
            fields.append(field)

        for row in rows:
            row[field] = expected

    if destination.is_symlink():
        raise ValueError('Import view must not be a symlink')

    if destination.exists():
        if not reuse:
            raise ValueError('Import view already exists; use a new run ID rather than overwrite registered metadata')

        generated = {}
        marker = destination / ARTIFACTS
        if marker.exists():
            if marker.is_symlink():
                raise ValueError('Importer artifact metadata must not be a symlink')

            generated = json.loads(marker.read_text())
            for relative, checksum in generated.items():
                if relative.startswith('external:'):
                    continue

                path = destination / relative
                if Path(relative).is_absolute() or '..' in Path(relative).parts or path.suffix != '.tgz' or path.is_symlink():
                    raise ValueError('Invalid generated artifact path')

                if path.exists():
                    if checksum is None:
                        path.unlink()
                    else:
                        digest = hashlib.sha256()
                        with path.open('rb') as handle:
                            for block in iter(lambda: handle.read(1024 * 1024), b''):
                                digest.update(block)

                        if digest.hexdigest() != checksum:
                            raise ValueError('Generated import artifact was modified')

        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            expected = Path(temporary) / 'expected'
            prepare_view(source, expected, project, site, cohort)

            def manifest(root):
                """Describe comparable view entries, excluding owned generated artifacts."""
                entries = {}
                for entry in root.rglob('*'):
                    relative = str(entry.relative_to(root))
                    if root == destination and (relative == ARTIFACTS or relative in generated):
                        continue

                    if entry.is_symlink():
                        entries[relative] = ('link', os.readlink(entry))
                    elif entry.is_dir():
                        entries[relative] = ('directory',)
                    elif relative == 'participants.tsv':
                        entries[relative] = ('participants', entry.read_bytes())
                    else:
                        raise ValueError('Unexpected regular file in import view')

                return entries

            if manifest(destination) != manifest(expected):
                raise ValueError('Existing import view conflicts with source or registration metadata')

        return len(subjects)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.bids-view-', dir=destination.parent))
    try:
        _write_view(source, temporary, fields, rows)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    return len(subjects)


def _write_view(source, destination, fields, rows):
    """Populate a staging view with source links and enriched participant rows.

    fields/rows must already be validated and enriched by prepare_view. Refuse
    directory symlinks, out-of-root targets, and non-regular source files. This
    helper can leave partial staging content on error; its caller cleans it up.
    """

    def unreadable(error):
        """Propagate traversal errors rather than publishing an incomplete view."""
        raise error

    for current, directories, files in os.walk(source, followlinks=False, onerror=unreadable):
        source_dir = Path(current)
        relative_dir = source_dir.relative_to(source)
        target_dir = destination / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        if relative_dir == Path('.'):
            directories[:] = [name for name in directories if name != 'derivatives']

        for name in directories:
            if (source_dir / name).is_symlink():
                raise ValueError('Source contains a symlinked directory: ' + str(relative_dir / name))

        for name in files:
            if relative_dir == Path('.') and name in ('participants.tsv', 'derivatives'):
                continue

            source_file = source_dir / name
            resolved = source_file.resolve(strict=True)
            if not resolved.is_relative_to(source.resolve()):
                raise ValueError('Source file symlink escapes the approved BIDS root')

            if not resolved.is_file():
                raise ValueError('Source contains a non-regular file')

            (target_dir / name).symlink_to(resolved)

    with (destination / 'participants.tsv').open('x', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)


def main():
    """Validate a source or create a view from CLI arguments and print JSON counts.

    Without --destination, perform only lightweight metadata inspection.
    Expected validation/filesystem failures exit with a concise diagnostic.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--project', default='')
    parser.add_argument('--site', default='')
    parser.add_argument('--cohort', default='')
    args = parser.parse_args()

    try:
        fields, rows, subjects = inspect_source(args.source)
        if args.destination:
            prepare_view(args.source, args.destination, args.project, args.site, args.cohort)

        print(json.dumps({'subjects': len(subjects), 'participant_rows': len(rows),
                          'missing_registration_columns': sorted(set(['project', 'site', 'cohort']) - set(fields)),
                          'import_view_created': args.destination is not None}))
    except (ValueError, OSError) as error:
        raise SystemExit('BIDS metadata check failed: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__))


if __name__ == '__main__':
    main()
