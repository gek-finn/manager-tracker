"""Tasks within tickets with transactional workflow rules and event logging."""
import hashlib
import secrets

from flask import abort, flash, redirect, render_template, request, url_for
from event_features import append_event, change, manager_snapshot, task_snapshot
from automation_features import INITIAL, INITIAL_HELP, validate_due

TASK_MIGRATION = 'stage_3b_tasks_v1'
TASK_STATES = ('Открыта', 'Выполнена', 'Отменена')
TASK_SELECT = '''SELECT task.*, m.name AS manager_name, m.active AS manager_active
                 FROM tasks task LEFT JOIN managers m ON m.id = task.manager_id'''


def migrate_tasks(connection, now):
    """Called inside the application's schema transaction; no implicit commits."""
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=?', (TASK_MIGRATION,)).fetchone():
        return
    connection.execute('''CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        manager_id INTEGER REFERENCES managers(id),
        due_at TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'Открыта' CHECK(state IN ('Открыта','Выполнена','Отменена')),
        created_at TEXT NOT NULL,
        finished_at TEXT,
        result TEXT NOT NULL DEFAULT '',
        last_reschedule_reason TEXT NOT NULL DEFAULT '',
        submission_key TEXT NOT NULL UNIQUE)''')
    connection.execute('CREATE INDEX tasks_ticket_state_idx ON tasks(ticket_id,state)')
    created = now.isoformat(timespec='seconds')
    for ticket_id, deadline, manager_id, status in connection.execute(
            'SELECT id,next_action,manager_id,status FROM tickets ORDER BY id').fetchall():
        if deadline is None or deadline == '':
            continue
        closed = status == 'Закрыта'
        connection.execute('''INSERT INTO tasks
            (ticket_id,title,manager_id,due_at,state,created_at,result,submission_key)
            VALUES (?,?,?,?,?,?,?,?)''',
            (ticket_id,'Следующее действие',manager_id,deadline,
             'Отменена' if closed else 'Открыта',created,
             'Перенесено из закрытой заявки; фактическое время отмены неизвестно' if closed else '',
             hashlib.sha256(f'{TASK_MIGRATION}:{ticket_id}'.encode()).hexdigest()))
    connection.execute('INSERT INTO schema_migrations VALUES (?,?)', (TASK_MIGRATION,created))


def task_deadline(task, now, parse):
    if task['state'] != 'Открыта':
        return dict(code='finished',label=task['state'],valid=True,parsed=None)
    if not task['due_at']:
        return dict(code='missing',label='Срок не назначен',valid=True,parsed=None)
    try:
        parsed = parse(task['due_at'])
    except (ValueError,TypeError,OverflowError):
        return dict(code='invalid',label='Некорректный срок',valid=False,parsed=None)
    overdue = parsed < now
    return dict(code='overdue' if overdue else 'on_time',label='Просрочена' if overdue else 'В срок',valid=True,parsed=parsed)


def task_overview(ticket, tasks, now, parse):
    details = {t['id']:task_deadline(t,now,parse) for t in tasks}
    opened = [t for t in tasks if t['state']=='Открыта']
    dated = sorted([t for t in opened if details[t['id']]['parsed'] is not None],
                   key=lambda t:(details[t['id']]['parsed'],t['id']))
    invalid = [t for t in opened if not details[t['id']]['valid']]
    missing = [t for t in opened if not t['due_at']]
    closed = sorted([t for t in tasks if t['state']!='Открыта'],
                    key=lambda t:(t['finished_at'] or t['created_at'],t['id']),reverse=True)
    if ticket['status']=='Закрыта':
        code,label,valid='closed','Закрыта',True
    elif any(details[t['id']]['code']=='overdue' for t in opened):
        code,label,valid='overdue','Просрочена',True
    elif invalid:
        code,label,valid='invalid','Некорректный срок',False
    elif dated:
        code,label,valid='on_time','В срок',True
    else:
        code,label,valid='missing','Срок не назначен',True
    return dict(deadline=dict(code=code,label=label,valid=valid),nearest=dated[0] if dated else None,
                open_tasks=dated+invalid+missing,closed_tasks=closed,task_deadlines=details)


