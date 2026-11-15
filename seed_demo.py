"""Explicit, append-only demo seeding. No schema changes, migrations or deletion."""
import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid

from app import APP_TIMEZONE, MANAGER_MIGRATION, parse_deadline
from task_features import TASK_MIGRATION, task_overview
from automation_features import AUTOMATION_MIGRATION
from event_features import EVENT_MIGRATION, append_event, ticket_snapshot, task_snapshot
from launcher import InstanceLock, PROJECT_ID

ROOT = Path(__file__).resolve().parent
DATASET = 'manager-tracker-demo-v1'
NAMES = ['Демо Менеджер Север', 'Демо Менеджер Восток', 'Демо Менеджер Юг',
         'Демо Менеджер Запад', 'Демо Менеджер Архив']
DESCRIPTIONS = [
    'Запрос цены на базовый пакет сопровождения. Подготовить расчёт и условия оплаты.',
    'Консультация по выбору услуги для небольшой команды. Уточнить объём работ.',
    'Подбор пакета обучения сотрудников. Сравнить групповой и индивидуальный формат.',
    'Повторное обращение по обслуживанию оборудования. Уточнить состав комплекта.',
    'Клиент рассматривает предложение по настройке рабочего места. Обсудить варианты.',
    'Запрос стоимости печатных материалов. Подготовить три варианта тиража.',
    'Новый запрос на консультацию по организации поддержки. Сначала собрать требования.',
    'Подбор услуги сопровождения сайта. Определить удобное время следующего разговора.',
    'Подготовка расчёта стоимости аудита процессов. Уточнить количество подразделений.',
    'Повторное обращение за обучением. Согласовать состав участников и программу.',
    'Консультация по выбору тарифного пакета. Ответить на вопросы по срокам.',
    'Подбор услуги настройки отчётности. Подготовить описание доступных вариантов.',
    'Запрос цены на консультационный проект. Рассчитать базовый и расширенный объём.',
    'Согласование предложения по сопровождению. Уточнить условия запуска работ.',
    'Повторное обращение по пакету услуг. Следующее действие будет назначено после уточнений.',
    'Клиент обещал дать решение по коммерческому предложению. Запросить обратную связь.',
    'Ожидается решение клиента по обучению команды. Вернуться к обсуждению через неделю.',
    'Клиент сравнивает варианты обслуживания. Согласовано повторное обсуждение стоимости.',
    'Ожидание внутреннего согласования бюджета клиента. Дата следующего контакта не определена.',
    'Клиент запросил паузу для выбора услуги. Срок следующего действия пока не назначен.',
    'Консультация завершена, клиент получил расчёт и ответы на вопросы. Обращение закрыто.',
    'Подбор услуги завершён. Клиент подтвердил выбранный вариант, заявка закрыта.',
    'Повторное обращение обработано. Требуемая информация передана, заявка закрыта.',
    'Клиент принял решение отложить заказ. Договорились закрыть текущее обращение.',
    'Запрос цены обработан, предложение рассмотрено. Дальнейших действий не требуется.',
]


def ticket_key(index):
    return hashlib.sha256(f'{DATASET}:ticket:{index}'.encode()).hexdigest()


def make_plan(now):
    now = now.astimezone(APP_TIMEZONE)
    result = []
    for i, description in enumerate(DESCRIPTIONS):
        status = 'Новая' if i < 8 else 'В работе' if i < 15 else 'Ожидает клиента' if i < 20 else 'Закрыта'
        if i in (0, 1, 2, 8, 9, 10, 15) or i >= 20:
            deadline = now - timedelta(days=3 + i % 4)
        elif i in (3, 4, 5, 11, 12, 13, 16, 17):
            deadline = now + timedelta(days=7 + i % 5)
        else:
            deadline = None
        result.append(dict(client=f'Демо Компания {i+1:02d}', contact=f'request{i+1:02d}@example.com',
                           description=description, status=status,
                           priority=('Низкий', 'Средний', 'Высокий')[i % 3],
                           next_action=deadline.replace(tzinfo=None).isoformat(timespec='minutes') if deadline else '',
                           created_at=(now-timedelta(days=40-i)).replace(tzinfo=None).isoformat(timespec='seconds'),
                           slot=i % 6 if i % 6 < 5 else None, submission_key=ticket_key(i)))
    return result


