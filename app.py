from contextlib import closing
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.serving import make_server
from task_features import TASK_SELECT, migrate_tasks, register_tasks, task_overview
from event_features import (MANUAL_TYPES, append_event, change, history_context, manager_snapshot,
                            migrate_events, task_snapshot, ticket_snapshot)
from event_features import control_info, control_matches, CONTROL_OPTIONS, safe_event, EVENT_LABELS
from automation_features import get_control_settings
from automation_features import (PROPOSAL_STATUS, assign_initial_if_empty, create_automatic_task,
                                  INITIAL, migrate_automation, register_automation)

from report_features import (migrate_reports, save_summary, row_snapshot, csv_response, date_text,
                             CURRENT_HEADERS, SUMMARY_HEADERS, row_cells)

from config_features import load_settings, ConfigurationError, LocalProxy
from auth_features import migrate_auth, register_auth

ROOT = Path(__file__).resolve().parent
STATUSES = ('Новая', 'В работе', 'Ожидает клиента', 'Закрыта', PROPOSAL_STATUS)
PRIORITIES = ('Низкий', 'Средний', 'Высокий')
# Europe/Kaliningrad uses UTC+02:00. A fixed offset avoids requiring tzdata
# on Windows. Existing naive deadlines are local application times, not UTC.
APP_TIMEZONE = timezone(timedelta(hours=2), 'Europe/Kaliningrad')
DEADLINE_LABELS = {'closed': 'Закрыта', 'missing': 'Срок не назначен',
                   'overdue': 'Просрочена', 'on_time': 'В срок', 'invalid': 'Некорректный срок'}


def application_now():
    return datetime.now(APP_TIMEZONE)


def parse_deadline(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=APP_TIMEZONE) if parsed.tzinfo is None else parsed.astimezone(APP_TIMEZONE)


def deadline_info(ticket, now):
    value = ticket['next_action']
    valid, deadline = True, None
    if value:
        try:
            deadline = parse_deadline(value)
        except (ValueError, TypeError, OverflowError):
            valid = False
    if ticket['status'] == 'Закрыта':
        code = 'closed'
    elif not value:
        code = 'missing'
    elif not valid:
        code = 'invalid'
    else:
        code = 'overdue' if deadline < now else 'on_time'
    return dict(code=code, label=DEADLINE_LABELS[code], valid=valid)


def unassigned(value):
    return value is None or not value.strip()


MANAGER_MIGRATION = 'stage_3a_managers_v1'
TICKET_SELECT = '''SELECT t.*, m.name AS manager_name, m.active AS manager_active
                   FROM tickets t LEFT JOIN managers m ON m.id = t.manager_id'''


