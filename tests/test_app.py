import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import urllib.parse
import http.cookiejar
from test_support import TEST_LOGIN,TEST_PASSWORD
from contextlib import closing
from pathlib import Path

from app import serve
from test_support import create_app, authenticated_client


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'test.sqlite3')
        self.config = dict(TESTING=True, DATABASE=self.path, SECRET_KEY='test-key')
        self.app = create_app(self.config)
        self.client = authenticated_client(self.app)

    def payload(self):
        html = self.client.get('/tickets/new').get_data(as_text=True)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                manager_id = db.execute("INSERT INTO managers(name) VALUES ('Тест')").lastrowid
        return dict(csrf_token=re.search('name="csrf_token" value="([^"]+)"', html)[1],
                    submission_key=re.search('name="submission_key" value="([^"]+)"', html)[1],
                    client='Тестовая компания', contact='test@example.test',
                    description='<script>alert(1)</script>', manager_id=str(manager_id),
                    status='Новая', priority='Высокий', next_action='2026-12-10T15:30')

    def count(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]

    def test_full_lifecycle_and_restart(self):
        data = self.payload()
        result = self.client.post('/tickets/new', data=data)
        self.assertEqual(result.status_code, 303)
        location = result.headers['Location']
        task_html = self.client.get(location + '/tasks/new').get_data(as_text=True)
        task_key = re.search('name="submission_key" value="([^"]+)"', task_html)[1]
        self.assertEqual(self.client.post(location + '/tasks/new', data=dict(
            csrf_token=data['csrf_token'], submission_key=task_key, title='Следующее действие',
            due_at='2026-12-10T15:30', manager_id=data['manager_id'])).status_code, 303)
        html = self.client.get(location).get_data(as_text=True)
        self.assertIn('Тестовая компания', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>alert', html)
        self.assertIn('10.12.2026 15:30', html)
        self.assertIn('Тестовая компания', self.client.get('/').get_data(as_text=True))
        self.assertEqual(self.client.post('/tickets/new', data=data).headers['Location'], location)
        self.assertEqual(self.count(), 1)
        data.update(client='Изменённый клиент', status='В работе', description='Обновлено')
        self.assertEqual(self.client.post(location + '/edit', data=data).status_code, 303)
        self.assertEqual(self.client.post(location + '/edit', data=data).status_code, 303)
        self.assertEqual(self.count(), 1)
        restarted = authenticated_client(create_app(self.config))
        html = restarted.get(location).get_data(as_text=True)
        self.assertIn('Изменённый клиент', html)
        self.assertIn('В работе', html)
        self.assertIn('Обновлено', html)
        self.assertEqual(self.client.get(location + '/delete').status_code, 200)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.client.post(location + '/delete', data=data).status_code, 303)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.client.get(location).status_code, 404)

    def test_validation_and_csrf(self):
        data = self.payload()
        for field, invalid in [('client', '  '), ('description', ''), ('status', 'Ошибка'),
                               ('priority', 'Ошибка'),
                               ('client', 'x' * 201), ('submission_key', 'bad')]:
            with self.subTest(field=field):
                self.assertEqual(self.client.post('/tickets/new', data={**data, field: invalid}).status_code, 422)
                self.assertEqual(self.count(), 0)
        self.assertEqual(self.client.post('/tickets/new', data={**data, 'csrf_token': 'bad'}).status_code, 400)
        self.assertEqual(self.client.get('/tickets/999/edit').status_code, 404)
        self.assertEqual(self.client.get('/tickets/999/delete').status_code, 404)

    def test_optional_fields_and_sql_text(self):
        data = self.payload()
        data.update(client="Robert'); DROP TABLE tickets;--", contact='', manager_id='', next_action='')
        self.assertEqual(self.client.post('/tickets/new', data=data).status_code, 303)
        self.assertEqual(self.count(), 1)

    def test_port_fallback(self):
        calls = []

        class Server:
            def serve_forever(self):
                pass

            def server_close(self):
                pass

        def factory(host, port, app):
            calls.append((host, port))
            if port == 5000:
                raise OSError('busy')
            return Server()

        serve(self.app, factory)
        self.assertEqual(calls, [('127.0.0.1', 5000), ('127.0.0.1', 5002)])
        with self.assertRaises(SystemExit):
            serve(self.app, lambda *args: (_ for _ in ()).throw(OSError('busy')))

    def test_http_and_process_restart(self):
        self.client.post('/tickets/new', data=self.payload())
        script = (
            'import sys; sys.path.insert(0,"tests"); from test_support import create_app; '
            'from werkzeug.serving import make_server; '
            'app=create_app({"DATABASE":sys.argv[1]}); '
            'server=make_server("127.0.0.1",0,app); '
            'print(server.server_port,flush=True); server.serve_forever()'
        )
        for _ in range(2):
            process = subprocess.Popen([sys.executable, '-u', '-c', script, self.path],
                                       cwd=Path(__file__).resolve().parents[1],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                port = int(process.stdout.readline().strip())
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                login_html=opener.open(f'http://127.0.0.1:{port}/login').read().decode('utf-8')
                token=re.search('name="csrf_token" value="([^"]+)"',login_html)[1]
                opener.open(f'http://127.0.0.1:{port}/login',data=urllib.parse.urlencode(dict(csrf_token=token,login=TEST_LOGIN,password=TEST_PASSWORD,next='/')).encode()).close()
                for path in ('/',  '/tickets/1', '/tickets/1/edit', '/tickets/1/delete',
                             '/tickets/new', '/static/style.css', '/static/app.js'):
                    with opener.open(f'http://127.0.0.1:{port}{path}', timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        if path == '/tickets/1':
                            self.assertIn('Тестовая компания', response.read().decode('utf-8'))
            finally:
                process.terminate()
                process.communicate(timeout=10)
        self.assertEqual(self.count(), 1)


if __name__ == '__main__':
    unittest.main()