def backup_database(database, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Проверка целостности резервной копии не пройдена.')


def _write_manifest(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _summary(db, ids, now):
    tickets = [db.execute('SELECT * FROM tickets WHERE id=?', (value,)).fetchone() for value in ids]
    return dict(statuses=dict(Counter(t['status'] for t in tickets)),
                deadlines=dict(Counter(task_overview(t,db.execute('SELECT * FROM tasks WHERE ticket_id=?',(t['id'],)).fetchall(),now,parse_deadline)['deadline']['label'] for t in tickets)),
                priorities=dict(Counter(t['priority'] for t in tickets)),
                unassigned=sum(t['manager_id'] is None for t in tickets))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def seed_database(database, backup_root=None, now=None):
    """All additions in one transaction; an external receipt tracks exact IDs.

    Deterministic submission keys recover the receipt when possible. The receipt
    is written before commit, so a filesystem error rolls back all additions.
    After a crash an ambiguous receipt is kept for manual inspection, never used
    as permission to recreate missing/deleted records.
    """
    database = Path(database).resolve()
    if not database.is_file():
        raise RuntimeError('База не найдена. Инструмент не создаёт схему и не выполняет миграции.')
    now = (now or datetime.now(APP_TIMEZONE)).astimezone(APP_TIMEZONE)
    receipt = database.with_suffix('.demo-manifest.json')
    with closing(sqlite3.connect(database.as_uri()+'?mode=rw', uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        schema = list(db.execute('SELECT * FROM sqlite_master ORDER BY name'))
        if not all(db.execute('SELECT 1 FROM schema_migrations WHERE version=?',(version,)).fetchone() for version in (MANAGER_MIGRATION,TASK_MIGRATION,EVENT_MIGRATION,AUTOMATION_MIGRATION)):
            raise RuntimeError('Требуется завершённый этап 4Б. Миграции здесь не выполняются.')
        if receipt.exists():
            value = json.loads(receipt.read_text(encoding='utf-8'))
            if value.get('dataset') != DATASET or value.get('database') != str(database):
                raise RuntimeError('Список ID относится к другому набору или пути базы. Ничего не изменено.')
            # The receipt is authoritative even if the user removed demo records.
            # A pending receipt is accepted only when all committed keys exist.
            if value.get('state') == 'complete':
                if len(value.get('manager_ids', [])) != 5 or len(value.get('ticket_ids', [])) != 25:
                    raise RuntimeError('Повреждён список ID. Ничего не изменено.')
                value['missing_ticket_ids'] = [i for i in value['ticket_ids']
                                               if not db.execute('SELECT 1 FROM tickets WHERE id=?', (i,)).fetchone()]
                return dict(already_seeded=True, receipt=str(receipt), **value)

        keys = [ticket_key(i) for i in range(25)]
        existing = [db.execute('SELECT id,manager_id FROM tickets WHERE submission_key=?', (key,)).fetchone() for key in keys]
        if any(existing):
            if not all(existing):
                raise RuntimeError('Обнаружена часть набора без завершённого списка ID. Восстановите список; добавления отменены.')
            if receipt.exists():
                value = json.loads(receipt.read_text(encoding='utf-8'))
                if value.get('ticket_ids') != [row['id'] for row in existing]:
                    raise RuntimeError('Список ID не совпадает с набором. Ничего не изменено.')
            else:
                manager_ids = []
                for slot in range(5):
                    values = {row['manager_id'] for i, row in enumerate(existing) if i % 6 == slot}
                    if len(values) != 1 or None in values:
                        raise RuntimeError('Назначения набора изменены. Для повторного запуска восстановите исходный список ID.')
                    manager_ids.append(values.pop())
                if len(set(manager_ids)) != 5:
                    raise RuntimeError('Не удалось однозначно восстановить ID менеджеров. Ничего не изменено.')
                value = dict(dataset=DATASET, database=str(database), manager_ids=manager_ids,
                             ticket_ids=[row['id'] for row in existing], recovered=True)
            value['state'] = 'complete'
            _write_manifest(receipt, value)
            return dict(already_seeded=True, receipt=str(receipt), **value)
        if receipt.exists() and json.loads(receipt.read_text(encoding='utf-8')).get('state') != 'pending':
            raise RuntimeError('Список ID требует проверки. Ничего не изменено.')

        folder = Path(backup_root or ROOT/'backups') / (now.strftime('%Y-%m-%d_%H-%M-%S_demo-data_')+uuid.uuid4().hex[:8])
        backup = folder / database.name
        backup_database(database, backup)
        pending_written = False
        committed = False
        try:
            db.execute('BEGIN IMMEDIATE')
            old_managers = list(db.execute('SELECT * FROM managers ORDER BY id'))
            old_tickets = list(db.execute('SELECT * FROM tickets ORDER BY id'))
            old_events = list(db.execute('SELECT * FROM ticket_events ORDER BY id'))
            # Never reuse, rename or disable an existing manager, even a similarly named demo manager.
            if any(row['name'].strip().casefold() in {name.casefold() for name in NAMES} for row in old_managers):
                raise RuntimeError('Такие демонстрационные имена уже существуют без списка набора. Ничего не изменено.')
            manager_ids = [db.execute('INSERT INTO managers(name,active) VALUES (?,1)', (name,)).lastrowid for name in NAMES]
            ticket_ids = []
            for item in make_plan(now):
                ticket_ids.append(db.execute('''INSERT INTO tickets
                    (created_at,client,contact,description,manager_id,status,priority,next_action,submission_key)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (now.replace(tzinfo=None).isoformat(timespec='seconds'),item['client'],item['contact'],item['description'],
                     manager_ids[item['slot']] if item['slot'] is not None else None,
                     item['status'],item['priority'],'',item['submission_key'])).lastrowid)
                created_ticket=db.execute('SELECT * FROM tickets WHERE id=?',(ticket_ids[-1],)).fetchone()
                append_event(db,ticket_ids[-1],'ticket.created',now,
                             {'ticket':ticket_snapshot(db,created_ticket),'text':'Создано инструментом демонстрационного заполнения'})
                if item['next_action']:
                    closed = item['status']=='Закрыта'
                    task_cursor=db.execute('''INSERT INTO tasks(ticket_id,title,manager_id,due_at,state,created_at,finished_at,result,submission_key)
                        VALUES (?,?,?,?,?,?,?,?,?)''',
                        (ticket_ids[-1],'Следующее действие',manager_ids[item['slot']] if item['slot'] is not None else None,
                         item['next_action'],'Отменена' if closed else 'Открыта',now.isoformat(),
                         now.isoformat() if closed else None,'Демо: заявка закрыта' if closed else '',
                         hashlib.sha256(f'{DATASET}:task:{len(ticket_ids)-1}'.encode()).hexdigest()))
                    created_task=db.execute('SELECT * FROM tasks WHERE id=?',(task_cursor.lastrowid,)).fetchone()
                    append_event(db,ticket_ids[-1],'task.created',now,{'task':task_snapshot(db,created_task),
                        'text':'Создана демонстрационная задача в указанном состоянии'},task_id=created_task['id'])
            # Model a valid historical assignment: first assign while active, then disable.
            db.execute('UPDATE managers SET active=0 WHERE id=?', (manager_ids[-1],))
            require(len(manager_ids) == 5 and len(ticket_ids) == 25, 'Неверный размер набора.')
            require(db.execute('SELECT COUNT(*) FROM managers').fetchone()[0] == len(old_managers)+5,
                    'Неверное количество добавленных менеджеров.')
            require(db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0] == len(old_tickets)+25,
                    'Неверное количество добавленных заявок.')
            for table, rows in (('managers',old_managers),('tickets',old_tickets),('ticket_events',old_events)):
                require(rows == [db.execute(f'SELECT * FROM {table} WHERE id=?', (row['id'],)).fetchone() for row in rows],
                        'Прежние записи изменились; транзакция отменена.')
            require(schema == list(db.execute('SELECT * FROM sqlite_master ORDER BY name')), 'Изменилась схема; транзакция отменена.')
            require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'Нарушены связи ответственных.')
            require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Нарушена целостность базы.')
            summary = _summary(db,ticket_ids,now)
            require(summary['deadlines'] == {'Просрочена':7,'В срок':8,'Срок не назначен':5,'Закрыта':5},
                    'Распределение сроков не соответствует плану.')
            value = dict(dataset=DATASET, database=str(database), state='complete',
                         filled_at=now.isoformat(), backup=str(backup), manager_ids=manager_ids,
                         ticket_ids=ticket_ids, submission_keys=keys, summary=summary)
            pending_written = True
            _write_manifest(receipt, value)
            db.commit()
            committed = True
            return dict(already_seeded=False, receipt=str(receipt), **value)
        except BaseException:
            db.rollback()
            if pending_written and not committed:
                receipt.unlink(missing_ok=True)
            raise


@contextmanager
def stopped_project():
    lock = InstanceLock(ROOT/'.runtime/launcher.lock')
    try:
        if not lock.acquire():
            raise RuntimeError('Приложение запущено. Остановите его через Ctrl+C и повторите команду. Процессы не завершаются.')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for port in (5000,5002):
            try:
                with opener.open(f'http://127.0.0.1:{port}/', timeout=1) as response:
                    identity = response.headers.get('X-Manager-Tracker-Project')
                    html = response.read(262144).decode('utf-8', errors='replace')
                    if identity == PROJECT_ID or (identity is None and 'Manager Tracker' in html and 'Контроль клиентских заявок' in html):
                        raise RuntimeError('Сервер отвечает. Остановите Manager Tracker через Ctrl+C и повторите команду.')
            except (OSError, urllib.error.URLError):
                pass
        yield
    finally:
        lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Добавить 5 вымышленных менеджеров и 25 заявок. По умолчанию — только копия базы.')
    parser.add_argument('--database', type=Path, help='Явный путь к существующей целевой базе; обязателен для рабочей базы.')
    args = parser.parse_args(argv)
    target = (args.database if args.database is not None else ROOT/'instance/demo_tracker.sqlite3').resolve()
    if not target.is_relative_to(ROOT):
        parser.error('Целевая база должна находиться внутри папки этого проекта.')
    if args.database is None and target == (ROOT/'instance/tracker.sqlite3').resolve():
        parser.error('Для рабочей базы нужен явный --database.')
    try:
        with stopped_project():
            if args.database is None and not target.exists():
                backup_database(ROOT/'instance/tracker.sqlite3', target)
            result = seed_database(target)
        print('Набор уже заполнен; записи не изменены.' if result['already_seeded'] else 'Добавлено: 5 менеджеров и 25 заявок.')
        print('База:', target)
        print('Список ID:', result['receipt'])
        if result.get('missing_ticket_ids'):
            print('Некоторые ID набора отсутствуют (удаление пользователем или прерванное заполнение). Автоматическое восстановление не выполняется.')
        if 'backup' in result:
            print('Резервная копия:', result['backup'])
        if 'summary' in result:
            print(json.dumps(result['summary'], ensure_ascii=False, indent=2))
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError, AssertionError) as error:
        print(f'Заполнение не выполнено или требует проверки списка ID: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
