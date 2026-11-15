from contextlib import closing
from datetime import timedelta
import re
import sqlite3
import unittest
from unittest.mock import patch
from flask import template_rendered
import test_automation as automation

NOW=automation.NOW

class DashboardTests(unittest.TestCase):
    setUp=automation.AutomationTests.setUp
    rows=automation.AutomationTests.rows
    post=automation.AutomationTests.post
    key=automation.AutomationTests.key
    payload=automation.AutomationTests.payload
    ticket=automation.AutomationTests.ticket
    ordinary=automation.AutomationTests.ordinary
    primary=automation.AutomationTests.primary
    proposal_data=automation.AutomationTests.proposal_data
    send=automation.AutomationTests.send
    edit=automation.AutomationTests.edit
    snapshot=automation.AutomationTests.snapshot

    def execute(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with db: db.execute(sql,args)

    def page(self,url='/dashboard'):
        captured=[]
        def receiver(sender,template,context,**extra): captured.append(context)
        template_rendered.connect(receiver,self.app,weak=False)
        try: response=self.client.get(url)
        finally: template_rendered.disconnect(receiver,self.app)
        self.assertEqual(response.status_code,200)
        return captured[-1],response.get_data(as_text=True)

    def fixtures(self):
        self.ticket(manager_id='')  # two overdue tasks, several reasons, counted once
        self.ticket(status='В работе')
        self.ticket(status='В работе')
        self.execute('UPDATE tickets SET manager_id=3 WHERE id=3')
        self.ticket(status='В работе')  # no remarks
        self.ticket(status='Закрыта',manager_id='')
        self.ticket(status='В работе')  # only data warnings, highest id
        self.execute("UPDATE tasks SET due_at='2026-12-15T10:00'")
        self.execute('UPDATE tickets SET created_at=? WHERE id=1',((NOW-timedelta(hours=30)).isoformat(),))
        self.execute('UPDATE ticket_events SET occurred_at=? WHERE ticket_id=1',((NOW-timedelta(hours=30)).isoformat(),))
        self.execute('UPDATE tasks SET due_at=? WHERE ticket_id=1',((NOW-timedelta(days=1)).isoformat(),))
        self.ordinary(ticket=1,due=(NOW-timedelta(hours=1)).replace(tzinfo=None).isoformat())
        self.execute("UPDATE tasks SET due_at='' WHERE ticket_id=2")
        self.ordinary(ticket=2,due='2026-12-15T10:00')
        self.execute("UPDATE tasks SET due_at='broken' WHERE id=(SELECT MAX(id) FROM tasks WHERE ticket_id=2)")
        self.ordinary(ticket=2,due='2026-12-16T10:00')
        self.execute('DELETE FROM tasks WHERE ticket_id=3')
        self.execute('DELETE FROM ticket_events WHERE ticket_id=3')
        self.execute("UPDATE ticket_events SET occurred_at='broken',details='null' WHERE ticket_id=6")
        self.execute("UPDATE tickets SET created_at='broken' WHERE id IN (5,6)")

    def test_every_metric_counts_unique_overlap_and_closed_exclusion(self):
        self.fixtures();ctx,html=self.page()
        expected=dict(active=5,new=1,problems=3,unassigned=1,new_stale=1,no_next_task=1,
                      missing_due=1,overdue_task=1,inactive=1,data=3)
        self.assertEqual({m['code']:m['count'] for m in ctx['metrics']},expected)
        self.assertIn('UTC+02:00',html)
        self.assertIn('могут пересекаться',html)
        self.assertIn('Неактивен',html)
        self.assertNotIn('Редактировать',html);self.assertNotIn('Удалить',html)

    def test_table_order_unique_all_reasons_and_trusted_activity(self):
        self.fixtures();ctx,html=self.page()
        self.assertEqual([t['id'] for t in ctx['tickets']],[1,3,2,6])
        self.assertEqual(re.findall(r'data-ticket-id="(\d+)"',html),['1','3','2','6'])
        self.assertEqual(set(ctx['controls'][1]['codes']),{'unassigned','new_stale','overdue_task','inactive'})
        self.assertEqual(ctx['activities'][1]['label'],'Заявка создана')
        self.assertIsNone(ctx['activities'][3]);self.assertIsNone(ctx['activities'][6])
        self.assertIn('Нет данных об активности',html);self.assertIn('Некорректные данные события',html)
        self.assertEqual(ctx['schedules'][2]['nearest']['due_at'],'2026-12-16T10:00')

    def test_all_metric_links_match_exact_list_selection_no_inherited_filters(self):
        self.fixtures();ctx,_=self.page('/dashboard?manager=unassigned&scope=closed&status=Закрыта&control=clear')
        for metric in ctx['metrics']:
            with self.subTest(metric=metric['code']):
                selected,_=self.page(metric['url'])
                self.assertEqual(len(selected['tickets']),metric['count'])
                self.assertEqual(selected['selected_manager'],'')
                self.assertEqual(selected['selected_overdue'],'')
                if metric['code']=='active':self.assertEqual({t['id'] for t in selected['tickets']},{1,2,3,4,6})
                if metric['code']=='data':self.assertEqual({t['id'] for t in selected['tickets']},{2,3,6})

    def test_scope_filter_get_default_unknown_reset_and_intersections(self):
        self.fixtures()
        for value,expected in (('',{1,2,3,4,5,6}),('active',{1,2,3,4,6}),('closed',{5}),('bad',{1,2,3,4,5,6})):
            ctx,html=self.page('/?scope='+value)
            self.assertEqual({t['id'] for t in ctx['tickets']},expected)
            actual=value if value in ('active','closed') else ''
            self.assertIn(f'value="{actual}" selected',html)
        ctx,html=self.page('/?scope=active&manager=unassigned&status=Новая&overdue=overdue&control=new_stale')
        self.assertEqual([t['id'] for t in ctx['tickets']],[1]);self.assertIn('href="/">Сбросить',html)
        self.assertEqual(self.page('/?scope=closed&control=problems')[0]['tickets'],[])
        self.assertEqual(self.page('/?scope=active&control=clear')[0]['tickets'][0]['id'],4)
        self.assertIn('По выбранным условиям заявки не найдены',self.page('/?scope=closed&status=Новая')[1])

    def test_strict_thresholds_and_clock_once_per_dashboard_request(self):
        self.ticket()
        self.time=NOW+timedelta(hours=2)
        self.assertEqual(self.page()[0]['metrics'][4]['count'],0)
        self.time+=timedelta(microseconds=1)
        self.assertEqual(self.page()[0]['metrics'][4]['count'],1)
        self.time=NOW+timedelta(hours=24)
        self.assertEqual(self.page()[0]['metrics'][8]['count'],0)
        self.time+=timedelta(microseconds=1)
        with patch('app.application_now',return_value=self.time) as clock:
            ctx,_=self.page();self.assertEqual(clock.call_count,1)
        self.assertEqual(ctx['metrics'][8]['count'],1)
        self.assertEqual(ctx['calculated_at'],self.time)

    def test_empty_closed_and_unremarkable_states(self):
        ctx,html=self.page();self.assertTrue(all(m['count']==0 for m in ctx['metrics']))
        self.assertIn('Заявок, требующих внимания, нет',html)
        self.ticket(status='В работе');ctx,html=self.page()
        self.assertEqual(ctx['tickets'],[])
        self.assertEqual({m['code']:m['count'] for m in ctx['metrics']}['active'],1)
        self.ticket(status='Закрыта',manager_id='');self.assertEqual(self.page()[0]['tickets'],[])

    def test_user_text_escaped_and_no_data_or_schema_changes(self):
        attack='<script>alert("x")</script>'
        self.ticket(client=attack,manager_id='')
        self.execute('UPDATE managers SET name=? WHERE id=1',(attack,))
        self.execute('UPDATE tasks SET title=?,manager_id=1 WHERE ticket_id=1',(attack,))
        self.execute('UPDATE tickets SET manager_id=1 WHERE id=1')
        self.execute("UPDATE tasks SET due_at='broken'")
        self.page()  # The first successful GET intentionally saves the daily summary.
        with closing(sqlite3.connect(self.path)) as db:
            before='\n'.join(db.iterdump())
        for _ in range(2):
            _,html=self.page();self.assertNotIn(attack,html);self.assertIn('&lt;script&gt;',html)
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual('\n'.join(db.iterdump()),before)

    def test_batch_queries_do_not_grow_with_rows(self):
        for _ in range(10):self.ticket(manager_id='')
        queries=[];original=sqlite3.connect
        def connect(*args,**kwargs):
            db=original(*args,**kwargs);db.set_trace_callback(queries.append);return db
        with patch('app.sqlite3.connect',side_effect=connect):self.page()
        self.assertLessEqual(sum(q.lstrip().upper().startswith('SELECT') for q in queries),6)
