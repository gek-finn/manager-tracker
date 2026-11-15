"""Transactional local workflow rules, without schedulers or external sending."""
from datetime import datetime, timedelta
import hashlib
import re
import secrets

from flask import abort, flash, redirect, render_template, request, url_for
from event_features import append_event, change, manager_snapshot, task_snapshot

AUTOMATION_MIGRATION='stage_4b_automatic_tasks_v1'
INITIAL='initial_processing'
FOLLOWUP='proposal_followup'
PROPOSAL_STATUS='КП отправлено'
INITIAL_HELP='Проверить обращение, связаться с клиентом, записать результат и определить следующий шаг. Это срок первой обработки, а не завершения продажи.'


def migrate_automation_base(connection,now):
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=?',(AUTOMATION_MIGRATION,)).fetchone():
        return
    connection.execute("ALTER TABLE tasks ADD COLUMN service_type TEXT NOT NULL DEFAULT 'ordinary'")
    connection.execute('ALTER TABLE tasks ADD COLUMN trigger_event_id INTEGER REFERENCES ticket_events(id) ON DELETE SET NULL')
    connection.execute('CREATE UNIQUE INDEX tasks_trigger_event_unique ON tasks(trigger_event_id) WHERE trigger_event_id IS NOT NULL')
    connection.execute("CREATE UNIQUE INDEX tasks_initial_ticket_unique ON tasks(ticket_id) WHERE service_type='initial_processing'")
    connection.execute('''CREATE TABLE automation_settings (
        id INTEGER PRIMARY KEY CHECK(id=1),
        initial_hours INTEGER NOT NULL CHECK(initial_hours BETWEEN 1 AND 720),
        proposal_hours INTEGER NOT NULL CHECK(proposal_hours BETWEEN 1 AND 720))''')
    connection.execute('INSERT INTO automation_settings VALUES (1,2,24)')
    connection.execute('INSERT INTO schema_migrations VALUES (?,?)',(AUTOMATION_MIGRATION,now.isoformat(timespec='seconds')))


def get_settings(connection):
    row=connection.execute('SELECT initial_hours,proposal_hours FROM automation_settings WHERE id=1').fetchone()
    return dict(initial_hours=row[0],proposal_hours=row[1])


def validate_due(value):
    try:
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?',value):
            raise ValueError
        datetime.fromisoformat(value)
    except (ValueError,TypeError):
        return 'Укажите корректные дату и время срока.'
    return None


def create_automatic_task(connection,ticket_id,service_type,manager_id,instant,event_id):
    title,setting=('Обработать новую заявку','initial_hours') if service_type==INITIAL else ('Связаться после отправки КП','proposal_hours')
    due=(instant+timedelta(hours=get_settings(connection)[setting])).replace(tzinfo=None).isoformat(timespec='seconds')
    task_id=connection.execute('''INSERT INTO tasks
        (ticket_id,title,manager_id,due_at,created_at,submission_key,service_type,trigger_event_id)
        VALUES (?,?,?,?,?,?,?,?)''',(ticket_id,title,manager_id,due,instant.isoformat(timespec='seconds'),
        hashlib.sha256(f'auto:{service_type}:{event_id}'.encode()).hexdigest(),service_type,event_id)).lastrowid
    task=connection.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    append_event(connection,ticket_id,'task.created',instant,
                 {'task':task_snapshot(connection,task),'text':INITIAL_HELP if service_type==INITIAL else 'Повторный контакт после зафиксированной отправки КП',
                  'trigger_event_id':event_id},task_id=task_id)
    return task_id


def assign_initial_if_empty(connection,ticket_id,manager_id,instant):
    manager=connection.execute('SELECT active FROM managers WHERE id=?',(manager_id,)).fetchone()
    if manager is None or not manager[0]:
        return
    tasks=connection.execute("SELECT * FROM tasks WHERE ticket_id=? AND service_type=? AND state='Открыта' AND manager_id IS NULL",(ticket_id,INITIAL)).fetchall()
    for task in tasks:
        connection.execute('UPDATE tasks SET manager_id=? WHERE id=?',(manager_id,task['id']))
        updated=connection.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone()
        append_event(connection,ticket_id,'task.updated',instant,
                     {'task':task_snapshot(connection,updated),'text':'Назначен вместе с ответственным заявки: незакреплённая первичная обработка',
                      'changes':[change('Ответственный',None,manager_snapshot(connection,manager_id))]},task_id=task['id'])


