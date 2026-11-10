"""Audit or repair registered MRI previews without rerunning dataset import."""

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

import bids_runtime as runtime
from lib.config_file import load_config
from lib.db.connect import get_database_engine
from lib.db.models.file import DbFile
from lib.db.models.file_parameter import DbFileParameter
from lib.db.models.parameter_type import DbParameterType
from lib.env import Env
from lib.imaging_lib.nifti_pic import create_nifti_preview_picture


def report(event, **fields):
    print(json.dumps({'event': event, **fields}, sort_keys=True), flush=True)


def preview_valid(db, file, root):
    value = db.execute(select(DbFileParameter.value).join(
        DbParameterType, DbParameterType.id == DbFileParameter.type_id).where(
            DbFileParameter.file_id == file.id,
            DbParameterType.name == 'check_pic_filename')).scalar_one_or_none()
    if not value:
        return False
    directory = (root / 'pic').resolve()
    path = (directory / value).resolve()
    if not path.is_relative_to(directory):
        raise ValueError('Registered preview escapes the preview directory')
    if not path.is_file():
        return False
    try:
        with Image.open(path) as image:
            if image.format != 'PNG':
                return False
            image.verify()
    except (OSError, SyntaxError):
        return False
    return True


def cache_content(path, root, site, dataset, cache_url, timeout=18000):
    clone = root / 'assembly_bids' / site / dataset
    objects = (clone / '.git/annex/objects').resolve()
    target = path.resolve()
    if not target.is_relative_to(objects) or not re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9._+-]*', target.name):
        raise ValueError('Registered MRI does not resolve to the approved dataset annex')
    if not cache_url.startswith(('http://', 'https://')):
        raise ValueError('An approved HTTP(S) cache URL is required')
    deadline = time.monotonic() + timeout
    while True:
        request = urllib.request.Request(
            cache_url.rstrip('/') + '/content',
            data=json.dumps({'site': site, 'dataset': dataset, 'key': target.name}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=60) as response:
            status = json.load(response)
        if status.get('state') == 'present':
            if not path.is_file():
                raise ValueError('Cache reported present but registered MRI is unreadable')
            return
        if status.get('state') != 'fetching':
            raise ValueError('MRI cache fetch failed: ' + str(status.get('message', status)))
        if time.monotonic() >= deadline:
            raise TimeoutError('MRI cache fetch timed out; inspect the cache and site journals')
        time.sleep(5)


def registered_files(db, view, root):
    relative = view.relative_to(root)
    files = db.scalars(select(DbFile).order_by(DbFile.id)).all()
    return [file for file in files if file.path.is_relative_to(relative)
            and str(file.path).endswith(('.nii', '.nii.gz'))]


def require_idle_import(row):
    if row is None or row['mode'] != 'import' or row['status'] not in (
            'succeeded', 'failed', 'updated'):
        raise ValueError('Dataset must have an idle existing LORIS import')


def repair(engine, view, root, site, dataset, cache_url, expected_count, apply=False):
    if version('loris-bids-importer') != '29.0.0':
        raise ValueError('Preview repair requires the verified LORIS-MRI 29.0.0')
    if not view.is_relative_to(root / 'assembly_bids') or view.is_symlink():
        raise ValueError('Registered import view must be inside assembly_bids')
    with Session(engine) as db, tempfile.TemporaryDirectory(prefix='loris-previews-') as temporary:
        files = registered_files(db, view, root)
        if len(files) != expected_count or not files:
            raise ValueError(f'Expected {expected_count} registered MRI files; found {len(files)}')
        original = [(file.id, file.session_id, str(file.path)) for file in files]
        missing = [file for file in files if not preview_valid(db, file, root)]
        report('preview-plan', registered=len(files), missing=len(missing), apply=apply)
        if not apply:
            return len(missing)
        env = Env(engine, db, 'bids_previews', None, Path(temporary),
                  Path(temporary) / 'repair.log', False, [])
        for number, file in enumerate(missing, 1):
            report('preview-fetch', file_id=file.id, number=number, total=len(missing))
            cache_content(root / file.path, root, site, dataset, cache_url)
            create_nifti_preview_picture(env, file)
            if not preview_valid(db, file, root):
                raise ValueError(f'Preview verification failed for file ID {file.id}')
            report('preview-repaired', file_id=file.id, number=number, total=len(missing))
        db.expire_all()
        current = registered_files(db, view, root)
        if [(file.id, file.session_id, str(file.path)) for file in current] != original:
            raise ValueError('Registered MRI identities changed during preview repair')
        if any(not preview_valid(db, file, root) for file in current):
            raise ValueError('Some registered MRI previews remain missing or invalid')
        report('preview-complete', registered=len(current), valid=len(current), repaired=len(missing))
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--expected-count', required=True, type=int)
    parser.add_argument('--cache-url', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
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
                raise ValueError('An import or preview repair is active; wait before retrying')
            row = runtime.get_dataset(engine, runtime.dataset_id(site, args.dataset))
            require_idle_import(row)
            clone = runtime.ROOT / 'assembly_bids' / site / args.dataset
            imported = subprocess.run(
                ['git', '-C', str(clone), 'rev-parse', 'refs/re-move/imported'],
                check=True, capture_output=True, text=True).stdout.strip()
            if imported != args.commit:
                raise ValueError('Imported marker does not match the approved preview repair commit')
            report('preview-import-verified', commit=imported, ledger_status=row['status'])
            repair(engine, Path(row['view_path']), runtime.ROOT, site, args.dataset,
                   args.cache_url, args.expected_count, args.apply)
    finally:
        engine.dispose()


if __name__ == '__main__':
    os.umask(0o022)
    main()
