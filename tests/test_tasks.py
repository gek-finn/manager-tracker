import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from flask import template_rendered
from app import APP_TIMEZONE, migrate_database, parse_deadline
from test_support import create_app, authenticated_client
from task_features import TASK_MIGRATION, task_overview
from test_managers import legacy_database

NOW = datetime(2026,11,12,12,0,tzinfo=APP_TIMEZONE)


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'tasks.sqlite3'
        self.app=create_app(dict(TESTING=True,DATABASE=str(self.path),SECRET_KEY='test'))
        self.client=authenticated_client(self.app)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("INSERT INTO managers(name) VALUES ('Тест активный')")
                db.execute("INSERT INTO managers(name,active) VALUES ('Тест неактивный',0)")
                for i,status in enumerate(['В работе','Новая','Закрыта'],1):
                    db.execute('''INSERT INTO tickets(created_at,client,description,manager_id,status,priority,next_action,submission_key)
                        VALUES (?,?,?,?,?,?,?,?)''',('2026-11-01T09:00',f'Тест {i}','Тест',1,status,'Средний','old-value',str(i)))
        html=self.client.get('/tickets/1').get_data(as_text=True)
        self.csrf=re.search('name="csrf_token" value="([^"]+)"',self.client.get('/tickets/1/tasks/new').get_data(as_text=True))[1]
        self.clock=patch('app.application_now',return_value=NOW)
        self.mock_clock=self.clock.start();self.addCleanup(self.clock.stop)

    def rows(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory=sqlite3.Row
            return [dict(row) for row in db.execute(sql,args)]

    def post(self,path,**data):
        return self.client.post(path,data={'csrf_token':self.csrf,**data})

    def payload(self,ticket=1,**overrides):
        html=self.client.get(f'/tickets/{ticket}/tasks/new').get_data(as_text=True)
        key=re.search('name="submission_key" value="([^"]+)"',html)[1]
        return dict(title='Позвонить клиенту',manager_id='1',due_at='2026-11-12T11:00',submission_key=key,**overrides)

    def add(self,ticket=1,**overrides):
        data=self.payload(ticket);data.update(overrides)
        self.assertEqual(self.post(f'/tickets/{ticket}/tasks/new',**data).status_code,303)
        return self.rows('SELECT * FROM tasks ORDER BY id DESC LIMIT 1')[0]['id']

    def raw(self,due='',state='Открыта',ticket=1,manager=1):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                return db.execute('''INSERT INTO tasks(ticket_id,title,manager_id,due_at,state,created_at,submission_key)
                    VALUES (?,?,?,?,?,?,lower(hex(randomblob(32))))''',(ticket,'Тест срока',manager,due,state,NOW.isoformat())).lastrowid

    def context(self,path='/'):
        contexts=[]
        def capture(sender,template,context,**extra):contexts.append(context)
        template_rendered.connect(capture,self.app,weak=False)
        try:
            response=self.client.get(path)
        finally:template_rendered.disconnect(capture,self.app)
        self.assertEqual(response.status_code,200)
        return contexts[-1],response.get_data(as_text=True)

    def edit_ticket(self,ticket=1,**overrides):
        values=dict(client='Тест',description='Тест',contact='',status='В работе',priority='Средний',manager_id='1')
        values.update(overrides)
        return self.post(f'/tickets/{ticket}/edit',**values)

    def test_create_default_assignment_duplicate_and_validation(self):
        html=self.client.get('/tickets/1/tasks/new').get_data(as_text=True)
        self.assertIn('value="1" selected',html)
        self.assertNotIn('value="2"',html)
        data=self.payload()
        for field,value in [('title',' '),('title','x'*201),('due_at',''),('due_at','2026-02-30T10:00'),
                            ('manager_id','2'),('manager_id','999'),('submission_key','bad')]:
            with self.subTest(field=field,value=value):
                self.assertEqual(self.post('/tickets/1/tasks/new',**{**data,field:value}).status_code,422)
        self.assertEqual(self.rows('SELECT * FROM tasks'),[])
        data['title']='  Позвонить  '
        self.assertEqual(self.post('/tickets/1/tasks/new',**data).status_code,303)
        self.assertEqual(self.post('/tickets/1/tasks/new',**data).status_code,303)
        self.assertEqual(len(self.rows('SELECT * FROM tasks')),1)
        self.assertEqual(self.rows('SELECT * FROM tasks')[0]['title'],'Позвонить')

    def test_edit_does_not_change_due_and_keeps_current_inactive(self):
        task=self.raw('2026-11-12T11:00',manager=2)
        self.assertEqual(self.post(f'/tickets/1/tasks/{task}/edit',title='Новое',manager_id='2',due_at='bad').status_code,303)
        row=self.rows('SELECT * FROM tasks')[0]
        self.assertEqual((row['title'],row['manager_id'],row['due_at']),('Новое',2,'2026-11-12T11:00'))
        other=self.add()
        self.assertEqual(self.post(f'/tickets/1/tasks/{other}/edit',title='Тест',manager_id='2').status_code,422)
        self.assertEqual(self.post(f'/tickets/1/tasks/{task}/edit',title='Без менеджера',manager_id='').status_code,303)
        self.assertIsNone(self.rows('SELECT manager_id FROM tasks WHERE id=?',(task,))[0]['manager_id'])

    def test_reschedule_requires_reason_and_only_keeps_last(self):
        task=self.raw('bad')
        path=f'/tickets/1/tasks/{task}/reschedule'
        for data in [dict(due_at='2026-12-01T10:00',note=' '),dict(due_at='bad',note='Причина')]:
            self.assertEqual(self.post(path,**data).status_code,422)
        for reason in ('Первая причина','Последняя причина'):
            self.assertEqual(self.post(path,due_at='2026-12-01T10:00',note=reason).status_code,303)
        row=self.rows('SELECT * FROM tasks')[0]
        self.assertEqual(row['last_reschedule_reason'],'Последняя причина')
        self.assertIn('Последняя причина',self.client.get('/tickets/1').get_data(as_text=True))

    def test_complete_cancel_and_terminal_restrictions(self):
        for action,state in [('complete','Выполнена'),('cancel','Отменена')]:
            task=self.add()
            path=f'/tickets/1/tasks/{task}'
            self.assertEqual(self.post(path+'/'+action,note=' ').status_code,422)
            self.assertEqual(self.post(path+'/'+action,note='Результат или причина').status_code,303)
            row=self.rows('SELECT * FROM tasks WHERE id=?',(task,))[0]
            self.assertEqual(row['state'],state)
            self.assertEqual(row['finished_at'],NOW.isoformat(timespec='seconds'))
            for forbidden in ('edit','reschedule','complete','cancel'):
                self.assertEqual(self.post(path+'/'+forbidden,title='bad',manager_id='',due_at='2026-12-01T10:00',note='bad').status_code,409)
            self.assertEqual(self.rows('SELECT * FROM tasks WHERE id=?',(task,))[0],row)
        context,_=self.context('/tickets/1')
        self.assertEqual(context['deadline']['code'],'missing')

    def test_csrf_xss_and_foreign_ticket_task_id(self):
        task=self.add(title='<script>task</script>')
        html=self.client.get('/tickets/1').get_data(as_text=True)
        self.assertIn('&lt;script&gt;task&lt;/script&gt;',html)
        self.assertNotIn('<script>task</script>',html)
        for action in ('edit','reschedule','complete','cancel'):
            self.assertEqual(self.post(f'/tickets/2/tasks/{task}/{action}',title='bad',note='bad',due_at='2026-12-01T10:00').status_code,404)
            self.assertEqual(self.client.post(f'/tickets/1/tasks/{task}/{action}',data={'note':'bad'}).status_code,400)
        self.assertEqual(self.client.post('/tickets/1/tasks/new',data=self.payload()).status_code,400)
        self.assertEqual(self.client.get(f'/tickets/1/tasks/{task}/delete').status_code,404)
        key=self.rows('SELECT submission_key FROM tasks')[0]['submission_key']
        self.assertEqual(self.post('/tickets/2/tasks/new',**{**self.payload(2),'submission_key':key}).status_code,400)

    def test_earliest_due_and_open_then_finished_order(self):
        missing=self.raw()
        future=self.raw('2026-11-15T12:00')
        past=self.raw('2026-11-01T12:00')
        finished=self.raw('2026-10-01T12:00',state='Выполнена')
        context,_=self.context('/tickets/1')
        self.assertEqual(context['nearest']['id'],past)
        self.assertEqual([t['id'] for t in context['open_tasks']],[past,future,missing])
        self.assertEqual([t['id'] for t in context['closed_tasks']],[finished])

    def test_deadline_boundaries_mixed_invalid_and_missing(self):
        for due,expected in [('2026-11-12T11:59','overdue'),('2026-11-12T12:00','on_time'),('2026-11-12T12:01','on_time')]:
            ticket=dict(status='В работе')
            tasks=[dict(id=1,state='Открыта',due_at=due,created_at=NOW.isoformat(),finished_at=None)]
            self.assertEqual(task_overview(ticket,tasks,NOW,parse_deadline)['deadline']['code'],expected)
        self.raw()
        self.raw('bad')
        context,_=self.context('/tickets/1')
        self.assertEqual(context['deadline']['code'],'invalid')
        self.assertIsNone(context['nearest'])
        equal=self.raw('2026-11-12T12:00')
        context,_=self.context('/tickets/1')
        self.assertEqual(context['deadline']['code'],'invalid')
        self.assertEqual(context['nearest']['id'],equal)
        overdue=self.raw('2026-11-12T11:59')
        context,_=self.context('/tickets/1')
        self.assertEqual(context['deadline']['code'],'overdue')
        self.assertEqual(context['nearest']['id'],overdue)

    def test_filters_clock_once_and_closed_invalid_included(self):
        self.raw('bad',ticket=1)
        self.raw('2026-11-12T11:59',ticket=2)
        self.raw('bad',ticket=3)
        self.mock_clock.reset_mock()
        context,_=self.context('/?overdue=not_overdue')
        self.mock_clock.assert_called_once_with()
        self.assertEqual([t['id'] for t in context['tickets']],[3])
        context,html=self.context('/?manager=1&status=Новая&overdue=overdue')
        self.assertEqual([t['id'] for t in context['tickets']],[2])
        self.assertIn('value="overdue" selected',html)

    def test_close_reopen_no_new_tasks_and_independent_manager(self):
        task=self.add()
        self.assertEqual(self.edit_ticket(manager_id='').status_code,303)
        self.assertEqual(self.rows('SELECT manager_id FROM tasks')[0]['manager_id'],1)
        self.assertEqual(self.edit_ticket(status='Закрыта').status_code,303)
        row=self.rows('SELECT * FROM tasks')[0]
        self.assertEqual((row['state'],row['result'],row['finished_at']),('Отменена','Заявка закрыта',NOW.isoformat(timespec='seconds')))
        self.assertEqual(self.post('/tickets/1/tasks/new',submission_key='a'*64,title='Тест',due_at='2026-12-01T10:00').status_code,409)
        self.assertEqual(self.edit_ticket(status='В работе').status_code,303)
        self.assertEqual(self.rows('SELECT state FROM tasks')[0]['state'],'Отменена')
        self.assertEqual(len(self.rows('SELECT * FROM tasks')),1)
        self.add()

    def test_closing_failure_rolls_back_ticket_and_tasks(self):
        self.add()
        import app as app_module
        original_connect=sqlite3.connect
        def guarded(*args,**kwargs):
            connection=original_connect(*args,**kwargs)
            connection.set_authorizer(lambda action,a,b,d,s:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_UPDATE and a=='tasks' else sqlite3.SQLITE_OK)
            return connection
        with patch.object(app_module.sqlite3,'connect',side_effect=guarded):
            with self.assertLogs('app',level='ERROR'):
                self.assertEqual(self.edit_ticket(status='Закрыта').status_code,503)
        self.assertEqual(self.rows('SELECT status FROM tickets WHERE id=1')[0]['status'],'В работе')
        self.assertEqual(self.rows('SELECT state FROM tasks')[0]['state'],'Открыта')

    def test_delete_cascades_and_old_field_is_not_current_source(self):
        task=self.add()
        self.assertEqual(self.post('/tickets/1/delete').status_code,303)
        self.assertEqual(self.rows('SELECT * FROM tasks'),[])
        context,_=self.context('/tickets/2')
        self.assertEqual(context['deadline']['code'],'missing')
        self.assertEqual(self.rows('SELECT next_action FROM tickets WHERE id=2')[0]['next_action'],'old-value')
        self.assertNotIn('name="next_action"',self.client.get('/tickets/2/edit').get_data(as_text=True))


class TaskMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'legacy.sqlite3'
        legacy_database(self.path,['Анна']*6)
        with closing(sqlite3.connect(self.path)) as db:
            with patch('app.migrate_tasks'), patch('app.migrate_events'), patch('app.migrate_automation'):
                migrate_database(db)
            with db:
                db.execute('UPDATE managers SET active=0')
                for i,value in enumerate(['2026-11-12T11:00','2026-11-15T12:00','bad','','2026-11-01T10:00','bad'],1):
                    db.execute('UPDATE tickets SET next_action=?,status=? WHERE id=?',(value,'Закрыта' if i>=5 else 'Новая',i))

    def test_transfer_preserves_originals_unknown_time_and_no_duplicates(self):
        with closing(sqlite3.connect(self.path)) as db:
            before=list(db.execute('SELECT * FROM tickets ORDER BY id'))
            managers=list(db.execute('SELECT * FROM managers'))
            with patch('app.application_now',return_value=NOW):migrate_database(db)
            self.assertEqual(list(db.execute('SELECT * FROM tickets ORDER BY id')),before)
            self.assertEqual(list(db.execute('SELECT * FROM managers')),managers)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks WHERE state='Открыта'").fetchone()[0],3)
            self.assertTrue(all(row[0] is None for row in db.execute("SELECT finished_at FROM tasks WHERE state='Отменена'")))
            self.assertTrue(all('фактическое время отмены неизвестно' in row[0] for row in db.execute("SELECT result FROM tasks WHERE state='Отменена'")))
            self.assertEqual(db.execute('SELECT DISTINCT manager_id FROM tasks').fetchall(),[(1,)])
            self.assertEqual(db.execute('SELECT DISTINCT created_at FROM tasks').fetchall(),[(NOW.isoformat(timespec='seconds'),)])
            with db:db.execute("UPDATE tasks SET state='Выполнена',result='Тест' WHERE id=1")
            migrate_database(db)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],5)
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=1').fetchone()[0],'Выполнена')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=?',(TASK_MIGRATION,)).fetchone()[0],1)

    def test_failed_transfer_rolls_back_entire_task_schema(self):
        with closing(sqlite3.connect(self.path)) as db:
            before=list(db.execute('SELECT * FROM tickets'))
            db.set_authorizer(lambda action,a,b,d,s:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and a=='tasks' else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError):migrate_database(db)
            db.set_authorizer(None)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone())
            self.assertIsNone(db.execute('SELECT version FROM schema_migrations WHERE version=?',(TASK_MIGRATION,)).fetchone())
            self.assertEqual(list(db.execute('SELECT * FROM tickets')),before)
            migrate_database(db)

    def test_empty_database(self):
        path=Path(self.temp.name)/'empty.sqlite3'
        app=create_app(dict(TESTING=True,DATABASE=str(path)))
        self.assertEqual(authenticated_client(app).get('/').status_code,200)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)


if __name__=='__main__':unittest.main()
