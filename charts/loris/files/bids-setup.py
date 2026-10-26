"""Configure LORIS registration metadata and imaging modules without importing scans.

Database helpers operate in a caller-owned SQLAlchemy transaction. Standalone
execution reads BIDS_* settings and Secret-backed credentials from the environment,
commits metadata, creates output directories, and activates modules over HTTP.
Those database/filesystem/HTTP phases are deliberately separate operations.
"""

import http.cookiejar
import json
import os
from pathlib import Path
import urllib.request

from sqlalchemy import MetaData, Table, insert, select
from sqlalchemy.orm import Session
from lib.config_file import load_config
from lib.db.connect import get_database_engine
from lib.db.models.cohort import DbCohort
from lib.db.models.config import DbConfig
from lib.db.models.config_setting import DbConfigSetting
from lib.db.models.project import DbProject
from lib.db.models.project_cohort import DbProjectCohort
from lib.db.models.site import DbSite
from lib.db.models.user import DbUser
from lib.db.models.user_project import DbUserProject
from lib.db.models.user_site import DbUserSite
from lib.db.models.visit import DbVisit
from lib.db.models.visit_window import DbVisitWindow


def ensure(db, model, match, values=None):
    """Return a uniquely matching ORM row, inserting and flushing it if absent.

    values supplies creation-only fields; existing rows are not updated here.
    The caller owns transaction commit/rollback. Duplicate matches propagate as
    an ORM error rather than choosing an arbitrary row.
    """
    row = db.scalars(select(model).filter_by(**match)).one_or_none()
    if row is None:
        row = model(**match, **(values or {}))
        db.add(row)
        db.flush()

    return row


def configure(db, project_name, project_alias, site_name, site_alias, cohort_name,
              target, visits, default_visit, admin_user):
    """Ensure compatible project/site/cohort, access, visit, and global settings.

    Names and 1-4-character aliases must agree with existing registration records.
    Visits contains prefix-free session labels; default_visit is also registered.
    target is a positive integer or None: omission preserves existing targets,
    while a supplied conflicting target raises ValueError.

    Flush changes within the caller's transaction without committing or importing
    candidates/scans. Return a dictionary containing configured_visits. Global dataDirBasepath and
    default_bids_vl must be compatible; only the installer path placeholder may
    be replaced automatically. Serialize callers that share the same database.
    """
    if not (0 < len(project_alias) <= 4 and 0 < len(site_alias) <= 4):
        raise ValueError('Aliases must contain 1-4 characters')

    if target is not None and (type(target) is not int or target <= 0):
        raise ValueError('Recruitment target must be a positive integer or unspecified')

    project = db.scalars(select(DbProject).where(
        (DbProject.name == project_name) | (DbProject.alias == project_alias)
    )).one_or_none()
    if project is not None and (project.name != project_name or project.alias != project_alias):
        raise ValueError('Project name/alias conflicts with existing configuration')

    if project is not None and target is not None and project.recruitement_target not in (None, target):
        raise ValueError('Project recruitment target conflicts with existing configuration')

    project = project or ensure(db, DbProject, {'name': project_name},
                               {'alias': project_alias, 'recruitement_target': target})
    site = db.scalars(select(DbSite).where(
        (DbSite.name == site_name) | (DbSite.alias == site_alias)
    )).one_or_none()
    if site is not None and (site.name != site_name or site.alias != site_alias):
        raise ValueError('Site name/alias conflicts with existing configuration')

    site = site or ensure(db, DbSite, {'name': site_name},
                         {'alias': site_alias, 'mri_alias': site_alias, 'study_site': True})
    cohort = ensure(db, DbCohort, {'name': cohort_name},
                    {'use_edc': False, 'window_difference': 'optimal', 'recruitment_target': target})
    if target is not None and cohort.recruitment_target not in (None, target):
        raise ValueError('Cohort recruitment target conflicts with existing configuration')

    if target is not None:
        project.recruitement_target = target
        cohort.recruitment_target = target

    relation = ensure(db, DbProjectCohort, {'project_id': project.id, 'cohort_id': cohort.id})
    admin = db.scalars(select(DbUser).where(DbUser.username == admin_user)).one()
    ensure(db, DbUserProject, {'user_id': admin.id, 'project_id': project.id})
    ensure(db, DbUserSite, {'user_id': admin.id, 'site_id': site.id})
    visit_rel = Table('visit_project_cohort_rel', MetaData(), autoload_with=db.connection())
    for label in sorted(set(visits) | {default_visit}):
        if not label or len(label) > 200 or label.startswith('ses-'):
            raise ValueError('Visit labels must be nonempty, prefix-free, and at most 200 characters')

        visit = ensure(db, DbVisit, {'label': label}, {'name': 'BIDS ' + label})
        ensure(db, DbVisitWindow, {'visit_label': label})
        existing = db.execute(select(visit_rel.c.VisitProjectCohortRelID).where(
            visit_rel.c.VisitID == visit.id,
            visit_rel.c.ProjectCohortRelID == relation.id,
        )).scalar_one_or_none()
        if existing is None:
            db.execute(insert(visit_rel).values(VisitID=visit.id, ProjectCohortRelID=relation.id))

    for name, value in {'dataDirBasepath': '/data/loris/data', 'default_bids_vl': default_visit}.items():
        setting = db.scalars(select(DbConfigSetting).where(DbConfigSetting.name == name)).one()
        config = db.scalars(select(DbConfig).where(DbConfig.setting_id == setting.id)).one_or_none()
        if config is None:
            db.add(DbConfig(setting_id=setting.id, value=value))
        elif config.value == '/data/%PROJECTNAME%/data/' and name == 'dataDirBasepath':
            config.value = value
        elif config.value not in (None, '', value):
            raise ValueError('Existing ' + name + ' conflicts with the selected import configuration')
        else:
            config.value = value

    return {'configured_visits': len(set(visits) | {default_visit})}


