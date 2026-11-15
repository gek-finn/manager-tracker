from contextlib import closing
from datetime import datetime,timedelta
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from flask import template_rendered

from app import APP_TIMEZONE, migrate_database
from test_support import create_app, authenticated_client
from automation_features import AUTOMATION_MIGRATION,INITIAL,FOLLOWUP,PROPOSAL_STATUS,get_settings

NOW=datetime(2026,11,13,23,30,17,tzinfo=APP_TIMEZONE)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.sqlite3';self.time=NOW
        clock=patch('app.application_now',side_effect=lambda:self.time);clock.start();self.addCleanup(clock.stop)
        self.app=create_app(dict(TESTING=True,DATABASE=str(self.path),SECRET_KEY='test'));self.client=authenticated_client(self.app)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("INSERT INTO managers(name) VALUES ('Активный 1')")
                db.execute("INSERT INTO managers(name) VALUES ('Активный 2')")
                db.execute("INSERT INTO managers(name,active) VALUES ('Неактивный',0)")
        html=self.client.get('/tickets/new').get_data(as_text=True)
        self.token=re.search('name="csrf_token" value="([^"]+)"',html)[1]

    def rows(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory=sqlite3.Row;return [dict(r) for r in db.execute(sql,args)]

    def snapshot(self):
        return {t:self.rows(f'SELECT * FROM {t} ORDER BY id') for t in ('tickets','tasks','ticket_events','automation_settings')}

    def post(self,path,**data):return self.client.post(path,data={'csrf_token':self.token,**data})

    def key(self,path):
        response=self.client.get(path);self.assertEqual(response.status_code,200)
        return re.search('name="submission_key" value="([^"]+)"',response.get_data(as_text=True))[1]

    def payload(self,**overrides):
        data=dict(client='Тест',description='Тест',contact='test@example.com',manager_id='1',status='Новая',priority='Средний',submission_key=self.key('/tickets/new'))
        data.update(overrides);return data

    def ticket(self,**overrides):
        response=self.post('/tickets/new',**self.payload(**overrides));self.assertEqual(response.status_code,303)
        return int(response.headers['Location'].rsplit('/',1)[1])

    def edit(self,ticket=1,**updates):
        current=self.rows('SELECT * FROM tickets WHERE id=?',(ticket,))[0]
        data={k:current[k] if current[k] is not None else '' for k in ('client','contact','description','status','priority','manager_id')}
        data.update(updates);return self.post(f'/tickets/{ticket}/edit',**data)

    def primary(self,ticket=1):return self.rows('SELECT * FROM tasks WHERE ticket_id=? AND service_type=?',(ticket,INITIAL))[0]

    def ordinary(self,ticket=1,title='Следующий шаг',due='2026-11-25T10:00',**overrides):
        data=dict(title=title,due_at=due,manager_id='1',submission_key=self.key(f'/tickets/{ticket}/tasks/new'));data.update(overrides)
        self.assertEqual(self.post(f'/tickets/{ticket}/tasks/new',**data).status_code,303)
        return self.rows('SELECT * FROM tasks ORDER BY id DESC LIMIT 1')[0]['id']

    def proposal_data(self,ticket=1,mode='send',**overrides):
        data=dict(comment='Предложение передано клиенту',confirmed='1',submission_key=self.key(f'/tickets/{ticket}/proposal/{mode}'))
        data.update(overrides);return data

    def send(self,ticket=1,mode='send',**overrides):
        data=self.proposal_data(ticket,mode,**overrides)
        self.assertEqual(self.post(f'/tickets/{ticket}/proposal/{mode}',**data).status_code,303)
        return data

    def context(self,ticket=1):
        captured=[]
        def receiver(sender,template,context,**extra):captured.append(context)
        template_rendered.connect(receiver,self.app,weak=False)
        try:response=self.client.get(f'/tickets/{ticket}')
        finally:template_rendered.disconnect(receiver,self.app)
        self.assertEqual(response.status_code,200);return captured[-1],response.get_data(as_text=True)

    def test_settings_defaults_validation_csrf_and_repeat_migration(self):
        self.assertEqual(self.rows('SELECT * FROM automation_settings')[0],dict(id=1,initial_hours=2,proposal_hours=24))
        for value in ('','0','721','-1','1.5','1e2','x','9'*10000):
            self.assertEqual(self.post('/settings/automation',initial_hours=value,proposal_hours='24').status_code,422)
        self.assertEqual(self.post('/settings/automation',initial_hours='720',proposal_hours='1').status_code,303)
        with closing(sqlite3.connect(self.path)) as db:
            migrate_database(db);self.assertEqual(get_settings(db),dict(initial_hours=720,proposal_hours=1))
        self.assertEqual(self.client.post('/settings/automation',data=dict(initial_hours=2,proposal_hours=24)).status_code,400)

    def test_new_ticket_atomic_primary_with_and_without_manager_calendar_hours(self):
        self.ticket();self.ticket(manager_id='')
        for ticket_id,manager_id in ((1,1),(2,None)):
            task=self.primary(ticket_id)
            self.assertEqual(task['manager_id'],manager_id)
            self.assertEqual(task['title'],'Обработать новую заявку')
            self.assertEqual(task['state'],'Открыта')
            self.assertEqual(datetime.fromisoformat(task['due_at']),NOW.replace(tzinfo=None)+timedelta(hours=2))
            event=self.rows('SELECT * FROM ticket_events WHERE id=?',(task['trigger_event_id'],))[0]
            self.assertEqual(event['event_type'],'ticket.created')
            self.assertEqual(event['ticket_id'],ticket_id)
        self.assertEqual(len(self.rows('SELECT * FROM tasks')),2)

    def test_closed_ticket_edit_reopen_and_existing_data_no_initial_task(self):
        self.ticket(status='Закрыта')
        self.assertEqual(self.rows('SELECT * FROM tasks'),[])
        self.assertEqual(self.edit(status='В работе').status_code,303)
        self.assertEqual(self.rows('SELECT * FROM tasks'),[])
        self.assertEqual(self.edit(client='Новое имя').status_code,303)
        self.assertEqual(self.rows('SELECT * FROM tasks'),[])

    def test_create_retry_exactly_one_ticket_task_and_two_events(self):
        data=self.payload()
        self.assertEqual(self.post('/tickets/new',**data).status_code,303)
        before=self.snapshot()
        self.assertEqual(self.post('/tickets/new',**data).status_code,303)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(len(before['tickets']),1);self.assertEqual(len(before['tasks']),1);self.assertEqual(len(before['ticket_events']),2)

    def test_setting_changes_only_future_tasks(self):
        self.ticket();old=self.primary()
        self.assertEqual(self.post('/settings/automation',initial_hours='5',proposal_hours='48').status_code,303)
        self.ticket();new=self.primary(2)
        self.assertEqual(self.primary(),old)
        self.assertEqual(datetime.fromisoformat(new['due_at']),NOW.replace(tzinfo=None)+timedelta(hours=5))
        self.send();task=self.rows('SELECT * FROM tasks WHERE service_type=?',(FOLLOWUP,))[0]
        self.assertEqual(datetime.fromisoformat(task['due_at']),NOW.replace(tzinfo=None)+timedelta(hours=48))
        self.assertEqual(self.post('/settings/automation',initial_hours='2',proposal_hours='24').status_code,303)
        self.assertEqual(self.rows('SELECT * FROM tasks WHERE id=?',(task['id'],))[0],task)

    def test_assignment_exception_only_unassigned_open_initial(self):
        self.ticket(manager_id='');primary=self.primary();other=self.ordinary(manager_id='')
        self.assertEqual(self.edit(manager_id='1').status_code,303)
        self.assertEqual(self.primary()['manager_id'],1)
        self.assertIsNone(self.rows('SELECT manager_id FROM tasks WHERE id=?',(other,))[0]['manager_id'])
        events=self.rows("SELECT * FROM ticket_events WHERE event_type='task.updated'")
        self.assertEqual(len(events),1);self.assertEqual(events[0]['task_id'],primary['id'])
        self.assertEqual(self.edit(manager_id='2').status_code,303)
        self.assertEqual(self.primary()['manager_id'],1)
        self.assertEqual(self.post(f"/tickets/1/tasks/{primary['id']}/edit",title='Переименована',manager_id='').status_code,303)
        self.assertEqual(self.edit(client='Сохранение без нового назначения').status_code,303)
        self.assertIsNone(self.primary()['manager_id'])

    def test_initial_completion_existing_next_step_and_ordinary_unaffected(self):
        self.ticket();primary=self.primary();next_id=self.ordinary()
        path=f"/tickets/1/tasks/{primary['id']}/complete"
        self.assertEqual(self.post(path,note='Обработано',next_mode='existing',next_task_id=str(next_id)).status_code,303)
        self.assertEqual(self.primary()['state'],'Выполнена')
        event=self.rows("SELECT * FROM ticket_events WHERE event_type='task.completed'")[0]
        self.assertEqual(json.loads(event['details'])['next_task']['id'],next_id)
        self.assertEqual(self.post(f'/tickets/1/tasks/{next_id}/complete',note='Готово').status_code,303)

    def test_initial_completion_new_next_step_and_repeat(self):
        self.ticket();primary=self.primary();path=f"/tickets/1/tasks/{primary['id']}/complete"
        data=dict(note='Позвонил, клиент не ответил',next_mode='new',next_title='Повторно позвонить',next_due_at='2026-11-25T12:00',next_manager_id='2')
        self.assertEqual(self.post(path,**data).status_code,303)
        tasks=self.rows('SELECT * FROM tasks ORDER BY id')
        self.assertEqual(len(tasks),2)
        self.assertEqual((tasks[-1]['service_type'],tasks[-1]['manager_id'],tasks[-1]['title']),('ordinary',2,'Повторно позвонить'))
        before=self.snapshot();self.assertEqual(self.post(path,**data).status_code,409);self.assertEqual(self.snapshot(),before)

    def test_missing_result_next_step_invalid_or_foreign_next_step_rejected(self):
        self.ticket();self.ticket();primary=self.primary();path=f"/tickets/1/tasks/{primary['id']}/complete"
        foreign=self.primary(2)['id']
        for data in [dict(note=''),dict(note='Ответ'),dict(note='Ответ',next_mode='existing',next_task_id=str(primary['id'])),
                     dict(note='Ответ',next_mode='existing',next_task_id=str(foreign)),
                     dict(note='Ответ',next_mode='new',next_title=' ',next_due_at='bad',next_manager_id='3')]:
            before=self.snapshot();self.assertEqual(self.post(path,**data).status_code,422);self.assertEqual(self.snapshot(),before)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                bad=db.execute("INSERT INTO tasks(ticket_id,title,due_at,created_at,submission_key) VALUES (1,'Неверный срок','bad','2026-11-12','bad-task')").lastrowid
        self.assertEqual(self.post(path,note='Ответ',next_mode='existing',next_task_id=str(bad)).status_code,422)

    def test_service_type_not_title_and_cannot_be_forged(self):
        self.ticket();primary=self.primary();ordinary=self.ordinary(title='Обработать новую заявку')
        self.assertEqual(self.post(f"/tickets/1/tasks/{primary['id']}/edit",title='Другое название',manager_id='1',service_type='ordinary').status_code,303)
        self.assertEqual(self.primary()['service_type'],INITIAL)
        self.assertEqual(self.post(f"/tickets/1/tasks/{primary['id']}/complete",note='Готово').status_code,422)
        self.assertEqual(self.post(f'/tickets/1/tasks/{ordinary}/complete',note='Готово').status_code,303)

    def test_close_is_alternative_to_initial_completion(self):
        self.ticket();self.assertEqual(self.edit(status='Закрыта').status_code,303)
        self.assertEqual(self.primary()['state'],'Отменена')
        self.assertEqual(self.edit(status='Новая').status_code,303)
        self.assertEqual(self.primary()['state'],'Отменена')
        self.assertEqual(len(self.rows('SELECT * FROM tasks')),1)

    def test_proposal_action_status_activity_and_retry(self):
        self.ticket();self.time+=timedelta(hours=3)
        data=self.send();before=self.snapshot()
        self.assertEqual(self.post('/tickets/1/proposal/send',**data).status_code,303);self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.rows('SELECT status FROM tickets')[0]['status'],PROPOSAL_STATUS)
        events=self.rows("SELECT * FROM ticket_events WHERE event_type='proposal.sent'")
        self.assertEqual(len(events),1);self.assertEqual(events[0]['meaningful'],1)
        self.assertEqual(json.loads(events[0]['details'])['changes'][0]['after'],PROPOSAL_STATUS)
        self.assertEqual(self.rows("SELECT * FROM ticket_events WHERE event_type='ticket.status_changed'"),[])
        task=self.rows('SELECT * FROM tasks WHERE service_type=?',(FOLLOWUP,))[0]
        self.assertEqual(task['trigger_event_id'],events[0]['id']);self.assertEqual(task['manager_id'],1)
        self.assertEqual(self.context()[0]['last_activity']['event_type'],'proposal.sent')
        before=self.snapshot();self.assertEqual(self.edit().status_code,303);self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.edit(status='В работе').status_code,303)

    def test_status_cannot_bypass_action_and_proposal_filter(self):
        self.assertEqual(self.post('/tickets/new',**self.payload(status=PROPOSAL_STATUS)).status_code,422)
        self.ticket();self.assertEqual(self.edit(status=PROPOSAL_STATUS).status_code,422)
        self.assertNotIn(PROPOSAL_STATUS,self.client.get('/tickets/1/edit').get_data(as_text=True))
        self.send()
        self.assertIn(PROPOSAL_STATUS,self.client.get('/?status='+PROPOSAL_STATUS).get_data(as_text=True))
        self.assertEqual(self.edit(status=PROPOSAL_STATUS).status_code,303)
        self.assertEqual(len(self.rows('SELECT * FROM tasks WHERE service_type=?',(FOLLOWUP,))),1)

    def test_repeat_cancels_only_prior_open_automatic_followup(self):
        self.ticket();self.send();first=self.rows('SELECT * FROM tasks WHERE service_type=?',(FOLLOWUP,))[0]
        manual=self.ordinary(title='Связаться после отправки КП')
        self.assertEqual(self.post(f"/tickets/1/tasks/{first['id']}/complete",note='Ответ получен').status_code,303)
        self.send(mode='repeat');second=self.rows('SELECT * FROM tasks WHERE service_type=? ORDER BY id DESC',(FOLLOWUP,))[0]
        self.time+=timedelta(hours=1);self.send(mode='repeat',comment='Обновлённое предложение')
        rows=self.rows('SELECT * FROM tasks WHERE service_type=? ORDER BY id',(FOLLOWUP,))
        self.assertEqual([t['state'] for t in rows],['Выполнена','Отменена','Открыта'])
        self.assertEqual(rows[1]['result'],'Заменена задачей после повторной отправки КП')
        self.assertEqual(self.rows('SELECT state FROM tasks WHERE id=?',(manual,))[0]['state'],'Открыта')
        self.assertEqual(self.primary()['state'],'Открыта')
        self.assertEqual(len(self.rows("SELECT * FROM ticket_events WHERE event_type='proposal.sent'")),3)

    def test_no_active_manager_confirmation_and_comment_required(self):
        self.ticket(manager_id='');data=self.proposal_data()
        self.assertEqual(self.post('/tickets/1/proposal/send',**data).status_code,422)
        self.assertEqual(self.edit(manager_id='1').status_code,303)
        for field,value in [('comment',' '),('comment','x'*2001),('confirmed',''),('submission_key','bad')]:
            self.assertEqual(self.post('/tickets/1/proposal/send',**{**data,field:value}).status_code,422)
        with closing(sqlite3.connect(self.path)) as db:
            with db:db.execute('UPDATE managers SET active=0 WHERE id=1')
        self.assertEqual(self.post('/tickets/1/proposal/send',**data).status_code,422)
        self.assertEqual(self.rows("SELECT * FROM ticket_events WHERE event_type='proposal.sent'"),[])

    def test_proposal_csrf_escaping_and_foreign_key_reuse(self):
        self.ticket();self.ticket();data=self.proposal_data(comment='<script>offer</script>')
        self.assertEqual(self.client.post('/tickets/1/proposal/send',data=data).status_code,400)
        self.assertEqual(self.post('/tickets/1/proposal/send',**data).status_code,303)
        self.assertEqual(self.post('/tickets/2/proposal/send',**data).status_code,400)
        self.assertEqual(self.post('/tickets/999/proposal/send',**data).status_code,404)
        html=self.context()[1];self.assertIn('&lt;script&gt;offer&lt;/script&gt;',html);self.assertNotIn('<script>offer</script>',html)
        self.assertEqual(self.edit(status='Закрыта').status_code,303)
        self.assertEqual(self.post('/tickets/1/proposal/repeat',comment='Нет',confirmed='1',submission_key='b'*64).status_code,409)

    def test_task_event_uniqueness_enforced_by_database(self):
        self.ticket();self.send();task=self.rows('SELECT * FROM tasks WHERE service_type=?',(FOLLOWUP,))[0]
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO tasks(ticket_id,title,due_at,created_at,submission_key,service_type,trigger_event_id) VALUES (1,'Дубль','2026-12-01T10:00','2026-11-13','different-key',?,?)",(FOLLOWUP,task['trigger_event_id']))

    def test_failure_rolls_back_ticket_auto_task_and_proposal_replacement(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_auto BEFORE INSERT ON ticket_events WHEN NEW.event_type='task.created' BEGIN SELECT RAISE(ABORT,'test event failure'); END")
        data=self.payload();before=self.snapshot()
        with self.assertLogs('app',level='ERROR'):self.assertEqual(self.post('/tickets/new',**data).status_code,503)
        self.assertEqual(self.snapshot(),before)
        with closing(sqlite3.connect(self.path)) as db:db.execute('DROP TRIGGER fail_auto')
        self.ticket();self.send();data=self.proposal_data(mode='repeat')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_auto BEFORE INSERT ON ticket_events WHEN NEW.event_type='task.created' BEGIN SELECT RAISE(ABORT,'test event failure'); END")
        before=self.snapshot()
        with self.assertLogs('app',level='ERROR'):self.assertEqual(self.post('/tickets/1/proposal/repeat',**data).status_code,503)
        self.assertEqual(self.snapshot(),before)

    def test_next_task_creation_and_completion_are_atomic(self):
        self.ticket();primary=self.primary()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_completion BEFORE INSERT ON ticket_events WHEN NEW.event_type='task.completed' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        before=self.snapshot()
        with self.assertLogs('app',level='ERROR'):
            self.assertEqual(self.post(f"/tickets/1/tasks/{primary['id']}/complete",note='Результат',next_mode='new',next_title='Перезвонить',next_due_at='2026-11-20T10:00',next_manager_id='1').status_code,503)
        self.assertEqual(self.snapshot(),before)

    def test_assignment_event_failure_rolls_back_parent_and_primary(self):
        self.ticket(manager_id='')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_assignment BEFORE INSERT ON ticket_events WHEN NEW.event_type='task.updated' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        before=self.snapshot()
        with self.assertLogs('app',level='ERROR'):
            self.assertEqual(self.edit(manager_id='1').status_code,503)
        self.assertEqual(self.snapshot(),before)

    def test_proposal_tasks_and_events_are_removed_with_ticket(self):
        self.ticket();self.send();self.send(mode='repeat')
        self.assertEqual(self.post('/tickets/1/delete').status_code,303)
        self.assertEqual(self.rows('SELECT * FROM tasks'),[])
        self.assertEqual(self.rows('SELECT * FROM ticket_events'),[])
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])


class AutomationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'old.sqlite3'
        with patch('app.migrate_automation'):create_app(dict(TESTING=True,DATABASE=str(self.path)))
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("INSERT INTO tickets(created_at,client,description,status,priority,submission_key) VALUES ('2020-01-01','Старая','Тест','Новая','Средний','old')")
                db.execute("INSERT INTO tasks(ticket_id,title,due_at,created_at,submission_key) VALUES (1,'Обработать новую заявку','2020-01-02T10:00','2020-01-01','old-task')")

    def test_migration_preserves_all_old_columns_and_no_backfill(self):
        with closing(sqlite3.connect(self.path)) as db:
            columns=[r[1] for r in db.execute('PRAGMA table_info(tasks)')]
            before=list(db.execute('SELECT * FROM tasks'));tickets=list(db.execute('SELECT * FROM tickets'));events=list(db.execute('SELECT * FROM ticket_events'))
            migrate_database(db)
            self.assertEqual(list(db.execute('SELECT '+','.join(columns)+' FROM tasks')),before)
            self.assertEqual(list(db.execute('SELECT * FROM tickets')),tickets);self.assertEqual(list(db.execute('SELECT * FROM ticket_events')),events)
            self.assertEqual(db.execute('SELECT service_type,trigger_event_id FROM tasks').fetchall(),[('ordinary',None)])
            self.assertEqual(get_settings(db),dict(initial_hours=2,proposal_hours=24))
            migrate_database(db);self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=?',(AUTOMATION_MIGRATION,)).fetchone()[0],1)

    def test_migration_failure_rolls_back_columns_and_settings(self):
        with closing(sqlite3.connect(self.path)) as db:
            before=list(db.execute('SELECT * FROM tasks'))
            db.set_authorizer(lambda code,a,b,d,s:sqlite3.SQLITE_DENY if code==sqlite3.SQLITE_INSERT and a=='automation_settings' else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError):migrate_database(db)
            db.set_authorizer(None)
            self.assertNotIn('service_type',[r[1] for r in db.execute('PRAGMA table_info(tasks)')])
            self.assertEqual(list(db.execute('SELECT * FROM tasks')),before)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='automation_settings'").fetchone())
            migrate_database(db)

    def test_empty_database(self):
        path=Path(self.temp.name)/'empty.sqlite3';app=create_app(dict(TESTING=True,DATABASE=str(path)))
        self.assertEqual(authenticated_client(app).get('/settings/automation').status_code,200)
        with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)


if __name__=='__main__':unittest.main()
