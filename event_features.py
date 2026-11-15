"""Append-only ticket activity in the same transaction as each business operation."""
import json

EVENT_MIGRATION = 'stage_4a_events_v1'
MANUAL_TYPES = [('manual.call','Звонок'),('manual.message','Сообщение клиенту'),('manual.comment','Комментарий')]
EVENT_LABELS = dict(MANUAL_TYPES, **{
    'journal.enabled':'Журнал подключён',
    'ticket.created':'Заявка создана',
    'ticket.status_changed':'Статус заявки изменён',
    'ticket.closed':'Заявка закрыта',
    'ticket.reopened':'Заявка открыта повторно',
    'ticket.manager_changed':'Ответственный заявки изменён',
    'ticket.updated':'Поля заявки изменены',
    'task.created':'Задача создана',
    'task.updated':'Задача изменена',
    'task.rescheduled':'Срок задачи перенесён',
    'task.completed':'Задача выполнена',
    'task.cancelled':'Задача отменена',
    'proposal.sent':'КП отправлено',
})
MEANINGFUL_TYPES = {code for code,_ in MANUAL_TYPES} | {'ticket.created','task.completed','proposal.sent'}


def append_event(connection,ticket_id,event_type,now,details=None,task_id=None,submission_key=None):
    if not connection.in_transaction:
        raise RuntimeError('Событие должно сохраняться внутри бизнес-транзакции.')
    if event_type not in EVENT_LABELS:
        raise ValueError('Неизвестный тип события.')
    if now.tzinfo is None:
        raise ValueError('Для события требуется время приложения с часовым поясом.')
    return connection.execute('''INSERT INTO ticket_events
        (ticket_id,occurred_at,event_type,source,meaningful,details,task_id,submission_key)
        VALUES (?,?,?,?,?,?,?,?)''',
        (ticket_id,now.isoformat(timespec='seconds'),event_type,
         'manual' if event_type.startswith('manual.') else 'application',
         int(event_type in MEANINGFUL_TYPES),json.dumps(details or {},ensure_ascii=False),task_id,submission_key)).lastrowid


def migrate_events(connection,now):
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=?',(EVENT_MIGRATION,)).fetchone():
        return
    connection.execute('''CREATE TABLE ticket_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        occurred_at TEXT NOT NULL,
        event_type TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN ('manual','application')),
        meaningful INTEGER NOT NULL CHECK(meaningful IN (0,1)),
        details TEXT NOT NULL DEFAULT '{}',
        task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        submission_key TEXT UNIQUE)''')
    connection.execute('CREATE INDEX ticket_events_order_idx ON ticket_events(ticket_id,occurred_at DESC,id DESC)')
    connection.execute('CREATE INDEX ticket_events_activity_idx ON ticket_events(ticket_id,meaningful,occurred_at DESC,id DESC)')
    for (ticket_id,) in connection.execute('SELECT id FROM tickets ORDER BY id').fetchall():
        append_event(connection,ticket_id,'journal.enabled',now,
                     {'text':'Журнал подключён. Предыдущая история действий недоступна'})
    connection.execute('INSERT INTO schema_migrations VALUES (?,?)',(EVENT_MIGRATION,now.isoformat(timespec='seconds')))


def manager_snapshot(connection,manager_id):
    if manager_id is None:
        return None
    row=connection.execute('SELECT id,name FROM managers WHERE id=?',(manager_id,)).fetchone()
    return {'id':row[0],'name':row[1]} if row else {'id':manager_id,'name':'Менеджер недоступен'}


def task_snapshot(connection,task):
    return dict(id=task['id'],title=task['title'],manager=manager_snapshot(connection,task['manager_id']),
                due_at=task['due_at'],state=task['state'],
                service_type=task['service_type'] if 'service_type' in task.keys() else 'ordinary')


def ticket_snapshot(connection,ticket):
    return dict(client=ticket['client'],contact=ticket['contact'],description=ticket['description'],
                status=ticket['status'],priority=ticket['priority'],manager=manager_snapshot(connection,ticket['manager_id']))


def change(label,before,after):
    return dict(label=label,before=before,after=after)


def display_value(value):
    if value is None:
        return 'Не назначен'
    if isinstance(value,dict) and 'name' in value:
        return f"{value['name']} · №{value['id']}"
    return str(value) if value != '' else 'Не указано'


