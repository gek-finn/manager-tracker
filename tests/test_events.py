from contextlib import closing, contextmanager
from datetime import datetime, timedelta
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
from event_features import EVENT_MIGRATION

NOW=datetime(2026,11,12,15,0,tzinfo=APP_TIMEZONE)


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.sqlite3'
        self.time=NOW
        self.clock=patch('app.application_now',side_effect=lambda:self.time)
        self.mock_clock=self.clock.start();self.addCleanup(self.clock.stop)
        self.config=dict(TESTING=True,DATABASE=str(self.path),SECRET_KEY='test')
        self.app=create_app(self.config);self.client=authenticated_client(self.app)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("INSERT INTO managers(name) VALUES ('Первое имя')")
                db.execute("INSERT INTO managers(name) VALUES ('Второе имя')")
        html=self.client.get('/tickets/new').get_data(as_text=True)
        self.token=re.search('name="csrf_token" value="([^"]+)"',html)[1]

    def key(self,path):
        return re.search('name="submission_key" value="([^"]+)"',self.client.get(path).get_data(as_text=True))[1]

    def post(self,path,**data):
        return self.client.post(path,data={'csrf_token':self.token,**data})

    def ticket_data(self):
        return dict(client='Тест',contact='test@example.com',description='Описание',manager_id='1',
                    status='Новая',priority='Средний',submission_key=self.key('/tickets/new'))

    def ticket(self):
        result=self.post('/tickets/new',**self.ticket_data())
        self.assertEqual(result.status_code,303)
        return int(result.headers['Location'].rsplit('/',1)[1])

    def edit(self,ticket_id,**updates):
        current=self.rows('SELECT * FROM tickets WHERE id=?',(ticket_id,))[0]
        data={field:current[field] if current[field] is not None else '' for field in
              ('client','contact','description','manager_id','status','priority')}
        data.update(updates)
        return self.post(f'/tickets/{ticket_id}/edit',**data)

    def task(self,ticket_id=1,**overrides):
        data=dict(title='Первое название',manager_id='1',due_at='2026-11-20T10:00',
                  submission_key=self.key(f'/tickets/{ticket_id}/tasks/new'))
        data.update(overrides)
        self.assertEqual(self.post(f'/tickets/{ticket_id}/tasks/new',**data).status_code,303)
        return self.rows('SELECT * FROM tasks ORDER BY id DESC LIMIT 1')[0]['id']

    def manual(self,ticket_id=1,**overrides):
        data=dict(kind='manual.call',text='Обсудили условия',submission_key=self.key(f'/tickets/{ticket_id}'))
        data.update(overrides)
        return self.post(f'/tickets/{ticket_id}/actions',**data)

    def rows(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute(sql,args)]

    def events(self,ticket_id=1):
        rows=self.rows('SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY id',(ticket_id,))
        for row in rows:row['payload']=json.loads(row['details'])
        return rows

    def context(self,ticket_id=1):
        captured=[]
        def capture(sender,template,context,**extra):captured.append(context)
        template_rendered.connect(capture,self.app,weak=False)
        try:response=self.client.get(f'/tickets/{ticket_id}')
        finally:template_rendered.disconnect(capture,self.app)
        self.assertEqual(response.status_code,200)
        return captured[-1],response.get_data(as_text=True)

    def snapshot(self):
        return {table:self.rows(f'SELECT * FROM {table} ORDER BY id') for table in ('tickets','tasks','ticket_events')}

    @contextmanager
    def fail_event_writes(self,delete=False):
        original=sqlite3.connect
        def connect(*args,**kwargs):
            db=original(*args,**kwargs)
            db.set_authorizer(lambda code,a,b,d,s:sqlite3.SQLITE_DENY if
                a=='ticket_events' and code==(sqlite3.SQLITE_DELETE if delete else sqlite3.SQLITE_INSERT) else sqlite3.SQLITE_OK)
            return db
        with patch('app.sqlite3.connect',side_effect=connect),self.assertLogs('app',level='ERROR'):
            yield

    def test_creation_real_event_snapshot_and_duplicate(self):
        data=self.ticket_data()
        self.assertEqual(self.post('/tickets/new',**data).status_code,303)
        self.assertEqual(self.post('/tickets/new',**data).status_code,303)
        event=self.events()[0]
        self.assertEqual(len(self.events()),2)
        self.assertEqual((event['event_type'],event['source'],event['meaningful']),('ticket.created','application',1))
        self.assertEqual(event['occurred_at'],NOW.isoformat(timespec='seconds'))
        self.assertEqual(event['payload']['ticket']['manager'],{'id':1,'name':'Первое имя'})
        self.assertEqual(self.context()[0]['last_activity']['event_type'],'ticket.created')

    def test_ticket_changes_grouping_status_and_assignment(self):
        self.ticket();self.time+=timedelta(hours=1)
        self.assertEqual(self.edit(1,status='В работе',manager_id='2',client='Другой',priority='Высокий').status_code,303)
        events=self.events()
        self.assertEqual([e['event_type'] for e in events],['ticket.created','task.created','ticket.status_changed','ticket.manager_changed','ticket.updated'])
        self.assertEqual(len(events[-1]['payload']['changes']),2)
        self.assertTrue(all(e['occurred_at']==self.time.isoformat(timespec='seconds') for e in events[2:]))
        self.assertTrue(all(not e['meaningful'] for e in events[1:]))
        self.assertEqual(self.context()[0]['last_activity']['occurred_at'],NOW.isoformat(timespec='seconds'))
        count=len(events)
        self.assertEqual(self.edit(1).status_code,303)
        self.assertEqual(len(self.events()),count)
        self.assertEqual(self.edit(1,manager_id='').status_code,303)
        self.assertIsNone(self.events()[-1]['payload']['changes'][0]['after'])

    def test_task_events_and_no_changes(self):
        self.ticket();task=self.task()
        path=f'/tickets/1/tasks/{task}'
        self.assertEqual(self.post(path+'/edit',title='Новое название',manager_id='2').status_code,303)
        count=len(self.events())
        self.assertEqual(self.post(path+'/edit',title='Новое название',manager_id='2').status_code,303)
        self.assertEqual(len(self.events()),count)
        self.assertEqual(self.post(path+'/reschedule',due_at='2026-11-25T10:00',note='Договорились позже').status_code,303)
        count=len(self.events())
        self.assertEqual(self.post(path+'/reschedule',due_at='2026-11-25T10:00',note='Договорились позже').status_code,303)
        self.assertEqual(len(self.events()),count)
        self.time+=timedelta(hours=2)
        self.assertEqual(self.post(path+'/complete',note='Получен ответ клиента').status_code,303)
        types=[e['event_type'] for e in self.events()]
        self.assertEqual(types,['ticket.created','task.created','task.created','task.updated','task.rescheduled','task.completed'])
        self.assertEqual(self.events()[-1]['payload']['text'],'Получен ответ клиента')
        self.assertEqual(self.context()[0]['last_activity']['event_type'],'task.completed')
        self.assertEqual(self.context()[0]['last_activity']['occurred_at'],self.time.isoformat(timespec='seconds'))

    def test_cancel_is_not_meaningful(self):
        self.ticket();task=self.task();self.time+=timedelta(hours=1)
        self.assertEqual(self.post(f'/tickets/1/tasks/{task}/cancel',note='Потребность отпала').status_code,303)
        self.assertEqual(self.events()[-1]['event_type'],'task.cancelled')
        self.assertEqual(self.events()[-1]['meaningful'],0)
        self.assertEqual(self.context()[0]['last_activity']['event_type'],'ticket.created')

    def test_all_manual_types_time_is_server_time_and_text_escaped(self):
        self.ticket()
        for kind in ('manual.call','manual.message','manual.comment'):
            self.time+=timedelta(minutes=1)
            self.assertEqual(self.manual(kind=kind,text='  <script>test</script>  ',occurred_at='1999-01-01',meaningful='0',source='application').status_code,303)
            event=self.events()[-1]
            self.assertEqual((event['event_type'],event['source'],event['meaningful']),(kind,'manual',1))
            self.assertEqual(event['occurred_at'],self.time.isoformat(timespec='seconds'))
            self.assertEqual(event['payload']['text'],'<script>test</script>')
        context,html=self.context()
        self.assertEqual(context['last_activity']['event_type'],'manual.comment')
        self.assertIn('&lt;script&gt;test&lt;/script&gt;',html)
        self.assertNotIn('<script>test</script>',html)
        self.assertIn('Журнал не фиксирует отдельного исполнителя события',html)

    def test_manual_validation_csrf_duplicate_and_other_ticket_key(self):
        self.ticket();self.ticket()
        key=self.key('/tickets/1')
        base=dict(kind='manual.call',text='Ответ получен',submission_key=key)
        for field,value in [('kind','ticket.created'),('kind',''),('text',' '),('text','x'*2001),('submission_key','bad')]:
            self.assertEqual(self.post('/tickets/1/actions',**{**base,field:value}).status_code,422)
        self.assertEqual(self.client.post('/tickets/1/actions',data=base).status_code,400)
        self.assertEqual(len(self.events()),2)
        self.assertEqual(self.post('/tickets/1/actions',**base).status_code,303)
        self.assertEqual(self.post('/tickets/1/actions',**base).status_code,303)
        self.assertEqual(len(self.events()),3)
        self.assertEqual(self.post('/tickets/2/actions',**base).status_code,400)
        self.assertEqual(len(self.events(2)),2)

    def test_closed_history_read_only_and_creation_replay(self):
        data=self.ticket_data();self.post('/tickets/new',**data)
        self.assertEqual(self.manual().status_code,303)
        self.assertEqual(self.edit(1,status='Закрыта').status_code,303)
        before=self.events()
        self.assertEqual(self.post('/tickets/1/actions',kind='manual.call',text='Запрещено',submission_key='a'*64).status_code,409)
        self.assertEqual(self.events(),before)
        self.assertEqual(self.post('/tickets/new',**data).status_code,303)
        self.assertEqual(self.events(),before)
        context,html=self.context()
        self.assertNotIn('id="action-text"',html)
        self.assertIn('История доступна только для чтения',html)

    def test_snapshots_survive_manager_and_task_renames(self):
        self.ticket();task=self.task()
        before=self.events()
        self.assertEqual(self.post('/managers/1/edit',name='Имя после переименования').status_code,303)
        self.assertEqual(self.events(),before)
        self.assertEqual(self.post(f'/tickets/1/tasks/{task}/edit',title='Название после переименования',manager_id='1').status_code,303)
        created=next(e for e in self.events() if e['event_type']=='task.created' and e['task_id']==task)
        self.assertEqual(created['payload']['task']['title'],'Первое название')
        self.assertEqual(created['payload']['task']['manager']['name'],'Первое имя')
        self.assertEqual(self.events()[0]['payload']['ticket']['manager']['name'],'Первое имя')
        context,html=self.context()
        self.assertIn('Первое название',html)
        self.assertIn('Первое имя',html)

    def test_order_same_timestamp_uses_id_and_latest_meaningful(self):
        self.ticket();self.manual(kind='manual.call');self.manual(kind='manual.message')
        self.task();self.edit(1,priority='Высокий')
        context,_=self.context()
        self.assertEqual([e['id'] for e in context['history']],sorted([e['id'] for e in context['history']],reverse=True))
        self.assertEqual(context['last_activity']['event_type'],'manual.message')
        self.assertEqual(len({e['occurred_at'] for e in self.events()}),1)

    def test_close_reopen_and_automatic_cancellation_events(self):
        self.ticket();self.task();self.task(title='Вторая')
        self.time+=timedelta(days=1)
        self.assertEqual(self.edit(1,status='Закрыта').status_code,303)
        latest=[e for e in self.events() if e['occurred_at']==self.time.isoformat(timespec='seconds')]
        self.assertEqual([e['event_type'] for e in latest],['ticket.closed','task.cancelled','task.cancelled','task.cancelled'])
        self.assertTrue(all(e['meaningful']==0 for e in latest))
        self.assertTrue(all(e['payload']['text']=='Заявка закрыта' for e in latest[1:]))
        self.assertEqual(self.edit(1,status='В работе').status_code,303)
        self.assertEqual(self.events()[-1]['event_type'],'ticket.reopened')
        self.assertEqual(self.context()[0]['last_activity']['event_type'],'ticket.created')
        self.assertTrue(all(t['state']=='Отменена' for t in self.rows('SELECT * FROM tasks')))

    def test_event_failure_rolls_back_create_and_ticket_edit(self):
        data=self.ticket_data()
        before=self.snapshot()
        with self.fail_event_writes():
            self.assertEqual(self.post('/tickets/new',**data).status_code,503)
        self.assertEqual(self.snapshot(),before)
        self.ticket();self.task();before=self.snapshot()
        with self.fail_event_writes():
            self.assertEqual(self.edit(1,status='Закрыта',client='Не сохранить').status_code,503)
        self.assertEqual(self.snapshot(),before)

    def test_event_failure_rolls_back_all_task_actions_and_manual(self):
        self.ticket()
        key=self.key('/tickets/1/tasks/new');before=self.snapshot()
        with self.fail_event_writes():
            self.assertEqual(self.post('/tickets/1/tasks/new',title='Тест',manager_id='1',due_at='2026-11-20T10:00',submission_key=key).status_code,503)
        self.assertEqual(self.snapshot(),before)
        task=self.task()
        for action,data in [('edit',dict(title='Не сохранить',manager_id='2')),
                            ('reschedule',dict(due_at='2026-11-25T10:00',note='Причина')),
                            ('complete',dict(note='Результат')),('cancel',dict(note='Причина'))]:
            before=self.snapshot()
            with self.fail_event_writes():
                self.assertEqual(self.post(f'/tickets/1/tasks/{task}/{action}',**data).status_code,503)
            self.assertEqual(self.snapshot(),before)
        key=self.key('/tickets/1');before=self.snapshot()
        with self.fail_event_writes():
            self.assertEqual(self.post('/tickets/1/actions',kind='manual.comment',text='Тест',submission_key=key).status_code,503)
        self.assertEqual(self.snapshot(),before)

    def test_delete_cascades_atomically_and_no_event_editing_routes(self):
        self.ticket();self.task();self.manual()
        event_id=self.events()[0]['id']
        for action in ('edit','delete'):
            self.assertEqual(self.post(f'/tickets/1/events/{event_id}/{action}',text='bad').status_code,404)
        before=self.snapshot()
        with self.fail_event_writes(delete=True):
            self.assertEqual(self.post('/tickets/1/delete').status_code,503)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.post('/tickets/1/delete').status_code,303)
        self.assertEqual(self.snapshot(),{'tickets':[],'tasks':[],'ticket_events':[]})


class EventMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'old.sqlite3'
        self.config=dict(TESTING=True,DATABASE=str(self.path),SECRET_KEY='test')
        with patch('app.migrate_events'),patch('app.application_now',return_value=NOW):create_app(self.config)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("INSERT INTO managers(name) VALUES ('Старый менеджер')")
                for i in range(2):
                    db.execute('''INSERT INTO tickets(created_at,client,description,manager_id,status,priority,submission_key)
                        VALUES ('2020-01-01T10:00',?,'Старое описание',1,?,'Средний',?)''',
                        (f'Старая заявка {i}','Закрыта' if i else 'В работе',str(i)))
                    db.execute('''INSERT INTO tasks(ticket_id,title,manager_id,due_at,state,created_at,finished_at,result,submission_key)
                        VALUES (?,'Старая задача',1,'2020-01-02T10:00','Выполнена','2020-01-01T10:00','2020-01-03T10:00','Старый результат',?)''',(i+1,str(i)))

    def snapshot(self,db):
        return {t:list(db.execute(f'SELECT * FROM {t} ORDER BY id')) for t in ('tickets','managers','tasks')}

    def test_old_data_one_technical_event_each_repeat_no_fake_history(self):
        with closing(sqlite3.connect(self.path)) as db:
            before=self.snapshot(db)
            with patch('app.application_now',return_value=NOW):migrate_database(db)
            self.assertEqual(self.snapshot(db),before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ticket_events').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT DISTINCT event_type,meaningful,occurred_at FROM ticket_events').fetchall(),
                             [('journal.enabled',0,NOW.isoformat(timespec='seconds'))])
            records=list(db.execute('SELECT * FROM ticket_events'))
            migrate_database(db)
            self.assertEqual(list(db.execute('SELECT * FROM ticket_events')),records)
        app=create_app(self.config);client=authenticated_client(app)
        html=client.get('/tickets/1').get_data(as_text=True)
        self.assertIn('Нет данных: журнал ведётся с 12.11.2026 15:00',html)
        self.assertNotIn('Задача выполнена',html)
        self.assertIn('Предыдущая история действий недоступна',html)

    def test_failed_migration_rolls_back_schema_and_technical_events(self):
        with closing(sqlite3.connect(self.path)) as db:
            before=self.snapshot(db)
            db.set_authorizer(lambda code,a,b,d,s:sqlite3.SQLITE_DENY if code==sqlite3.SQLITE_INSERT and a=='ticket_events' else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError):migrate_database(db)
            db.set_authorizer(None)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='ticket_events'").fetchone())
            self.assertIsNone(db.execute('SELECT version FROM schema_migrations WHERE version=?',(EVENT_MIGRATION,)).fetchone())
            self.assertEqual(self.snapshot(db),before)
            migrate_database(db)

    def test_empty_database_no_technical_events(self):
        empty=Path(self.temp.name)/'empty.sqlite3'
        app=create_app(dict(TESTING=True,DATABASE=str(empty)))
        self.assertEqual(authenticated_client(app).get('/').status_code,200)
        with closing(sqlite3.connect(empty)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ticket_events').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=?',(EVENT_MIGRATION,)).fetchone()[0],1)


if __name__=='__main__':unittest.main()
