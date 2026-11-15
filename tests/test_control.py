from datetime import timedelta
from contextlib import closing
import sqlite3
from unittest.mock import patch
from test_automation import AutomationTests, NOW
from app import migrate_database, parse_deadline
from event_features import control_info, CONTROL_LABELS, control_matches
from automation_features import get_control_settings


# Reuse helpers without rerunning inherited tests.
class ControlTests(AutomationTests):
    pass
for name in dir(AutomationTests):
    if name.startswith('test_'): setattr(ControlTests,name,None)

del AutomationTests

class ControlTests(ControlTests):
    def info(self,ticket=1):
        return control_info(self.rows('SELECT * FROM tickets WHERE id=?',(ticket,))[0],
            self.rows('SELECT * FROM tasks WHERE ticket_id=?',(ticket,)),
            self.rows('SELECT * FROM ticket_events WHERE ticket_id=?',(ticket,)),self.time,parse_deadline,
            self.rows('SELECT * FROM control_settings')[0])

    def execute(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute('PRAGMA foreign_keys=ON');db.execute(sql,args)

    def test_each_reason_independently_and_combinations(self):
        ticket=dict(status='В работе',manager_id=1,created_at=NOW.isoformat())
        task=dict(state='Открыта',due_at=(NOW+timedelta(days=3)).isoformat())
        event=dict(occurred_at=NOW.isoformat(),event_type='manual.call',meaningful=1,details='{}')
        def calc(t=ticket,tasks=None,events=None):
            return control_info(t,[task] if tasks is None else tasks,[event] if events is None else events,NOW,parse_deadline,dict(new_hours=2,inactivity_hours=24))
        self.assertEqual(calc()['codes'],[])
        cases={
            'unassigned':dict(t={**ticket,'manager_id':None}),
            'new_stale':dict(t={**ticket,'status':'Новая','created_at':(NOW-timedelta(hours=3)).isoformat()}),
            'no_next_task':dict(tasks=[]),
            'missing_due':dict(tasks=[{**task,'due_at':''}]),
            'overdue_task':dict(tasks=[{**task,'due_at':(NOW-timedelta(seconds=1)).isoformat()}]),
            'inactive':dict(events=[{**event,'occurred_at':(NOW-timedelta(hours=25)).isoformat()}]),
            'no_activity':dict(events=[]),
            'invalid_due':dict(tasks=[{**task,'due_at':'broken'}]),
            'invalid_created':dict(t={**ticket,'created_at':'broken'}),
        }
        for code,kwargs in cases.items():
            with self.subTest(code=code):self.assertEqual(calc(**kwargs)['codes'],[code])
        for state in ('Выполнена','Отменена'):
            self.assertEqual(calc(tasks=[{**task,'state':state,'due_at':'bad'}])['codes'],['no_next_task'])
        mixed=calc(tasks=[task,{**task,'due_at':''},{**task,'due_at':'bad'},{**task,'due_at':(NOW-timedelta(days=2)).isoformat()}])
        self.assertEqual(set(mixed['codes']),{'missing_due','invalid_due','overdue_task'})
        self.assertTrue(mixed['problems']);self.assertTrue(mixed['data'])
        self.assertEqual(calc(t={**ticket,'status':'Закрыта','created_at':'bad'},tasks=[],events=[])['codes'],[])

    def test_thresholds_strict_boundary_timezone_reopen(self):
        self.ticket();self.time=NOW+timedelta(hours=2)
        self.assertNotIn('new_stale',self.info()['codes'])
        self.time+=timedelta(microseconds=1);self.assertIn('new_stale',self.info()['codes'])
        self.time=NOW+timedelta(hours=24);self.assertNotIn('inactive',self.info()['codes'])
        self.time+=timedelta(microseconds=1);self.assertIn('inactive',self.info()['codes'])
        self.edit(status='В работе');self.edit(status='Новая');self.assertIn('new_stale',self.info()['codes'])
        self.execute('UPDATE managers SET active=0 WHERE id=1')
        self.assertNotIn('unassigned',self.info()['codes'])

    def test_settings_validation_independent_no_data_mutation(self):
        self.ticket();before=self.snapshot()
        for raw in ('','0','721','1.2','-1','2e1','x'):
            self.assertEqual(self.post('/settings/automation',group='control',new_hours=raw,inactivity_hours='24').status_code,422)
        self.assertEqual(self.post('/settings/automation',group='control',new_hours='720',inactivity_hours='1').status_code,303)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.client.post('/settings/automation',data=dict(group='control',new_hours=2,inactivity_hours=24)).status_code,400)
        with closing(sqlite3.connect(self.path)) as db:
            migrate_database(db);self.assertEqual(get_control_settings(db),dict(new_hours=720,inactivity_hours=1))
        self.time=NOW+timedelta(hours=3)
        self.assertNotIn('new_stale',self.info()['codes']);self.assertIn('inactive',self.info()['codes'])

    def test_corrupt_events_dates_do_not_crash_or_prove_activity(self):
        self.ticket()
        for details in ('null','[]','{"changes":null}','{"changes":42}','{"task":42}'):
            self.execute('UPDATE ticket_events SET details=?',(details,))
            for url in ('/','/tickets/1'):
                self.assertEqual(self.client.get(url).status_code,200)
        self.execute("UPDATE ticket_events SET occurred_at='bad',details='{}'")
        self.execute("UPDATE tickets SET created_at='bad'")
        self.assertTrue({'invalid_created','invalid_event','no_activity'}<=set(self.info()['codes']))
        self.assertEqual(self.client.get('/tickets/1').status_code,200)
        self.execute('UPDATE ticket_events SET occurred_at=?,details=?',((NOW+timedelta(days=1)).isoformat(),'{}'))
        self.assertIn('no_activity',self.info()['codes'])

    def test_filters_all_options_and_intersection_get_reset_no_writes(self):
        self.ticket(manager_id='');self.ticket(status='Закрыта');self.ticket(status='В работе')
        self.time=NOW+timedelta(days=2)
        before=self.snapshot()
        for code in ('','problems','data','clear',*CONTROL_LABELS):
            response=self.client.get('/',query_string=dict(control=code))
            self.assertEqual(response.status_code,200)
            self.assertIn(f'value="{code}" selected',response.get_data(as_text=True))
            expected=[i for i in (1,2,3) if control_matches(self.info(i),code)]
            for i in (1,2,3):
                self.assertEqual(f'class="number" href="/tickets/{i}"' in response.get_data(as_text=True),i in expected)
        html=self.client.get('/?control=unassigned&manager=1&status=Новая&overdue=overdue').get_data(as_text=True)
        self.assertIn('По выбранным условиям заявки не найдены',html)
        self.assertIn('href="/">Сбросить',html)
        self.assertEqual(self.client.get('/?control=unknown').data,self.client.get('/').data)
        self.assertEqual(self.snapshot(),before)
        with patch('app.application_now',return_value=self.time) as clock:
            self.client.get('/');self.assertEqual(clock.call_count,1)

    def test_activity_only_meaningful_events(self):
        self.ticket();self.time=NOW+timedelta(days=3)
        self.edit(description='Изменение');self.assertIn('inactive',self.info()['codes'])
        primary=self.primary()
        self.assertEqual(self.post(f"/tickets/1/tasks/{primary['id']}/reschedule",due_at='2026-12-01T10:00',note='Перенос').status_code,303)
        self.assertIn('inactive',self.info()['codes'])
        self.post('/tickets/1/actions',kind='manual.call',text='Связались',submission_key=self.key('/tickets/1'))
        self.assertNotIn('inactive',self.info()['codes'])
        self.time+=timedelta(days=3);self.send();self.assertNotIn('inactive',self.info()['codes'])

    def test_legacy_no_history_and_migration_repeat(self):
        self.ticket();self.execute('DELETE FROM ticket_events')
        self.assertIn('no_activity',self.info()['codes'])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE control_settings');db.execute("DELETE FROM schema_migrations WHERE version='stage_4v_control_settings_v1'");db.commit()
            before={t:db.execute(f'SELECT * FROM {t}').fetchall() for t in ('tickets','tasks','managers','ticket_events')}
            migrate_database(db);migrate_database(db)
            self.assertEqual(get_control_settings(db),dict(new_hours=2,inactivity_hours=24))
            self.assertEqual(before,{t:db.execute(f'SELECT * FROM {t}').fetchall() for t in before})

    def test_completion_refreshes_activity_and_schedule_edits_do_not(self):
        self.ticket();task=self.ordinary();self.time=NOW+timedelta(days=3)
        self.assertIn('inactive',self.info()['codes'])
        response=self.post(f'/tickets/1/tasks/{task}/complete',note='Обсудили запрос')
        self.assertEqual(response.status_code,303)
        self.assertNotIn('inactive',self.info()['codes'])

    def test_invalid_latest_event_does_not_prove_inactivity(self):
        self.ticket();self.time=NOW+timedelta(days=3)
        self.post('/tickets/1/actions',kind='manual.call',text='Ответ клиента',submission_key=self.key('/tickets/1'))
        self.execute("UPDATE ticket_events SET occurred_at='bad' WHERE event_type='manual.call'")
        self.assertIn('no_activity',self.info()['codes']);self.assertNotIn('inactive',self.info()['codes'])
        self.assertIn('Нет данных',self.client.get('/tickets/1').get_data(as_text=True))

    def test_no_n_plus_one_queries_and_view_read_only(self):
        for _ in range(8):self.ticket()
        queries=[]
        original=sqlite3.connect
        def connect(*args,**kwargs):
            db=original(*args,**kwargs);db.set_trace_callback(queries.append);return db
        before=self.snapshot()
        with patch('app.sqlite3.connect',side_effect=connect):
            self.assertEqual(self.client.get('/?control=problems').status_code,200)
        selects=[q for q in queries if q.lstrip().upper().startswith('SELECT')]
        self.assertLessEqual(len(selects),6)
        self.assertEqual(before,self.snapshot())