def register_automation(app,db,ticket_or_404,now):
    @app.route('/settings/automation',methods=['GET','POST'])
    def automation_settings():
        values={**get_settings(db()), **get_control_settings(db())};errors={}
        if request.method=='POST':
            fields = ('new_hours','inactivity_hours') if request.form.get('group')=='control' else ('initial_hours','proposal_hours')
            values.update({name:request.form.get(name,'').strip() for name in fields})
            parsed={}
            for name in fields:
                raw=values[name]
                try:
                    if not re.fullmatch(r'[0-9]{1,10}',raw):raise ValueError
                    value=int(raw)
                    if not 1<=value<=720:raise ValueError
                    parsed[name]=value
                except ValueError:
                    errors[name]='Интервал должен быть целым числом от 1 до 720 часов.'
            if not errors:
                with db():
                    if request.form.get('group')=='control':
                        db().execute('UPDATE control_settings SET new_hours=?,inactivity_hours=? WHERE id=1',
                                     (parsed['new_hours'],parsed['inactivity_hours']))
                    else:
                        db().execute('UPDATE automation_settings SET initial_hours=?,proposal_hours=? WHERE id=1',
                                     (parsed['initial_hours'],parsed['proposal_hours']))
                flash('Настройки сохранены. Сроки существующих задач не изменены.','success')
                return redirect(url_for('automation_settings'),code=303)
        return render_template('automation_settings.html',values=values,errors=errors),422 if errors else 200

    @app.route('/tickets/<int:ticket_id>/proposal/<mode>',methods=['GET','POST'])
    def proposal_form(ticket_id,mode):
        if mode not in ('send','repeat'):abort(404,description='Неизвестное действие с КП.')
        ticket=ticket_or_404(ticket_id)
        errors={};comment='';confirmed=False
        key=request.form.get('submission_key','') if request.method=='POST' else secrets.token_hex(32)
        if request.method=='POST':
            comment=request.form.get('comment','').strip()
            confirmed=request.form.get('confirmed')=='1'
            if not 1<=len(comment)<=2000:errors['comment']='Комментарий о предложении: от 1 до 2000 символов.'
            if not confirmed:errors['confirmed']='Подтвердите, что КП действительно отправлено клиенту.'
            if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):errors['submission_key']='Обновите форму отправки КП.'
            with db():
                db().execute('BEGIN IMMEDIATE')
                ticket=ticket_or_404(ticket_id)
                if not errors:
                    existing=db().execute('SELECT ticket_id,event_type FROM ticket_events WHERE submission_key=?',(key,)).fetchone()
                    if existing:
                        if existing['ticket_id']!=ticket_id or existing['event_type']!='proposal.sent':
                            abort(400,description='Ключ формы относится к другому действию или заявке.')
                        return redirect(url_for('detail',ticket_id=ticket_id),code=303)
                check_state(ticket,mode)
                if not ticket['manager_active']:
                    errors['manager']='Для отправки КП назначьте заявке активного ответственного менеджера.'
                if not errors:
                    instant=now()
                    old_tasks=db().execute("SELECT * FROM tasks WHERE ticket_id=? AND service_type=? AND state='Открыта'",(ticket_id,FOLLOWUP)).fetchall()
                    for task in old_tasks:
                        reason='Заменена задачей после повторной отправки КП'
                        db().execute("UPDATE tasks SET state='Отменена',finished_at=?,result=? WHERE id=?",
                                     (instant.isoformat(timespec='seconds'),reason,task['id']))
                        updated=db().execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone()
                        append_event(db(),ticket_id,'task.cancelled',instant,
                                     {'task':task_snapshot(db(),updated),'text':reason,
                                      'changes':[change('Состояние','Открыта','Отменена')]},task_id=task['id'])
                    changes=[change('Статус',ticket['status'],PROPOSAL_STATUS)] if ticket['status']!=PROPOSAL_STATUS else []
                    event_id=append_event(db(),ticket_id,'proposal.sent',instant,
                                          {'text':comment,'changes':changes,'repeat':mode=='repeat',
                                           'manager':manager_snapshot(db(),ticket['manager_id'])},submission_key=key)
                    db().execute('UPDATE tickets SET status=? WHERE id=?',(PROPOSAL_STATUS,ticket_id))
                    create_automatic_task(db(),ticket_id,FOLLOWUP,ticket['manager_id'],instant,event_id)
            if not errors:
                flash('Отправка КП зафиксирована. Задача повторного контакта создана.','success')
                return redirect(url_for('detail',ticket_id=ticket_id),code=303)
        else:
            check_state(ticket,mode)
        return render_template('proposal_form.html',ticket=ticket,mode=mode,errors=errors,comment=comment,
                               confirmed=confirmed,submission_key=key,interval=get_settings(db())['proposal_hours']),422 if errors else 200

    def check_state(ticket,mode):
        if ticket['status']=='Закрыта':abort(409,description='В закрытой заявке отправку КП отмечать нельзя.')
        if (mode=='repeat')!=(ticket['status']==PROPOSAL_STATUS):
            abort(409,description='Статус заявки изменился. Откройте актуальную форму отправки КП из карточки.')


CONTROL_MIGRATION = 'stage_4v_control_settings_v1'


def migrate_automation(connection, now):
    migrate_automation_base(connection, now)
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=?', (CONTROL_MIGRATION,)).fetchone():
        return
    connection.execute("""CREATE TABLE control_settings (
        id INTEGER PRIMARY KEY CHECK(id=1),
        new_hours INTEGER NOT NULL CHECK(new_hours BETWEEN 1 AND 720),
        inactivity_hours INTEGER NOT NULL CHECK(inactivity_hours BETWEEN 1 AND 720))""")
    connection.execute('INSERT INTO control_settings VALUES (1,2,24)')
    connection.execute('INSERT INTO schema_migrations VALUES (?,?)', (CONTROL_MIGRATION,now.isoformat()))


def get_control_settings(connection):
    row = connection.execute('SELECT new_hours,inactivity_hours FROM control_settings WHERE id=1').fetchone()
    return dict(new_hours=row[0], inactivity_hours=row[1])
