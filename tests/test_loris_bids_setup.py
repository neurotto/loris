import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import Column, Integer, MetaData, Table, create_engine, func, inspect, select
from sqlalchemy.orm import Session


SCRIPT = Path(__file__).resolve().parents[1] / 'charts/loris/files/bids-setup.py'
spec = importlib.util.spec_from_file_location('bids_setup', SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        models = [setup.DbProject, setup.DbSite, setup.DbCohort, setup.DbProjectCohort,
                  setup.DbUser, setup.DbUserProject, setup.DbUserSite, setup.DbVisit,
                  setup.DbVisitWindow, setup.DbConfigSetting, setup.DbConfig]
        setup.DbProject.metadata.create_all(self.engine, tables=[model.__table__ for model in models])
        Table('visit_project_cohort_rel', MetaData(),
              Column('VisitProjectCohortRelID', Integer, primary_key=True),
              Column('VisitID', Integer), Column('ProjectCohortRelID', Integer)).create(self.engine)
        with Session(self.engine) as db, db.begin():
            db.add(setup.DbUser(username='admin'))
            for name in ('dataDirBasepath', 'default_bids_vl'):
                db.add(setup.DbConfigSetting(name=name))

    def configure(self, db):
        return setup.configure(db, 'Penthera_3T', 'PEN3', 'scilus', 'SCIL',
                               'Penthera-3T', 12, {'001', '002'}, '000', 'admin')

    def test_configuration_is_repeatable_and_does_not_register_scans(self):
        for _ in range(2):
            with Session(self.engine) as db, db.begin():
                self.assertEqual(self.configure(db)['configured_visits'], 3)
        with Session(self.engine) as db:
            for model in (setup.DbProject, setup.DbSite, setup.DbCohort, setup.DbProjectCohort):
                self.assertEqual(db.scalar(select(func.count()).select_from(model)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(setup.DbVisit)), 3)
            self.assertEqual(db.scalar(select(setup.DbCohort.recruitment_target)), 12)
            self.assertEqual(db.scalar(select(setup.DbSite.alias)), 'SCIL')
        self.assertNotIn('candidate', inspect(self.engine).get_table_names())

    def test_conflicting_configuration_rolls_back(self):
        with Session(self.engine) as db, db.begin():
            db.add(setup.DbProject(name='Another', alias='PEN3'))
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            with Session(self.engine) as db, db.begin():
                self.configure(db)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(setup.DbSite)), 0)

    def test_invalid_alias_is_rejected(self):
        with Session(self.engine) as db:
            with self.assertRaises(ValueError):
                setup.configure(db, 'Penthera_3T', 'PEN3', 'scilus', 'scilus',
                                'Penthera-3T', 12, set(), '000', 'admin')

    def test_unspecified_target_is_null_and_does_not_erase_existing_target(self):
        with Session(self.engine) as db, db.begin():
            setup.configure(db, 'Optional', 'OPT', 'nandou', 'NAND',
                            'Dataset', None, set(), '000', 'admin')
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(setup.DbProject.recruitement_target)))
            self.assertIsNone(db.scalar(select(setup.DbCohort.recruitment_target)))
        for target in (12, None):
            with Session(self.engine) as db, db.begin():
                setup.configure(db, 'Optional', 'OPT', 'nandou', 'NAND',
                                'Dataset', target, set(), '000', 'admin')
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(setup.DbProject.recruitement_target)), 12)
            self.assertEqual(db.scalar(select(setup.DbCohort.recruitment_target)), 12)

    def test_invalid_targets_are_rejected(self):
        for target in (0, -1, True, '12'):
            with self.subTest(target=target), Session(self.engine) as db:
                with self.assertRaisesRegex(ValueError, 'target'):
                    setup.configure(db, 'Optional', 'OPT', 'nandou', 'NAND',
                                    'Dataset', target, set(), '000', 'admin')

    def test_existing_target_conflict_is_not_silently_ignored(self):
        with Session(self.engine) as db, db.begin():
            self.configure(db)
        with self.assertRaisesRegex(ValueError, 'target conflicts'):
            with Session(self.engine) as db, db.begin():
                setup.configure(db, 'Penthera_3T', 'PEN3', 'scilus', 'SCIL',
                                'Penthera-3T', 42, set(), '000', 'admin')

    def test_installer_placeholder_is_replaced_but_custom_paths_are_not(self):
        with Session(self.engine) as db, db.begin():
            setting = db.scalar(select(setup.DbConfigSetting).where(
                setup.DbConfigSetting.name == 'dataDirBasepath'))
            db.add(setup.DbConfig(setting_id=setting.id, value='/data/%PROJECTNAME%/data/'))
        with Session(self.engine) as db, db.begin():
            self.configure(db)
            config = db.scalar(select(setup.DbConfig).join(setup.DbConfigSetting).where(
                setup.DbConfigSetting.name == 'dataDirBasepath'))
            self.assertEqual(config.value, '/data/loris/data')
            config.value = '/another/data'
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            with Session(self.engine) as db, db.begin():
                self.configure(db)

    def test_module_activation_requires_empty_205(self):
        class Response:
            def __init__(self, status, body):
                self.status, self.body = status, body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.body

        class Opener:
            def __init__(self):
                self.responses = iter([
                    Response(200, b'{}'),
                    Response(200, b'{"Data":[["api","", "Y"],["imaging_browser","", "N"],'
                                  b'["brainbrowser","", "Y"]]}'),
                    Response(200, b'<html>login</html>'),
                ])

            def open(self, request, timeout):
                return next(self.responses)

        with patch.object(setup.urllib.request, 'build_opener', return_value=Opener()):
            with self.assertRaisesRegex(ValueError, 'HTTP 205'):
                setup.activate_modules('http://loris', 'admin', 'fixture-not-a-secret')

    def test_module_activation_reenables_disabled_api(self):
        class Response:
            def __init__(self, status, body):
                self.status, self.body = status, body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.body

        class Opener:
            def __init__(self):
                self.patched = []
                self.responses = iter([
                    Response(200, b'{}'),
                    Response(200, b'{"Data":[["api","", "N"],["imaging_browser","", "Y"],'
                                  b'["brainbrowser","", "Y"]]}'),
                    Response(205, b''),
                    Response(200, b'{"Data":[["api","", "Y"],["imaging_browser","", "Y"],'
                                  b'["brainbrowser","", "Y"]]}'),
                ])

            def open(self, request, timeout):
                if request.get_method() == 'PATCH':
                    self.patched.append(request.full_url)
                return next(self.responses)

        opener = Opener()
        with patch.object(setup.urllib.request, 'build_opener', return_value=opener):
            setup.activate_modules('http://loris', 'admin', 'fixture-not-a-secret')
        self.assertEqual(opener.patched, ['http://loris/module_manager/modules/api'])

    def test_module_activation_requires_api_module(self):
        class Response:
            def __init__(self, status, body):
                self.status, self.body = status, body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.body

        class Opener:
            def __init__(self):
                self.responses = iter([
                    Response(200, b'{}'),
                    Response(200, b'{"Data":[["imaging_browser","", "Y"],["brainbrowser","", "Y"]]}'),
                ])

            def open(self, request, timeout):
                return next(self.responses)

        with patch.object(setup.urllib.request, 'build_opener', return_value=Opener()):
            with self.assertRaisesRegex(ValueError, 'not available'):
                setup.activate_modules('http://loris', 'admin', 'fixture-not-a-secret')


if __name__ == '__main__':
    unittest.main()