import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app import MANAGER_MIGRATION, migrate_database
from test_support import create_app, authenticated_client

LEGACY_SCHEMA = '''CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
    client TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '', description TEXT NOT NULL,
    manager TEXT, status TEXT NOT NULL, priority TEXT NOT NULL,
    next_action TEXT NOT NULL DEFAULT '', submission_key TEXT NOT NULL UNIQUE)'''


def legacy_database(path, names):
    with closing(sqlite3.connect(path)) as db:
        db.execute(LEGACY_SCHEMA)
        with db:
            for index, name in enumerate(names):
                db.execute('''INSERT INTO tickets
                    (created_at,client,description,manager,status,priority,next_action,submission_key)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    ('2026-11-01T09:00', f'Тест-{index}', 'Тест', name,
                     'В работе', 'Средний', '2026-11-02T12:00', str(index)))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.app = create_app(dict(TESTING=True, DATABASE=str(self.path), SECRET_KEY='test'))
        self.client = authenticated_client(self.app)
        html = self.client.get('/managers/new').get_data(as_text=True)
        self.token = re.search('name="csrf_token" value="([^"]+)"', html)[1]

    def post(self, path, **data):
        return self.client.post(path, data={'csrf_token': self.token, **data})

    def rows(self, sql, args=()):
        with closing(sqlite3.connect(self.path)) as db:
            return list(db.execute(sql, args))

    def add(self, name):
        self.assertEqual(self.post('/managers/new', name=name).status_code, 303)
        return self.rows('SELECT id FROM managers ORDER BY id DESC LIMIT 1')[0][0]

    def ticket(self, manager_id='', suffix=''):
        html = self.client.get('/tickets/new').get_data(as_text=True)
        return dict(submission_key=re.search('name="submission_key" value="([^"]+)"', html)[1],
                    client='Тест'+suffix, description='Потребность', contact='', manager_id=str(manager_id),
                    status='В работе', priority='Средний', next_action='2026-11-01T09:00')

    def test_add_rename_activity_and_no_delete(self):
        manager_id = self.add('  Анна  ')
        self.assertEqual(self.rows('SELECT name, active FROM managers'), [('Анна', 1)])
        self.assertEqual(self.post(f'/managers/{manager_id}/edit', name='Елена').status_code, 303)
        for active in ('0', '0', '1'):
            self.assertEqual(self.post(f'/managers/{manager_id}/activity', active=active).status_code, 303)
            self.assertEqual(self.rows('SELECT active FROM managers')[0][0], int(active))
        self.assertEqual(self.client.get(f'/managers/{manager_id}/delete').status_code, 404)
        self.assertEqual(self.client.get(f'/managers/{manager_id}/activity').status_code, 405)

    def test_names_required_limited_and_normalized_duplicates(self):
        anna = self.add('Анна')
        boris = self.add('Борис')
        self.post(f'/managers/{anna}/activity', active='0')
        for name in ('', ' \t ', 'a'*101, 'АННА', ' анна '):
            with self.subTest(name=name):
                self.assertEqual(self.post('/managers/new', name=name).status_code, 422)
        for name in ('', 'x'*101, ' анНА '):
            self.assertEqual(self.post(f'/managers/{boris}/edit', name=name).status_code, 422)
        self.assertEqual(len(self.rows('SELECT id FROM managers')), 2)

    def test_assignment_rename_and_unassignment_survive_restart(self):
        manager_id = self.add('Анна')
        data = self.ticket(manager_id)
        response = self.post('/tickets/new', **data)
        self.assertEqual(response.status_code, 303)
        location = response.headers['Location']
        self.post(f'/managers/{manager_id}/edit', name='Новое имя')
        for path in ('/', location):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('Новое имя', html)
        self.assertEqual(self.rows('SELECT manager_id FROM tickets'), [(manager_id,)])
        data['manager_id'] = ''
        self.assertEqual(self.post(location+'/edit', **data).status_code, 303)
        create_app(dict(TESTING=True, DATABASE=str(self.path)))
        self.assertEqual(self.rows('SELECT manager_id FROM tickets'), [(None,)])

    def test_inactive_current_allowed_other_inactive_rejected(self):
        first, other = self.add('Анна'), self.add('Борис')
        data = self.ticket(first)
        location = self.post('/tickets/new', **data).headers['Location']
        for manager_id in (first, other):
            self.post(f'/managers/{manager_id}/activity', active='0')
        html = self.client.get(location+'/edit').get_data(as_text=True)
        self.assertIn(f'value="{first}" selected', html)
        self.assertIn('Неактивен', html)
        self.assertNotIn(f'value="{other}"', html)
        self.assertEqual(self.post(location+'/edit', **data).status_code, 303)
        data['manager_id'] = str(other)
        self.assertEqual(self.post(location+'/edit', **data).status_code, 422)
        self.assertEqual(self.post('/tickets/new', **self.ticket(first)).status_code, 422)
        for value in ('9999', '-1', 'abc', "1 OR 1=1", '9'*500):
            self.assertEqual(self.post('/tickets/new', **self.ticket(value)).status_code, 422)
        for path in ('/', location):
            self.assertIn('Неактивен', self.client.get(path).get_data(as_text=True))
        self.assertEqual(self.rows('SELECT manager_id FROM tickets'), [(first,)])
        self.post(f'/managers/{other}/activity', active='1')
        self.assertEqual(self.post(location+'/edit', **data).status_code, 303)

    def test_filter_includes_active_and_assigned_inactive(self):
        first, inactive_empty, active_empty = self.add('Анна'), self.add('Борис'), self.add('Вера')
        location = self.post('/tickets/new', **self.ticket(first)).headers['Location']
        html = self.client.get(location+'/tasks/new').get_data(as_text=True)
        key = re.search('name="submission_key" value="([^"]+)"',html)[1]
        self.assertEqual(self.post(location+'/tasks/new',submission_key=key,title='Тест срока',manager_id=str(first),due_at='2026-11-01T09:00').status_code,303)
        for value in (first, inactive_empty):
            self.post(f'/managers/{value}/activity', active='0')
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn(f'value="{first}"', html)
        self.assertNotIn(f'value="{inactive_empty}"', html)
        self.assertIn(f'value="{active_empty}"', html)
        query = dict(manager=str(first), status='В работе', overdue='overdue')
        for _ in range(2):
            html = self.client.get('/', query_string=query).get_data(as_text=True)
            self.assertIn('Тест', html)
            self.assertIn(f'value="{first}" selected', html)
        self.assertIn('По выбранным условиям', self.client.get('/?manager=unassigned').get_data(as_text=True))
        self.assertEqual(self.client.get('/?manager=invalid').status_code, 200)

    def test_csrf_escaping_and_wrong_ids(self):
        name = '<script>alert(1)</script>'
        manager_id = self.add(name)
        for path in ('/managers', f'/managers/{manager_id}/edit', '/tickets/new', '/'):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('&lt;script&gt;', html)
            self.assertNotIn(name, html)
        for path in ('/managers/new', f'/managers/{manager_id}/edit', f'/managers/{manager_id}/activity'):
            self.assertEqual(self.client.post(path, data=dict(name='bad', active='0')).status_code, 400)
        self.assertEqual(self.post(f'/managers/{manager_id}/activity', active='bad').status_code, 400)
        self.assertEqual(self.post('/managers/999/edit', name='bad').status_code, 404)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'legacy.sqlite3'
        self.names = ['Анна', 'Анна', 'анна', ' Анна ', '', ' \t ', None, 'x'*150]
        legacy_database(self.path, self.names)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(tickets)') if row[1] != 'manager_id']
            return list(db.execute('SELECT '+','.join(columns)+' FROM tickets ORDER BY id'))

    def test_exact_import_preserves_every_old_column_and_is_repeatable(self):
        before = self.snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            migrate_database(db)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            managers = list(db.execute('SELECT id,name,active FROM managers ORDER BY id'))
            self.assertEqual([row[1] for row in managers], ['Анна','анна',' Анна ','x'*150])
            self.assertTrue(all(row[2] == 1 for row in managers))
            ids = [row[0] for row in db.execute('SELECT manager_id FROM tickets ORDER BY id')]
            self.assertEqual(ids, [1,1,2,3,None,None,None,4])
            migrate_database(db)
            self.assertEqual(list(db.execute('SELECT id,name,active FROM managers ORDER BY id')), managers)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=?',(MANAGER_MIGRATION,)).fetchone()[0], 1)
        self.assertEqual(self.snapshot(), before)

    def test_no_reimport_or_restoration_after_rename_and_clear(self):
        with closing(sqlite3.connect(self.path)) as db:
            migrate_database(db)
            with db:
                db.execute("UPDATE managers SET name='Новое имя' WHERE id=1")
                db.execute('UPDATE tickets SET manager_id=NULL WHERE id=1')
            migrate_database(db)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM managers').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT name FROM managers WHERE id=1').fetchone()[0], 'Новое имя')
            self.assertIsNone(db.execute('SELECT manager_id FROM tickets WHERE id=1').fetchone()[0])
            self.assertEqual(db.execute('SELECT manager FROM tickets WHERE id=1').fetchone()[0], 'Анна')

    def test_failure_rolls_back_schema_and_import(self):
        before = self.snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            def fail_import(action, arg1, arg2, database, source):
                if action == sqlite3.SQLITE_UPDATE and arg1 == 'tickets' and arg2 == 'manager_id':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(fail_import)
            with self.assertRaises(sqlite3.DatabaseError):
                migrate_database(db)
            db.set_authorizer(None)
            self.assertNotIn('manager_id', [row[1] for row in db.execute('PRAGMA table_info(tickets)')])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='managers'").fetchone())
            migrate_database(db)
        self.assertEqual(self.snapshot(), before)

    def test_imported_duplicate_or_long_name_can_stay_or_toggle(self):
        app = create_app(dict(TESTING=True, DATABASE=str(self.path), SECRET_KEY='test'))
        client = authenticated_client(app)
        html = client.get('/managers').get_data(as_text=True)
        token = re.search('name="csrf_token" value="([^"]+)"', html)[1]
        for manager_id, name in ((1,'Анна'),(2,'анна'),(3,' Анна '),(4,'x'*150)):
            self.assertEqual(client.post(f'/managers/{manager_id}/edit', data=dict(csrf_token=token,name=name)).status_code, 303)
            self.assertEqual(client.post(f'/managers/{manager_id}/activity', data=dict(csrf_token=token,active='0')).status_code, 303)
        self.assertEqual(client.post('/managers/4/edit', data=dict(csrf_token=token,name=' АННА ')).status_code, 422)
        for manager_id in (1,2,3):
            response = client.get('/', query_string=dict(manager=str(manager_id)))
            self.assertIn(f'value="{manager_id}" selected', response.get_data(as_text=True))
            self.assertIn(f'№{manager_id}', response.get_data(as_text=True))

    def test_empty_database_and_foreign_key_protection(self):
        empty = Path(self.temp.name) / 'empty.sqlite3'
        app = create_app(dict(TESTING=True, DATABASE=str(empty)))
        self.assertEqual(authenticated_client(app).get('/managers').status_code, 200)
        with closing(sqlite3.connect(empty)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM managers').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT version FROM schema_migrations').fetchone()[0], MANAGER_MIGRATION)
            self.assertTrue(any(row[2] == 'managers' for row in db.execute('PRAGMA foreign_key_list(tickets)')))


if __name__ == '__main__':
    unittest.main()
