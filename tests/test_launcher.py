"""Run the real BAT in isolated project copies; never open a real browser."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import venv

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows BAT integration')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project with spaces'
        self.project.mkdir()
        # Preserve running applications. Only isolated copies remap occupied ports.
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.primary = probe.getsockname()[1]
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', 5002))
            except OSError:
                probe.bind(('127.0.0.1', 0))
            self.fallback = probe.getsockname()[1]
        for name in ('app.py', 'task_features.py', 'event_features.py', 'automation_features.py', 'report_features.py', 'auth_features.py', 'config_features.py', 'setup_owner.py', 'launcher.py', 'запуск.bat', 'requirements.txt'):
            shutil.copy2(ROOT / name, self.project / name)
        for name in ('templates', 'static'):
            shutil.copytree(ROOT / name, self.project / name)
        for name in ('app.py', 'launcher.py'):
            path = self.project / name
            text = path.read_text(encoding='utf-8').replace('5000', str(self.primary)).replace('5002', str(self.fallback))
            path.write_text(text, encoding='utf-8')
        from test_support import TEST_HASH,TEST_LOGIN
        (self.project/'instance').mkdir()
        (self.project/'instance/local-owner.json').write_text(json.dumps(dict(SECRET_KEY='test-launcher-key-'*4,OWNER_LOGIN=TEST_LOGIN,OWNER_PASSWORD_HASH=TEST_HASH)),encoding='utf-8')
        # The callback asserts an actual successful HTTP response at browser-open time.
        (self.project / 'sitecustomize.py').write_text('''
import json, pathlib, sys, urllib.request, webbrowser
def browser(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response:
        assert response.status == 200
        html=response.read().decode('utf-8')
        assert 'Manager Tracker' in html and 'Вход владельца' in html
    with pathlib.Path(__file__).with_name('browser.jsonl').open('a', encoding='utf-8') as output:
        output.write(json.dumps({'url': url, 'python': sys.executable}) + '\\n')
    return True
webbrowser.open = browser
# Graceful cleanup of only the copied test server, without process enumeration.
import app, threading, time
from werkzeug.serving import make_server
original_serve = app.serve
def factory(*args):
    server = make_server(*args)
    def stop_when_requested():
        while not pathlib.Path(__file__).with_name('stop-test-server').exists():
            time.sleep(0.05)
        server.shutdown()
    threading.Thread(target=stop_when_requested, daemon=True).start()
    return server
def serve(application, on_bound=None):
    return original_serve(application, server_factory=factory, on_bound=on_bound)
app.serve = serve
''', encoding='utf-8')
        self.env = {**os.environ, 'PYTHONPATH': str(self.project), 'PYTHONIOENCODING': 'utf-8'}

    def reserve(self, port):
        sock = socket.socket()
        try:
            sock.bind(('127.0.0.1', port))
            sock.listen()
        except OSError:
            sock.close()
            self.skipTest(f'Port {port} is in use outside this test')
        self.addCleanup(sock.close)
        return sock

    def start_bat(self):
        log = (self.project / 'console.log').open('wb')
        process = subprocess.Popen(['cmd.exe', '/d', '/c', str(self.project / 'запуск.bat')],
                                   cwd=self.project, env=self.env,
                                   stdin=subprocess.PIPE, stdout=log, stderr=log)

        def cleanup():
            if process.poll() is None:
                (self.project / 'stop-test-server').touch()
            process.communicate(timeout=10)
            log.close()

        self.addCleanup(cleanup)
        return process

    def browser_calls(self, count=1):
        for _ in range(150):
            try:
                lines = (self.project / 'browser.jsonl').read_text(encoding='utf-8').splitlines()
                if len(lines) >= count:
                    return [json.loads(line) for line in lines]
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.fail((self.project / 'console.log').read_text(encoding='utf-8', errors='replace'))

    def test_incomplete_venv_system_fallback_and_second_click(self):
        self.reserve(self.fallback)
        probe = self.reserve(self.primary)
        probe.close()
        venv.EnvBuilder(with_pip=False).create(self.project / '.venv')
        first = self.start_bat()
        calls = self.browser_calls()
        self.assertEqual(calls[0]['url'], f'http://127.0.0.1:{self.primary}')
        self.assertNotIn('.venv', calls[0]['python'])
        state = (self.project / '.runtime' / 'server.json').read_text()
        second = subprocess.run(['cmd.exe', '/d', '/c', str(self.project / 'запуск.bat')],
                                cwd=self.project, env=self.env, input=b'\n', capture_output=True, timeout=15)
        self.assertEqual(second.returncode, 0, second.stdout.decode('utf-8', errors='replace'))
        self.assertEqual(self.browser_calls(2)[1]['url'], calls[0]['url'])
        self.assertEqual((self.project / '.runtime' / 'server.json').read_text(), state)
        self.assertIsNone(first.poll())
        # The blocked fallback also proves the second click did not start another server.

    def test_ready_venv_and_fallback_port(self):
        self.reserve(self.primary)
        probe = self.reserve(self.fallback)
        probe.close()
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(self.project / '.venv')
        self.start_bat()
        call = self.browser_calls()[0]
        self.assertIn('.venv', call['python'])
        self.assertEqual(call['url'], f'http://127.0.0.1:{self.fallback}')

    def test_both_ports_busy_and_no_browser(self):
        self.reserve(self.primary)
        self.reserve(self.fallback)
        process = self.start_bat()
        stdout, stderr = process.communicate(input=b'\n', timeout=15)
        self.assertEqual(process.returncode, 1)
        log = (self.project / 'console.log').read_text(encoding='utf-8', errors='replace')
        self.assertIn(f'Порты {self.primary} и {self.fallback} заняты', log)
        self.assertIn('Окно оставлено открытым', log)
        self.assertFalse((self.project / 'browser.jsonl').exists())

    def test_missing_dependencies_does_not_install(self):
        # Make all interpreter probes fail without altering any installed Python.
        self.env['PYTHONHOME'] = str(self.project / 'missing-python-home')
        process = self.start_bat()
        process.communicate(input=b'\n', timeout=15)
        self.assertEqual(process.returncode, 1)
        log = (self.project / 'console.log').read_text(encoding='utf-8', errors='replace')
        self.assertIn('Автоматическое скачивание при запуске отключено', log)
        self.assertIn('--timeout 15 --retries 0', log)
        self.assertFalse((self.project / '.venv').exists())
        self.assertFalse((self.project / 'browser.jsonl').exists())

    def test_missing_owner_configuration_instruction_no_auto_setup(self):
        (self.project/'instance/local-owner.json').unlink()
        process=self.start_bat();process.communicate(input=b'\n',timeout=15)
        self.assertEqual(process.returncode,1)
        log=(self.project/'console.log').read_text(encoding='utf-8',errors='replace')
        self.assertIn('python setup_owner.py',log)
        self.assertFalse((self.project/'instance/local-owner.json').exists())
        self.assertFalse((self.project/'instance/tracker.sqlite3').exists())
        self.assertFalse((self.project/'browser.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