REQUIRED_MODULES = ('api', 'imaging_browser', 'brainbrowser')


def activate_modules(url, username, password):
    """Authenticate to LORIS and ensure the API and imaging viewer modules are active.

    imaging_browser thumbnails and BrainBrowser volumes are fetched through the
    `api` module, so disabling it turns every image request into an HTML 404.
    Use a cookie-backed session and require the patched module endpoint's empty
    HTTP 205 response for activation. Re-read state to verify every module.
    HTTP/decoding errors propagate; missing modules or incompatible responses
    raise ValueError. Database metadata is not rolled back if this step fails.
    """
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, payload=None, method=None):
        """Send an authenticated JSON request and return HTTP status and body bytes."""
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url.rstrip('/') + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=30) as response:
            return response.status, response.read()

    request('/login/Authentication', {'login': 'true', 'command': 'login',
                                     'username': username, 'password': password})
    _, body = request('/module_manager/?format=json')
    modules = {row[0]: row[2] for row in json.loads(body)['Data']}

    for name in REQUIRED_MODULES:
        if name not in modules or modules[name] not in ('Y', 'N'):
            raise ValueError('Required imaging module is not available')

        if modules[name] == 'N':
            status, body = request('/module_manager/modules/' + name, {'Active': 'Y'}, 'PATCH')
            if status != 205 or body:
                raise ValueError('Module activation did not return an empty HTTP 205')

    _, body = request('/module_manager/?format=json')
    current = {row[0]: row[2] for row in json.loads(body)['Data']}
    if any(current.get(name) != 'Y' for name in REQUIRED_MODULES):
        raise ValueError('Module activation verification failed')


def main():
    """Configure a read-only BIDS source using environment-backed registration values.

    Load the config.py database profile, discover visits, commit setup, create
    logs/pic output folders, and activate viewer modules. An absent or empty
    BIDS_RECRUITMENT_TARGET means unspecified. Print a scan-free setup summary.
    """
    source = Path(os.environ['BIDS_DIRECTORY'])
    if not os.statvfs(source).f_flag & os.ST_RDONLY:
        raise ValueError('Source must remain mounted read-only during setup')

    subjects = [entry for entry in source.glob('sub-*') if entry.is_dir()]
    visits = {entry.name[4:] for subject in subjects for entry in subject.glob('ses-*') if entry.is_dir()}
    engine = get_database_engine(load_config('config.py').mysql)
    with Session(engine) as db, db.begin():
        target = os.environ.get('BIDS_RECRUITMENT_TARGET', '')
        summary = configure(db, os.environ['BIDS_PROJECT'], os.environ['BIDS_PROJECT_ALIAS'],
                            os.environ['BIDS_SITE'], os.environ['BIDS_SITE_ALIAS'],
                            os.environ['BIDS_COHORT'], int(target) if target else None, visits,
                            os.environ['BIDS_DEFAULT_VISIT'], os.environ['LORIS_ADMIN_USER'])

    for directory in ('logs', 'pic'):
        (Path('/data/loris/data') / directory).mkdir(exist_ok=True)

    activate_modules('http://loris', os.environ['LORIS_ADMIN_USER'],
                     os.environ['LORIS_FRONTEND_ADMIN_PASSWORD'])
    print(json.dumps({**summary, 'setup_complete': True, 'scans_imported': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('LORIS setup failed: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__))
