from contextlib import closing
from datetime import datetime,timedelta,timezone
import concurrent.futures
import csv
import io
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch
import test_dashboard as dashboard_tests
from app import migrate_database
from test_support import create_app, authenticated_client
from report_features import csv_cell,REPORT_MIGRATION,FORMAT_VERSION,CURRENT_HEADERS,SUMMARY_HEADERS

NOW=dashboard_tests.NOW

class ReportTests(unittest.TestCase):
    setUp=dashboard_tests.DashboardTests.setUp
    rows=dashboard_tests.DashboardTests.rows
    post=dashboard_tests.DashboardTests.post
    key=dashboard_tests.DashboardTests.key
    payload=dashboard_tests.DashboardTests.payload
    ticket=dashboard_tests.DashboardTests.ticket
    ordinary=dashboard_tests.DashboardTests.ordinary
    primary=dashboard_tests.DashboardTests.primary
    execute=dashboard_tests.DashboardTests.execute
    fixtures=dashboard_tests.DashboardTests.fixtures
    page=dashboard_tests.DashboardTests.page
    edit=dashboard_tests.DashboardTests.edit

    def summaries(self):return self.rows('SELECT * FROM daily_summaries ORDER BY local_date')
    def snapshot(self):return json.loads(self.summaries()[0]['payload'])
    def csv(self,path):
        response=self.client.get(path)
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.data.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(response.headers['Content-Type'],'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="manager-tracker-',response.headers['Content-Disposition'])
        return list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'),newline=''),delimiter=';'))

    def test_first_repeat_empty_and_no_historical_backfill(self):
        self.assertEqual(self.summaries(),[])
        self.page();first=self.summaries();self.assertEqual(len(first),1)
        payload=self.snapshot();self.assertEqual(payload['rows'],[])
        self.assertEqual(payload['format_version'],FORMAT_VERSION)
        self.assertEqual(payload['thresholds'],dict(new_hours=2,inactivity_hours=24))
        self.assertTrue(all(m['count']==0 for m in payload['metrics']))
        self.page();self.assertEqual(first,self.summaries())
        self.time+=timedelta(days=3);self.page()
        self.assertEqual([r['local_date'] for r in self.summaries()],['2026-11-13','2026-11-16'])
        self.assertIn('На момент формирования заявок, требующих внимания, не было',self.client.get('/summaries/1').get_data(as_text=True))

    def test_local_midnight_utc_offset_and_single_instant(self):
        self.time=datetime(2026,11,13,21,59,59,tzinfo=timezone.utc)
        with patch('app.application_now',return_value=self.time) as clock:
            self.page();self.assertEqual(clock.call_count,1)
        self.time+=timedelta(seconds=1);self.page()
        self.assertEqual([r['local_date'] for r in self.summaries()],['2026-11-13','2026-11-14'])
        self.assertEqual(self.summaries()[1]['formed_at'],'2026-11-14T00:00:00+02:00')

    def test_saved_metrics_rows_codes_labels_order_and_thresholds_agree(self):
        self.fixtures();context,_=self.page();payload=self.snapshot()
        self.assertEqual([r['ticket_id'] for r in payload['rows']],[1,3,2,6])
        self.assertEqual({m['code']:m['count'] for m in payload['metrics']},{m['code']:m['count'] for m in context['metrics']})
        for row in payload['rows']:
            key=row['ticket_id'];self.assertEqual(row['reasons'],context['controls'][key]['labels'])
        self.assertTrue(payload['rows'][1]['manager']['inactive'])
        self.assertEqual(payload['rows'][0]['activity']['code'],'ticket.created')
        self.assertEqual(payload['rows'][3]['activity']['code'],'no_activity')
        self.assertEqual(payload['rows'][0]['task']['id'],context['schedules'][1]['nearest']['id'])

    def test_snapshot_independent_of_edits_deletion_thresholds_and_renames(self):
        self.fixtures();self.page();original=self.summaries();old_csv=self.client.get('/summaries/1/export.csv').data
        self.edit(client='Новое имя',status='В работе')
        self.execute("UPDATE managers SET name='Переименован',active=0 WHERE id=1")
        self.execute("UPDATE tasks SET title='Новая задача',due_at='2027-01-01T12:00'")
        self.execute('UPDATE control_settings SET new_hours=720,inactivity_hours=720')
        self.assertIn('Открыть текущую карточку',self.client.get('/summaries/1').get_data(as_text=True))
        self.assertEqual(self.post('/tickets/1/delete').status_code,303)
        self.time+=timedelta(minutes=10);self.page()
        self.assertEqual(self.summaries(),original)
        self.assertEqual(self.client.get('/summaries/1/export.csv').data,old_csv)
        html=self.client.get('/summaries/1').get_data(as_text=True)
        self.assertIn('Исходная заявка удалена',html);self.assertNotIn('href="/tickets/1"',html)
        self.assertNotIn('Новое имя',html);self.assertNotIn('Переименован',html)
        self.assertIn('Сохранённый снимок, данные не обновляются',html)
        self.assertNotIn('data-metric=',html)

    def test_failed_insert_and_render_roll_back_and_panel_stays_available(self):
        self.fixtures()
        self.execute("CREATE TRIGGER fail_summary BEFORE INSERT ON daily_summaries BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertLogs('app',level='ERROR'):
            response=self.client.get('/dashboard')
        self.assertEqual(response.status_code,200);self.assertIn('Не удалось сохранить',response.get_data(as_text=True))
        self.assertEqual(self.summaries(),[])
        self.execute('DROP TRIGGER fail_summary')
        with patch('app.render_template',side_effect=RuntimeError('render failure')):
            with self.assertRaises(RuntimeError):self.client.get('/dashboard')
        self.assertEqual(self.summaries(),[])
        self.page();self.assertEqual(len(self.summaries()),1)

    def test_parallel_openings_unique_and_complete(self):
        self.fixtures();barrier=threading.Barrier(2)
        def visit(_):
            with authenticated_client(self.app) as client:
                barrier.wait(timeout=5)
                return client.get('/dashboard').status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(visit,range(2))),[200,200])
        self.assertEqual(len(self.summaries()),1);self.assertEqual(self.summaries()[0]['row_count'],4)
        self.assertEqual(len(self.snapshot()['rows']),4)
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO daily_summaries(local_date,formed_at,format_version,row_count,payload) SELECT local_date,formed_at,format_version,row_count,payload FROM daily_summaries")

    def test_other_pages_and_exports_never_create_summary(self):
        self.fixtures()
        for path in ('/','/tickets/1','/summaries','/tickets/export.csv','/settings/automation','/managers'):
            self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.summaries(),[])
        self.assertEqual(self.client.get('/summaries/999').status_code,404)
        self.assertEqual(self.client.get('/summaries/999/export.csv').status_code,404)
        self.assertEqual(self.summaries(),[])
        self.page();self.time+=timedelta(days=1)
        for path in ('/summaries','/summaries/1','/summaries/1/export.csv','/tickets/export.csv'):
            self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(len(self.summaries()),1)

    def test_summary_list_order_empty_state_and_today_link(self):
        self.assertIn('Первая появится после открытия',self.client.get('/summaries').get_data(as_text=True))
        _,html=self.page();self.assertIn('Сегодняшняя сводка',html)
        self.assertIn('могут отличаться',html)
        self.time+=timedelta(days=1);self.page()
        html=self.client.get('/summaries').get_data(as_text=True)
        self.assertLess(html.index('2026-11-14'),html.index('2026-11-13'))

    def test_current_csv_shared_filters_sort_and_unknown_values(self):
        self.fixtures()
        queries=['','?scope=active','?scope=closed','?manager=unassigned',
                 '?scope=active&manager=unassigned&status=Новая&overdue=overdue&control=new_stale',
                 '?scope=bad&manager=bad&status=bad&overdue=bad&control=bad',
                 '?scope=closed&control=problems']
        for query in queries:
            with self.subTest(query=query):
                ctx,_=self.page('/'+query);csv_rows=self.csv('/tickets/export.csv'+query)
                self.assertEqual(csv_rows[0],CURRENT_HEADERS)
                self.assertEqual([int(r[0]) for r in csv_rows[1:]],[t['id'] for t in ctx['tickets']])
                self.assertTrue(all(len(r)==len(CURRENT_HEADERS) for r in csv_rows))
        self.assertEqual(self.summaries(),[])
        for control in ('problems','data','clear','unassigned','new_stale','no_next_task','missing_due','overdue_task','inactive','no_activity','invalid_due','invalid_created','invalid_event'):
            ctx,_=self.page('/?control='+control)
            self.assertEqual([int(r[0]) for r in self.csv('/tickets/export.csv?control='+control)[1:]],[t['id'] for t in ctx['tickets']])

    def test_csv_formats_quotes_newlines_bom_dates_and_empty(self):
        self.assertEqual(self.csv('/tickets/export.csv'),[CURRENT_HEADERS])
        self.page();self.assertEqual(self.csv('/summaries/1/export.csv'),[SUMMARY_HEADERS])
        self.time+=timedelta(days=1)
        text='Демо; "Кавычки"\nНовая строка'
        self.ticket(client=text,manager_id='')
        csv_rows=self.csv('/tickets/export.csv');self.assertEqual(csv_rows[1][2],text)
        self.assertTrue(csv_rows[1][1].endswith('+02:00'))
        self.page();csv_rows=self.csv('/summaries/2/export.csv');self.assertEqual(csv_rows[0],SUMMARY_HEADERS)
        self.assertEqual(csv_rows[1][3],text);self.assertTrue(csv_rows[1][1].endswith('+02:00'))
        self.assertTrue(all(len(r)==13 for r in csv_rows))

    def test_formula_neutralization_export_only_including_whitespace(self):
        attacks=['=1+1','+123','-123','@SUM(A1)','\tplain','\nplain','\rplain','   =1+1',' \t@A1','\u00a0-1','\ufeff=2']
        for value in attacks:self.assertEqual(csv_cell(value),"'"+value)
        self.assertEqual(csv_cell('  Обычный текст'),'  Обычный текст')
        self.ticket(manager_id='')
        attack='  =HYPERLINK("bad")'
        self.execute('UPDATE tickets SET client=?,contact=?',(attack,'+mail@example.com'))
        self.execute('UPDATE tasks SET title=?,due_at=?',('@test','2026-11-12T10:00'))
        self.page();payload=self.snapshot();self.assertEqual(payload['rows'][0]['client'],attack)
        current=self.csv('/tickets/export.csv')[1];saved=self.csv('/summaries/1/export.csv')[1]
        self.assertEqual(current[2],"'"+attack);self.assertEqual(current[3],"'+mail@example.com")
        self.assertEqual(saved[3],"'"+attack);self.assertEqual(saved[9],"'@test")
        self.assertEqual(self.rows('SELECT client FROM tickets')[0]['client'],attack)

    def test_snapshot_html_escaped_and_exports_do_not_modify_data(self):
        attack='<script>alert(1)</script>'
        self.ticket(manager_id='');self.execute('UPDATE tickets SET client=?',(attack,));self.page()
        html=self.client.get('/summaries/1').get_data(as_text=True)
        self.assertNotIn(attack,html);self.assertIn('&lt;script&gt;',html)
        with closing(sqlite3.connect(self.path)) as db:before='\n'.join(db.iterdump())
        self.csv('/tickets/export.csv');self.csv('/summaries/1/export.csv')
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(before,'\n'.join(db.iterdump()))

    def test_migration_repeat_no_backfill_empty_and_rollback(self):
        self.ticket();self.assertEqual(self.summaries(),[])
        with closing(sqlite3.connect(self.path)) as db:
            before={t:db.execute(f'SELECT * FROM {t}').fetchall() for t in ('tickets','tasks','ticket_events','managers')}
            db.execute('DROP TABLE daily_summaries');db.execute('DELETE FROM schema_migrations WHERE version=?',(REPORT_MIGRATION,));db.commit()
            def authorizer(action,arg1,arg2,dbname,source):
                return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and arg1=='schema_migrations' else sqlite3.SQLITE_OK
            db.set_authorizer(authorizer)
            with self.assertRaises(sqlite3.DatabaseError):migrate_database(db)
            db.set_authorizer(None)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='daily_summaries'").fetchone())
            migrate_database(db);migrate_database(db)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_summaries').fetchone()[0],0)
            self.assertEqual(before,{t:db.execute(f'SELECT * FROM {t}').fetchall() for t in before})
        other=self.path.parent/'empty.sqlite3'
        create_app(dict(TESTING=True,DATABASE=str(other)))
        with closing(sqlite3.connect(other)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_summaries').fetchone()[0],0)