def migrate_database(connection):
    """Atomic schema upgrade and one-time exact import; never infer from NULL IDs."""
    try:
        connection.execute('BEGIN IMMEDIATE')
        migration_time = application_now()
        connection.execute('''CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            client TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL, manager TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, priority TEXT NOT NULL,
            next_action TEXT NOT NULL DEFAULT '', submission_key TEXT NOT NULL UNIQUE)''')
        connection.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)''')
        done = connection.execute('SELECT 1 FROM schema_migrations WHERE version = ?',
                                  (MANAGER_MIGRATION,)).fetchone()
        if not done:
            connection.execute('''CREATE TABLE managers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)))''')
            connection.execute('ALTER TABLE tickets ADD COLUMN manager_id INTEGER REFERENCES managers(id)')
            imported = {}
            for ticket_id, old_name in connection.execute('SELECT id, manager FROM tickets ORDER BY id').fetchall():
                if unassigned(old_name):
                    continue
                if old_name not in imported:
                    imported[old_name] = connection.execute('INSERT INTO managers(name) VALUES (?)',
                                                             (old_name,)).lastrowid
                connection.execute('UPDATE tickets SET manager_id = ? WHERE id = ?',
                                   (imported[old_name], ticket_id))
            connection.execute('CREATE INDEX tickets_manager_id_idx ON tickets(manager_id)')
            connection.execute('INSERT INTO schema_migrations VALUES (?, ?)',
                               (MANAGER_MIGRATION, migration_time.isoformat()))
        migrate_tasks(connection, migration_time)
        migrate_events(connection, migration_time)
        migrate_automation(connection, migration_time)
        migrate_reports(connection, migration_time)
        migrate_auth(connection, migration_time)
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise sqlite3.IntegrityError('Нарушены связи базы данных; обновление отменено.')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def check_schema(connection):
    from task_features import TASK_MIGRATION
    from event_features import EVENT_MIGRATION
    from automation_features import AUTOMATION_MIGRATION, CONTROL_MIGRATION
    from report_features import REPORT_MIGRATION
    from auth_features import AUTH_MIGRATION
    expected={MANAGER_MIGRATION,TASK_MIGRATION,EVENT_MIGRATION,AUTOMATION_MIGRATION,CONTROL_MIGRATION,REPORT_MIGRATION,AUTH_MIGRATION}
    required={'tickets':{'id','client','manager_id','status'},'tasks':{'id','ticket_id','service_type','trigger_event_id'},
              'managers':{'id','name','active'},'ticket_events':{'id','event_type','meaningful','details'},
              'automation_settings':{'initial_hours','proposal_hours'},'control_settings':{'new_hours','inactivity_hours'},
              'daily_summaries':{'payload','local_date'},'auth_attempts':{'id','client_ip','created_at','failed'},'auth_blocks':{'client_ip','blocked_until'}}
    try:
        versions={r[0] for r in connection.execute('SELECT version FROM schema_migrations')}
        if versions!=expected:raise ValueError
        for table,columns in required.items():
            if not columns <= {r[1] for r in connection.execute('PRAGMA table_info('+table+')')}:raise ValueError
    except (sqlite3.Error,ValueError):
        raise ConfigurationError('Схема несовместима. Остановите только службу трекера и выполните python server_tools.py migrate --config <закрытый JSON> --backup-dir <закрытый каталог>.') from None


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=128 * 1024, PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    app.config.update(load_settings(ROOT,test_config))
    if app.config['APP_MODE']=='local':
        Path(app.config['DATABASE']).parent.mkdir(parents=True,exist_ok=True)
    else:
        path=Path(app.config['DATABASE'])
        if not path.is_file():
            raise ConfigurationError('База не инициализирована. Выполните python server_tools.py migrate --config <закрытый JSON> --backup-dir <закрытый каталог>.')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
            check_schema(connection)
    if app.config['TRUST_LOCAL_PROXY']:
        app.wsgi_app=LocalProxy(app.wsgi_app)

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys = ON')
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        connection = g.pop('db', None)
        if connection:
            connection.close()

    if app.config['APP_MODE']=='local':
        with app.app_context():
            migrate_database(db())

    register_auth(app, db)

    @app.before_request
    def csrf_protection():
        if request.endpoint in ('static','health'): return
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        if request.method == 'POST' and not secrets.compare_digest(
                session['csrf_token'], request.form.get('csrf_token', '')):
            abort(400, description='Форма устарела. Обновите страницу и попробуйте снова.')

    @app.context_processor
    def common():
        return dict(statuses=STATUSES, priorities=PRIORITIES)

    @app.template_filter('date')
    def date_format(value):
        if not value:
            return 'Не указан'
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(APP_TIMEZONE)
            return parsed.strftime('%d.%m.%Y %H:%M')
        except (ValueError, TypeError, OverflowError):
            return f'Некорректный срок: {value}'

    def request_now():
        if 'request_now' not in g:
            g.request_now = application_now().astimezone(APP_TIMEZONE)
        return g.request_now

    def overview(ticket, tasks=None):
        if tasks is None:
            tasks = db().execute(TASK_SELECT + ' WHERE task.ticket_id=?', (ticket['id'],)).fetchall()
        return task_overview(ticket, tasks, request_now(), parse_deadline)

    def ticket_or_404(ticket_id):
        ticket = db().execute(TICKET_SELECT + ' WHERE t.id = ?', (ticket_id,)).fetchone()
        if ticket is None:
            abort(404, description='Заявка не найдена. Возможно, она уже удалена.')
        return ticket

    def latest_activity(events, control):
        if 'no_activity' in control['codes']:
            return None
        valid = [(safe_event(e, parse_deadline, request_now()), e) for e in events if e['meaningful']]
        valid = [(instant, e) for instant, e in valid if instant is not None]
        if not valid:
            return None
        _, event = max(valid, key=lambda item: (item[0], item[1]['id']))
        return dict(event, label=EVENT_LABELS[event['event_type']])

    def load_control_state():
        """Shared batch loading and 4V calculation for list and dashboard."""
        instant = request_now()
        tickets = db().execute(TICKET_SELECT + ' ORDER BY t.id DESC').fetchall()
        tasks_by_ticket, events_by_ticket = {}, {}
        for task in db().execute(TASK_SELECT):
            tasks_by_ticket.setdefault(task['ticket_id'], []).append(task)
        for event in db().execute('SELECT * FROM ticket_events'):
            events_by_ticket.setdefault(event['ticket_id'], []).append(event)
        settings = get_control_settings(db())
        g.control_settings = settings
        controls, schedules, activities = {}, {}, {}
        for ticket in tickets:
            key = ticket['id']
            tasks = tasks_by_ticket.get(key, [])
            events = events_by_ticket.get(key, [])
            controls[key] = control_info(ticket, tasks, events, instant, parse_deadline, settings)
            schedules[key] = overview(ticket, tasks)
            activities[key] = latest_activity(events, controls[key])
        return tickets, controls, schedules, activities

    def dashboard_context():
        tickets, controls, schedules, activities = load_control_state()
        active = [t for t in tickets if t['status'] != 'Закрыта']
        definitions = [
            ('active', 'Активные', dict(scope='active')),
            ('new', 'Новые', dict(status='Новая')),
            ('problems', 'Есть проблемы', dict(control='problems')),
            ('unassigned', 'Нет ответственного', dict(control='unassigned')),
            ('new_stale', 'Не взяты в работу', dict(control='new_stale')),
            ('no_next_task', 'Нет следующей задачи', dict(control='no_next_task')),
            ('missing_due', 'Есть задача без срока', dict(control='missing_due')),
            ('overdue_task', 'Есть просроченная задача', dict(control='overdue_task')),
            ('inactive', 'Давно нет активности', dict(control='inactive')),
            ('data', 'Требуют проверки данных', dict(control='data')),
        ]
        metrics = []
        for code, label, filters in definitions:
            if code == 'active': count = len(active)
            elif code == 'new': count = sum(t['status']=='Новая' for t in active)
            else: count = sum(control_matches(controls[t['id']], filters['control']) for t in active)
            metrics.append(dict(code=code, label=label, count=count, url=url_for('index', **filters)))
        attention = [t for t in active if controls[t['id']]['problems'] or controls[t['id']]['data']]
        attention.sort(key=lambda t: (0 if 'overdue_task' in controls[t['id']]['codes'] else
                                     1 if controls[t['id']]['problems'] else 2, -t['id']))
        return dict(tickets=attention, controls=controls, schedules=schedules,
                    activities=activities, metrics=metrics, calculated_at=request_now())

    @app.get('/dashboard')
    def dashboard():
        context = None
        try:
            # Serializes first daily visits; all SELECTs share this database snapshot.
            with db():
                db().execute('BEGIN IMMEDIATE')
                context = dashboard_context()
                rows = [row_snapshot(t, context['controls'][t['id']], context['schedules'][t['id']],
                                     context['activities'][t['id']], parse_deadline) for t in context['tickets']]
                summary = save_summary(db(),request_now(),context['metrics'],g.control_settings,rows)
                # A failed render rolls back the snapshot as well.
                html = render_template('dashboard.html', **context, today_summary=summary)
            return html
        except (sqlite3.Error, ValueError, TypeError):
            app.logger.exception('Не удалось сохранить ежедневную сводку')
            if context is None:
                context = dashboard_context()
            return render_template('dashboard.html', **context, today_summary=None,
                summary_error='Не удалось сохранить ежедневную сводку. Текущая панель доступна; попробуйте открыть её снова.')


    def list_context():
        tickets, controls, schedules, activities = load_control_state()
        selected_scope = request.args.get('scope', '')
        if selected_scope not in ('active', 'closed'): selected_scope = ''
        selected_control = request.args.get('control', '')
        if selected_control not in dict(CONTROL_OPTIONS): selected_control = ''
        managers = db().execute('''SELECT m.* FROM managers m WHERE m.active = 1
            OR EXISTS(SELECT 1 FROM tickets t WHERE t.manager_id = m.id) ORDER BY m.id''').fetchall()
        manager_options = [(str(m['id']), manager_label(m)) for m in managers]
        manager = request.args.get('manager', '')
        if manager not in {'', 'unassigned', *[value for value, _ in manager_options]}:
            manager = ''
        status = request.args.get('status', '')
        if status not in STATUSES:
            status = ''
        overdue = request.args.get('overdue', '')
        if overdue not in ('overdue', 'not_overdue'):
            overdue = ''
        filtered = []
        deadlines = {}
        nearest_tasks = {}
        for ticket in tickets:
            if selected_scope == 'active' and ticket['status'] == 'Закрыта': continue
            if selected_scope == 'closed' and ticket['status'] != 'Закрыта': continue
            schedule = schedules[ticket['id']]
            if not control_matches(controls[ticket['id']], selected_control): continue
            info = schedule['deadline']
            nearest_tasks[ticket['id']] = schedule['nearest']
            deadlines[ticket['id']] = info
            if manager == 'unassigned' and ticket['manager_id'] is not None:
                continue
            if manager not in ('', 'unassigned') and str(ticket['manager_id']) != manager:
                continue
            if status and ticket['status'] != status:
                continue
            if overdue and not info['valid']:
                continue
            if overdue == 'overdue' and info['code'] != 'overdue':
                continue
            if overdue == 'not_overdue' and info['code'] == 'overdue':
                continue
            filtered.append(ticket)
        return dict(tickets=filtered, total=len(tickets), deadlines=deadlines, nearest_tasks=nearest_tasks,
                               schedules=schedules, activities=activities,
                               manager_options=manager_options, selected_manager=manager,
                               selected_status=status, selected_overdue=overdue,
                               has_filters=bool(manager or status or overdue or selected_control or selected_scope), unassigned=unassigned,
                               controls=controls, control_options=CONTROL_OPTIONS, selected_control=selected_control, selected_scope=selected_scope)

    @app.get('/')
    def index():
        context=list_context()
        export_url=url_for('export_tickets',scope=context['selected_scope'],manager=context['selected_manager'],
                           status=context['selected_status'],overdue=context['selected_overdue'],control=context['selected_control'])
        return render_template('index.html', **context, export_url=export_url)

    @app.get('/tickets/export.csv')
    def export_tickets():
        with db():
            db().execute('BEGIN')
            context=list_context()
            rows=[]
            for ticket in context['tickets']:
                key=ticket['id']
                row=row_snapshot(ticket,context['controls'][key],context['schedules'][key],context['activities'][key],parse_deadline)
                cells=row_cells(row)
                rows.append([ticket['id'],date_text(ticket['created_at'],parse_deadline),ticket['client'],ticket['contact'],
                             ticket['status'],ticket['priority'],*cells[3:]])
        return csv_response(CURRENT_HEADERS,rows,'manager-tracker-tickets-'+request_now().date().isoformat()+'.csv')

    @app.get('/summaries')
    def summaries():
        rows=db().execute('SELECT id,local_date,formed_at,row_count FROM daily_summaries ORDER BY local_date DESC').fetchall()
        return render_template('summaries.html',summaries=rows)

    def summary_or_404(summary_id):
        record=db().execute('SELECT * FROM daily_summaries WHERE id=?',(summary_id,)).fetchone()
        if record is None: abort(404,description='Сводка не найдена.')
        try: payload=json.loads(record['payload'])
        except (ValueError,TypeError): abort(409,description='Данные сохранённой сводки повреждены.')
        return record,payload

    @app.get('/summaries/<int:summary_id>')
    def summary_detail(summary_id):
        record,payload=summary_or_404(summary_id)
        existing={row[0] for row in db().execute('SELECT id FROM tickets')}
        return render_template('summary_detail.html',summary=record,snapshot=payload,existing_ids=existing)

    @app.get('/summaries/<int:summary_id>/export.csv')
    def export_summary(summary_id):
        record,payload=summary_or_404(summary_id)
        rows=[[record['local_date'],record['formed_at'],*row_cells(row)] for row in payload['rows']]
        return csv_response(SUMMARY_HEADERS,rows,'manager-tracker-summary-'+str(record['id'])+'.csv')

    @app.route('/tickets/new', methods=['GET', 'POST'])
    @app.route('/tickets/<int:ticket_id>/edit', methods=['GET', 'POST'])
    def form(ticket_id=None):
        ticket = ticket_or_404(ticket_id) if ticket_id else None
        values = dict(ticket) if ticket else dict(status='Новая', priority='Средний', manager_id='')
        errors = {}
        submission_key = request.form.get('submission_key', '') if request.method == 'POST' else secrets.token_hex(32)
        if request.method == 'POST':
            fields = ('client', 'contact', 'description', 'manager_id', 'status', 'priority')
            values = {field: request.form.get(field, '').strip() for field in fields}
            for field, label, limit in [('client', 'Клиент', 200), ('contact', 'Контакт', 250),
                                         ('description', 'Описание потребности', 10000)]:
                if field in ('client', 'description') and not values[field]:
                    errors[field] = f'Заполните поле «{label}».'
                elif len(values[field]) > limit:
                    errors[field] = f'Поле «{label}»: не более {limit} символов.'
            if values['status'] not in STATUSES:
                errors['status'] = 'Выберите статус из списка.'
            if values['status']==PROPOSAL_STATUS and (ticket is None or ticket['status']!=PROPOSAL_STATUS):
                errors['status']='Отправку КП фиксируйте специальным действием в карточке заявки.'
            if values['priority'] not in PRIORITIES:
                errors['priority'] = 'Выберите приоритет из списка.'
            selected_manager = None
            if values['manager_id']:
                selected_manager = db().execute('SELECT * FROM managers WHERE CAST(id AS TEXT) = ?',
                                               (values['manager_id'],)).fetchone()
                if selected_manager is None or (not selected_manager['active'] and
                        (ticket is None or ticket['manager_id'] != selected_manager['id'])):
                    errors['manager_id'] = 'Выберите активного менеджера или «Не назначен».'
            if not ticket_id and (len(submission_key) != 64 or any(c not in '0123456789abcdef' for c in submission_key)):
                errors['submission_key'] = 'Некорректная форма. Откройте создание заявки заново.'
            if not errors:
                connection = db()
                with connection:
                    # Serialize with deactivation; recheck assignment under the write lock.
                    connection.execute('BEGIN IMMEDIATE')
                    if selected_manager is not None:
                        latest = connection.execute('SELECT active FROM managers WHERE id = ?',
                                                    (selected_manager['id'],)).fetchone()
                        current_id = connection.execute('SELECT manager_id FROM tickets WHERE id = ?',
                                                        (ticket_id,)).fetchone() if ticket_id else None
                        if latest is None or (not latest['active'] and
                                (current_id is None or current_id['manager_id'] != selected_manager['id'])):
                            errors['manager_id'] = 'Менеджер уже отключён. Выберите активного менеджера.'
                    if errors:
                        return render_ticket_form(ticket, values, errors, submission_key), 422
                    saved_values = [values[f] if f != 'manager_id' else
                                    (selected_manager['id'] if selected_manager else None) for f in fields]
                    if ticket_id:
                        previous = ticket_or_404(ticket_id)
                        if values['status']==PROPOSAL_STATUS and previous['status']!=PROPOSAL_STATUS:
                            errors['status']='Статус изменился. Отправку КП фиксируйте специальным действием.'
                            return render_ticket_form(ticket,values,errors,submission_key),422
                        connection.execute('UPDATE tickets SET ' + ', '.join(f'{f} = ?' for f in fields) + ' WHERE id = ?',
                                           (*saved_values, ticket_id))
                        new_manager_id = selected_manager['id'] if selected_manager else None
                        instant = request_now()
                        if previous['status'] != values['status']:
                            event_type = ('ticket.closed' if values['status']=='Закрыта' else
                                          'ticket.reopened' if previous['status']=='Закрыта' else 'ticket.status_changed')
                            append_event(connection,ticket_id,event_type,instant,
                                         {'changes':[change('Статус',previous['status'],values['status'])]})
                        if previous['manager_id'] != new_manager_id:
                            append_event(connection,ticket_id,'ticket.manager_changed',instant,
                                         {'changes':[change('Ответственный',manager_snapshot(connection,previous['manager_id']),
                                                            manager_snapshot(connection,new_manager_id))]})
                            if new_manager_id is not None:
                                assign_initial_if_empty(connection,ticket_id,new_manager_id,instant)
                        changes = [change(label,previous[field],values[field]) for field,label in
                                   [('client','Клиент'),('contact','Контакт'),('description','Описание'),('priority','Приоритет')]
                                   if previous[field] != values[field]]
                        if changes:
                            append_event(connection,ticket_id,'ticket.updated',instant,{'changes':changes})
                        if values['status'] == 'Закрыта':
                            cancelled = connection.execute(TASK_SELECT+" WHERE task.ticket_id=? AND task.state='Открыта'",(ticket_id,)).fetchall()
                            connection.execute('''UPDATE tasks SET state='Отменена',result='Заявка закрыта',finished_at=?
                                WHERE ticket_id=? AND state='Открыта' ''',
                                (instant.isoformat(timespec='seconds'), ticket_id))
                            for task in cancelled:
                                snapshot = task_snapshot(connection,task)
                                snapshot['state'] = 'Отменена'
                                append_event(connection,ticket_id,'task.cancelled',instant,
                                             {'task':snapshot,'text':'Заявка закрыта',
                                              'changes':[change('Состояние','Открыта','Отменена')]},task_id=task['id'])
                    else:
                        cursor = connection.execute('''INSERT INTO tickets
                            (client, contact, description, manager_id, status, priority, created_at, submission_key)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(submission_key) DO NOTHING''',
                            (*saved_values, request_now().replace(tzinfo=None).isoformat(timespec='seconds'), submission_key))
                        ticket_id = connection.execute('SELECT id FROM tickets WHERE submission_key = ?', (submission_key,)).fetchone()['id']
                        if cursor.rowcount:
                            event_id=append_event(connection,ticket_id,'ticket.created',request_now(),
                                                  {'ticket':ticket_snapshot(connection,ticket_or_404(ticket_id))})
                            if values['status']!='Закрыта':
                                create_automatic_task(connection,ticket_id,INITIAL,selected_manager['id'] if selected_manager else None,
                                                      request_now(),event_id)
                flash('Заявка сохранена.', 'success')
                return redirect(url_for('detail', ticket_id=ticket_id), code=303)
        return render_ticket_form(ticket, values, errors, submission_key), 422 if errors else 200

    def manager_label(manager):
        return f"{manager['name']} · №{manager['id']}" + (' · Неактивен' if not manager['active'] else '')

    def render_ticket_form(ticket, values, errors, submission_key):
        managers = db().execute('SELECT * FROM managers WHERE active = 1 OR id = ? ORDER BY id',
                                (ticket['manager_id'] if ticket else None,)).fetchall()
        return render_template('form.html', ticket=ticket, values=values, errors=errors,
                               submission_key=submission_key,
                               form_statuses=[s for s in STATUSES if s!=PROPOSAL_STATUS or (ticket and ticket['status']==PROPOSAL_STATUS)],
                               manager_options=[(str(m['id']), manager_label(m)) for m in managers])

    @app.get('/managers')
    def managers():
        items = db().execute('''SELECT m.*, COUNT(t.id) AS ticket_count FROM managers m
            LEFT JOIN tickets t ON t.manager_id = m.id GROUP BY m.id ORDER BY m.id''').fetchall()
        return render_template('managers.html', managers=items)

    def manager_or_404(manager_id):
        item = db().execute('SELECT * FROM managers WHERE id = ?', (manager_id,)).fetchone()
        if item is None:
            abort(404, description='Менеджер не найден.')
        return item

    @app.route('/managers/new', methods=['GET', 'POST'])
    @app.route('/managers/<int:manager_id>/edit', methods=['GET', 'POST'])
    def manager_form(manager_id=None):
        item = manager_or_404(manager_id) if manager_id else None
        name = item['name'] if item else ''
        error = None
        if request.method == 'POST':
            raw = request.form.get('name', '')
            unchanged = item is not None and raw == item['name']
            name = raw if unchanged else raw.strip()
            connection = db()
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if not unchanged:
                    if not name:
                        error = 'Укажите имя менеджера.'
                    elif len(name) > 100:
                        error = 'Имя менеджера: не более 100 символов.'
                    elif any(row['id'] != manager_id and row['name'].strip().casefold() == name.casefold()
                             for row in connection.execute('SELECT id, name FROM managers')):
                        error = 'Менеджер с таким именем уже существует (включая неактивных).'
                if not error:
                    if manager_id:
                        if not unchanged:
                            connection.execute('UPDATE managers SET name = ? WHERE id = ?', (name, manager_id))
                    else:
                        connection.execute('INSERT INTO managers(name) VALUES (?)', (name,))
            if not error:
                flash('Менеджер сохранён.', 'success')
                return redirect(url_for('managers'), code=303)
        return render_template('manager_form.html', manager=item, name=name, error=error), 422 if error else 200

    @app.post('/managers/<int:manager_id>/activity')
    def manager_activity(manager_id):
        manager_or_404(manager_id)
        active = request.form.get('active')
        if active not in ('0', '1'):
            abort(400, description='Некорректное состояние менеджера.')
        with db():
            db().execute('UPDATE managers SET active = ? WHERE id = ?', (int(active), manager_id))
        flash('Состояние менеджера обновлено.', 'success')
        return redirect(url_for('managers'), code=303)

    @app.get('/tickets/<int:ticket_id>')
    def detail(ticket_id):
        return render_detail(ticket_id)

    def render_detail(ticket_id,action_errors=None,action_values=None,action_key=None):
        ticket = ticket_or_404(ticket_id)
        schedule = overview(ticket)
        events = db().execute('SELECT * FROM ticket_events WHERE ticket_id=?',(ticket_id,)).fetchall()
        control = control_info(ticket, schedule['open_tasks']+schedule['closed_tasks'], events,
                               request_now(), parse_deadline, get_control_settings(db()))
        history = history_context(db(),ticket_id)
        history['last_activity'] = latest_activity(events, control)
        return render_template('detail.html', ticket=ticket, **schedule, **history, control=control,
                               action_errors=action_errors or {}, action_values=action_values or {},
                               action_key=action_key if action_key is not None else secrets.token_hex(32))

    @app.post('/tickets/<int:ticket_id>/actions')
    def add_action(ticket_id):
        values={field:request.form.get(field,'').strip() for field in ('kind','text')}
        key=request.form.get('submission_key','')
        errors={}
        ticket_or_404(ticket_id)
        if values['kind'] not in {code for code,_ in MANUAL_TYPES}:
            errors['kind']='Выберите звонок, сообщение клиенту или комментарий.'
        if not 1<=len(values['text'])<=2000:
            errors['text']='Текст действия: от 1 до 2000 символов.'
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
            errors['submission_key']='Форма устарела. Обновите карточку заявки.'
        with db():
            db().execute('BEGIN IMMEDIATE')
            ticket=ticket_or_404(ticket_id)
            if not errors:
                existing=db().execute('SELECT ticket_id FROM ticket_events WHERE submission_key=?',(key,)).fetchone()
                if existing:
                    if existing['ticket_id']!=ticket_id:
                        abort(400,description='Ключ действия относится к другой заявке.')
                    return redirect(url_for('detail',ticket_id=ticket_id),code=303)
            if ticket['status']=='Закрыта':
                abort(409,description='В закрытой заявке история доступна только для чтения.')
            if not errors:
                append_event(db(),ticket_id,values['kind'],request_now(),{'text':values['text']},submission_key=key)
        if errors:
            return render_detail(ticket_id,errors,values,key),422
        flash('Действие добавлено.','success')
        return redirect(url_for('detail',ticket_id=ticket_id),code=303)

    register_tasks(app, db, ticket_or_404, manager_label, request_now, parse_deadline)
    register_automation(app, db, ticket_or_404, request_now)

    @app.route('/tickets/<int:ticket_id>/delete', methods=['GET', 'POST'])
    def delete(ticket_id):
        ticket = ticket_or_404(ticket_id)
        if request.method == 'POST':
            with db():
                db().execute('DELETE FROM tickets WHERE id = ?', (ticket_id,))
            flash('Заявка удалена.', 'success')
            return redirect(url_for('index'), code=303)
        return render_template('delete.html', ticket=ticket)

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(409)
    def request_error(error):
        if error.code == 400 and request.url_rule is None:
            return 'Недопустимый запрос или Host.', 400
        message = 'Слишком большой запрос. Сократите введённый текст.' if error.code == 413 else error.description
        return render_template('error.html', message=message, code=error.code), error.code

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        app.logger.exception('Database error')
        return render_template('error.html', code=503, message='Не удалось сохранить или прочитать данные. Проверьте доступ к файлу базы и повторите попытку.'), 503

    return app


def serve(app, server_factory=make_server, on_bound=None):
    for port in (5000, 5002):
        try:
            server = server_factory('127.0.0.1', port, app)
        except (OSError, SystemExit):
            continue
        print(f'Manager Tracker: http://127.0.0.1:{port}', flush=True)
        try:
            if on_bound:
                on_bound(f'http://127.0.0.1:{port}')
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    raise SystemExit('Порты 5000 и 5002 заняты. Освободите один из них и повторите запуск.')


if __name__ == '__main__':
    from launcher import main
    raise SystemExit(main())