def history_context(connection,ticket_id):
    rows=connection.execute('''SELECT * FROM ticket_events WHERE ticket_id=?
        ORDER BY occurred_at DESC,id DESC''',(ticket_id,)).fetchall()
    history=[]
    for row in rows:
        item=dict(row)
        try:
            details=json.loads(item['details'])
            if not isinstance(details,dict):
                raise ValueError
        except (ValueError,TypeError):
            details={'text':'Сведения события недоступны: неверный формат записи.'}
        item['label']=EVENT_LABELS.get(item['event_type'],'Неизвестное событие')
        item['text']=details.get('text','')
        item['task_snapshot']=details.get('task') if isinstance(details.get('task'),dict) else None
        item['changes']=[dict(label=c.get('label','Поле'),before=display_value(c.get('before')),
                              after=display_value(c.get('after'))) for c in (details.get('changes') if isinstance(details.get('changes'),list) else []) if isinstance(c,dict)]
        item['created_ticket']=details.get('ticket') if isinstance(details.get('ticket'),dict) else None
        item['next_task']=details.get('next_task') if isinstance(details.get('next_task'),dict) else None
        history.append(item)
    started=connection.execute('SELECT applied_at FROM schema_migrations WHERE version=?',(EVENT_MIGRATION,)).fetchone()
    return dict(history=history,last_activity=next((item for item in history if item['meaningful']),None),
                journal_started_at=started[0] if started else None,manual_types=MANUAL_TYPES)


CONTROL_LABELS = {
    'unassigned':'Нет ответственного', 'new_stale':'Не взята в работу',
    'no_next_task':'Нет следующей задачи', 'missing_due':'Задача без срока',
    'overdue_task':'Просрочена задача', 'inactive':'Давно нет активности',
    'no_activity':'Нет данных об активности', 'invalid_due':'Некорректный срок задачи',
    'invalid_created':'Некорректная дата создания', 'invalid_event':'Некорректные данные события',
}
CONTROL_PROBLEMS = set(list(CONTROL_LABELS)[:6])
CONTROL_OPTIONS = [('', 'Все'), ('problems','Есть проблемы'),
                   ('data','Требуют проверки данных'), ('clear','Без замечаний')] + list(CONTROL_LABELS.items())


def safe_event(row, parse, now):
    """Do not infer activity from a corrupt or unknown event."""
    try:
        instant = parse(row['occurred_at'])
        details = json.loads(row['details'])
        if not isinstance(details, dict) or row['event_type'] not in EVENT_LABELS:
            raise ValueError
        if not isinstance(details.get('changes', []), list):
            raise ValueError
        if any(details.get(k) is not None and not isinstance(details[k], dict)
               for k in ('task', 'ticket', 'next_task')):
            raise ValueError
        if instant > now:
            raise ValueError
        if bool(row['meaningful']) != (row['event_type'] in MEANINGFUL_TYPES):
            raise ValueError
        return instant
    except (ValueError, TypeError, OverflowError):
        return None


def control_info(ticket, tasks, events, now, parse, settings):
    from datetime import timedelta
    codes = []
    if ticket['status'] == 'Закрыта':
        return dict(codes=[], problems=False, data=False, clear=False, labels=[])
    if ticket['manager_id'] is None: codes.append('unassigned')
    try:
        created = parse(ticket['created_at'])
        if created > now: raise ValueError
        if ticket['status']=='Новая' and now-created > timedelta(hours=settings['new_hours']):
            codes.append('new_stale')
    except (ValueError, TypeError, OverflowError):
        codes.append('invalid_created')
    opened = [t for t in tasks if t['state']=='Открыта']
    if not opened: codes.append('no_next_task')
    for task in opened:
        value = task['due_at']
        if not value:
            codes.append('missing_due'); continue
        try:
            if parse(value) < now: codes.append('overdue_task')
        except (ValueError, TypeError, OverflowError):
            codes.append('invalid_due')
    activity=[]
    uncertain_activity=False
    for event in events:
        instant = safe_event(event,parse,now)
        if instant is None:
            codes.append('invalid_event')
            if event['meaningful'] or event['event_type'] in MEANINGFUL_TYPES: uncertain_activity=True
        elif event['event_type'] in MEANINGFUL_TYPES: activity.append(instant)
    if not activity or uncertain_activity: codes.append('no_activity')
    elif now-max(activity) > timedelta(hours=settings['inactivity_hours']): codes.append('inactive')
    codes = [c for c in CONTROL_LABELS if c in codes]
    problems = bool(set(codes) & CONTROL_PROBLEMS)
    data = bool(set(codes) - CONTROL_PROBLEMS)
    return dict(codes=codes, problems=problems, data=data, clear=not codes,
                labels=[dict(code=c,label=CONTROL_LABELS[c],data=c not in CONTROL_PROBLEMS) for c in codes])


def control_matches(info, selected):
    return not selected or (info.get(selected, False) if selected in ('problems','data','clear') else selected in info['codes'])
