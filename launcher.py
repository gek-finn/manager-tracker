"""Local startup only: no dependency installation and no changes to ticket data."""
import json
import hashlib
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROJECT_ID = hashlib.sha256(str(ROOT.resolve()).casefold().encode('utf-8')).hexdigest()


def ready(url, allow_legacy=False):
    try:
        with HTTP.open(url, timeout=0.5) as response:
            html = response.read(256 * 1024).decode('utf-8')
            project = response.headers.get('X-Manager-Tracker-Project')
            identity = project == PROJECT_ID or (allow_legacy and project is None)
            return identity and response.status == 200 and 'Manager Tracker' in html and 'Контроль клиентских заявок' in html
    except (OSError, urllib.error.URLError, UnicodeError):
        return False


def open_browser(url):
    print(f'Приложение готово: {url}', flush=True)
    try:
        if webbrowser.open(url):
            return
    except Exception as error:
        print(f'Не удалось открыть браузер: {error}', flush=True)
    print(f'Откройте адрес вручную: {url}', flush=True)


class InstanceLock:
    """OS-owned lock is released even after a crash; the file itself may remain."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('a+b')
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b' ')
            self.file.flush()
        self.owned = False

    def acquire(self):
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owned = True
            return True
        except OSError:
            return False

    def close(self):
        self.file.close()


def main(app_factory=None, runtime_dir=None):
    runtime = Path(runtime_dir) if runtime_dir else ROOT / '.runtime'
    lock = InstanceLock(runtime / 'launcher.lock')
    state = runtime / 'server.json'
    try:
        if not lock.acquire():
            # Another click may arrive while the first process is still starting.
            for _ in range(60):
                try:
                    url = json.loads(state.read_text(encoding='utf-8'))['url']
                    if url in ('http://127.0.0.1:5000', 'http://127.0.0.1:5002') and ready(url):
                        print('Manager Tracker уже запущен. Открываю существующий экземпляр.', flush=True)
                        open_browser(url)
                        return 0
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                time.sleep(0.1)
            print('Другой экземпляр запускается или не отвечает. Проверьте его окно консоли. Новый сервер не запущен.', flush=True)
            return 1

        state.unlink(missing_ok=True)
        # Recognize an instance started with the previous app.py, without our lock.
        for port in (5000, 5002):
            url = f'http://127.0.0.1:{port}'
            if ready(url):
                print('Manager Tracker уже работает на этом порту.', flush=True)
                open_browser(url)
                return 0
            if ready(url, allow_legacy=True):
                print(f'По адресу {url} работает прежняя версия Manager Tracker без идентификатора проекта. '
                      'Остановите её в её окне консоли (Ctrl+C) и повторите запуск. Второй экземпляр не запущен.', flush=True)
                return 1

        from app import create_app, serve
        application = (app_factory or create_app)()
        original_wsgi = application.wsgi_app

        def identified_wsgi(environ, start_response):
            def identified_response(status, headers, exc_info=None):
                return start_response(status, headers + [('X-Manager-Tracker-Project', PROJECT_ID)], exc_info)
            return original_wsgi(environ, identified_response)

        application.wsgi_app = identified_wsgi
        stop = threading.Event()
        worker = None

        def wait_until_ready(url):
            for _ in range(100):
                if stop.is_set():
                    return
                if ready(url):
                    if not stop.is_set():
                        state.write_text(json.dumps({'url': url}), encoding='utf-8')
                        open_browser(url)
                    return
                stop.wait(0.1)
            print(f'Сервер не ответил вовремя. Проверьте ошибки в консоли; адрес: {url}', flush=True)

        def on_bound(url):
            nonlocal worker
            worker = threading.Thread(target=wait_until_ready, args=(url,), daemon=True)
            worker.start()

        try:
            serve(application, on_bound=on_bound)
        finally:
            stop.set()
            if worker:
                worker.join(timeout=2)
            state.unlink(missing_ok=True)
        return 0
    finally:
        lock.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'Не удалось запустить Manager Tracker: {error}', flush=True)
        raise SystemExit(1)