def register_tasks(app, db, ticket_or_404, manager_label, now, parse):
    def get_task(ticket_id,task_id):
        item=db().execute(TASK_SELECT+' WHERE task.ticket_id=? AND task.id=?',(ticket_id,task_id)).fetchone()
        if item is None:
            abort(404,description='Задача не найдена в этой заявке.')
        return item

    def active_manager(value,current=None):
        if not value:
            return None,None
        item=db().execute('SELECT * FROM managers WHERE CAST(id AS TEXT)=?',(value,)).fetchone()
        if item is None or (not item['active'] and item['id']!=current):
            return None,'Выберите активного менеджера или «Не назначен».'
        return item['id'],None

    def next_candidates(ticket_id,task_id):
        result=[]
        for item in db().execute(TASK_SELECT+" WHERE task.ticket_id=? AND task.id!=? AND task.state='Открыта'",(ticket_id,task_id)).fetchall():
            try:
                if not item['due_at']:continue
                parsed=parse(item['due_at'])
            except (ValueError,TypeError,OverflowError):
                continue
            result.append((parsed,item))
        return [item for _,item in sorted(result,key=lambda pair:(pair[0],pair[1]['id']))]

    @app.route('/tickets/<int:ticket_id>/tasks/new',methods=['GET','POST'])
    @app.route('/tickets/<int:ticket_id>/tasks/<int:task_id>/<action>',methods=['GET','POST'])
    def task_form(ticket_id,task_id=None,action='new'):
        if action not in ('new','edit','reschedule','complete','cancel') or (task_id is not None and action=='new'):
            abort(404,description='Такого действия с задачей нет.')
        ticket=ticket_or_404(ticket_id)
        task=get_task(ticket_id,task_id) if task_id is not None else None
        values=dict(task) if task else dict(title='',due_at='',manager_id=ticket['manager_id'] if ticket['manager_active'] else '')
        errors={}
        key=request.form.get('submission_key','') if request.method=='POST' else secrets.token_hex(32)
        if request.method=='POST':
            connection=db()
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                ticket=ticket_or_404(ticket_id)
                task=get_task(ticket_id,task_id) if task_id is not None else None
                if action=='new':
                    if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
                        errors['submission_key']='Форма устарела. Откройте создание задачи заново.'
                    else:
                        existing=connection.execute('SELECT ticket_id FROM tasks WHERE submission_key=?',(key,)).fetchone()
                        if existing:
                            if existing['ticket_id']!=ticket_id:
                                abort(400,description='Ключ формы относится к другой заявке.')
                            return redirect(url_for('detail',ticket_id=ticket_id),code=303)
                if ticket['status']=='Закрыта':
                    abort(409,description='В закрытой заявке задачи изменять нельзя. Сначала откройте заявку повторно.')
                if task and task['state']!='Открыта':
                    abort(409,description='Выполненную или отменённую задачу изменять нельзя.')
                values={name:request.form.get(name,'').strip() for name in
                        ('title','manager_id','due_at','note','next_mode','next_task_id','next_title','next_due_at','next_manager_id')}
                manager_id=None
                if action in ('new','edit'):
                    if not 1<=len(values['title'])<=200:
                        errors['title']='Название задачи: от 1 до 200 символов.'
                    manager_id,error=active_manager(values['manager_id'],task['manager_id'] if task else None)
                    if error: errors['manager_id']=error
                if action in ('new','reschedule'):
                    error=validate_due(values['due_at'])
                    if error: errors['due_at']=error
                if action in ('reschedule','complete','cancel') and not 1<=len(values['note'])<=10000:
                    errors['note']='Укажите результат или причину: от 1 до 10000 символов.'
                initial_completion=action=='complete' and task['service_type']==INITIAL
                next_choice=None;next_manager_id=None
                if initial_completion:
                    if values['next_mode']=='existing':
                        next_choice=next((item for item in next_candidates(ticket_id,task_id) if str(item['id'])==values['next_task_id']),None)
                        if next_choice is None:
                            errors['next_step']='Выберите другую открытую задачу этой заявки с корректным сроком.'
                    elif values['next_mode']=='new':
                        if not 1<=len(values['next_title'])<=200:errors['next_title']='Название следующей задачи: от 1 до 200 символов.'
                        error=validate_due(values['next_due_at'])
                        if error:errors['next_due_at']=error
                        next_manager_id,error=active_manager(values['next_manager_id'])
                        if error:errors['next_manager_id']=error
                    else:
                        errors['next_step']='Для первичной обработки нужен следующий шаг: выберите существующую задачу или создайте новую.'
                if not errors:
                    instant=now()
                    timestamp=instant.isoformat(timespec='seconds')
                    if initial_completion and values['next_mode']=='new':
                        next_key=hashlib.sha256(f"next-step:{task_id}:{task['submission_key']}".encode()).hexdigest()
                        next_id=connection.execute('''INSERT INTO tasks
                            (ticket_id,title,manager_id,due_at,created_at,submission_key) VALUES (?,?,?,?,?,?)''',
                            (ticket_id,values['next_title'],next_manager_id,values['next_due_at'],timestamp,next_key)).lastrowid
                        next_choice=get_task(ticket_id,next_id)
                        append_event(connection,ticket_id,'task.created',instant,
                                     {'task':task_snapshot(connection,next_choice),'text':'Следующий шаг после первичной обработки'},task_id=next_id)
                    if action=='new':
                        cursor=connection.execute('''INSERT INTO tasks
                            (ticket_id,title,manager_id,due_at,created_at,submission_key) VALUES (?,?,?,?,?,?)''',
                            (ticket_id,values['title'],manager_id,values['due_at'],timestamp,key))
                        created=get_task(ticket_id,cursor.lastrowid)
                        append_event(connection,ticket_id,'task.created',instant,
                                     {'task':task_snapshot(connection,created)},task_id=created['id'])
                    elif action=='edit':
                        connection.execute('UPDATE tasks SET title=?,manager_id=? WHERE id=? AND ticket_id=?',
                                           (values['title'],manager_id,task_id,ticket_id))
                        changes=[]
                        if task['title']!=values['title']:
                            changes.append(change('Название',task['title'],values['title']))
                        if task['manager_id']!=manager_id:
                            changes.append(change('Ответственный',manager_snapshot(connection,task['manager_id']),
                                                  manager_snapshot(connection,manager_id)))
                        if changes:
                            append_event(connection,ticket_id,'task.updated',instant,
                                         {'task':task_snapshot(connection,get_task(ticket_id,task_id)),
                                          'changes':changes},task_id=task_id)
                    elif action=='reschedule':
                        connection.execute('UPDATE tasks SET due_at=?,last_reschedule_reason=? WHERE id=? AND ticket_id=?',
                                           (values['due_at'],values['note'],task_id,ticket_id))
                        changes=[]
                        for field,label,new_value in [('due_at','Срок',values['due_at']),
                                                      ('last_reschedule_reason','Последняя причина переноса',values['note'])]:
                            if task[field]!=new_value:
                                changes.append(change(label,task[field],new_value))
                        if changes:
                            append_event(connection,ticket_id,'task.rescheduled',instant,
                                         {'task':task_snapshot(connection,get_task(ticket_id,task_id)),
                                          'text':values['note'],'changes':changes},task_id=task_id)
                    else:
                        connection.execute('UPDATE tasks SET state=?,finished_at=?,result=? WHERE id=? AND ticket_id=?',
                                           ('Выполнена' if action=='complete' else 'Отменена',timestamp,values['note'],task_id,ticket_id))
                        updated=get_task(ticket_id,task_id)
                        details={'task':task_snapshot(connection,updated),'text':values['note'],
                                 'changes':[change('Состояние',task['state'],updated['state'])]}
                        if next_choice is not None:details['next_task']=task_snapshot(connection,next_choice)
                        append_event(connection,ticket_id,'task.completed' if action=='complete' else 'task.cancelled',instant,
                                     details,task_id=task_id)
            if not errors:
                flash('Задача сохранена.','success')
                return redirect(url_for('detail',ticket_id=ticket_id),code=303)
        elif ticket['status']=='Закрыта' or (task and task['state']!='Открыта'):
            abort(409,description='Задача или заявка уже закрыта; изменение недоступно.')
        managers=db().execute('SELECT * FROM managers WHERE active=1 OR id=? ORDER BY id',
                              (task['manager_id'] if task else None,)).fetchall()
        candidates=next_candidates(ticket_id,task_id) if task and task['service_type']==INITIAL and action=='complete' else []
        values.setdefault('next_mode','existing' if candidates else 'new')
        values.setdefault('next_manager_id',ticket['manager_id'] if ticket['manager_active'] else '')
        return render_template('task_form.html',ticket=ticket,task=task,action=action,values=values,errors=errors,
                               submission_key=key,manager_options=[(str(m['id']),manager_label(m)) for m in managers],
                               initial_completion=bool(task and task['service_type']==INITIAL and action=='complete'),
                               initial_help=INITIAL_HELP,next_candidates=candidates,
                               next_manager_options=[(str(m['id']),manager_label(m)) for m in db().execute('SELECT * FROM managers WHERE active=1 ORDER BY id')]),422 if errors else 200
