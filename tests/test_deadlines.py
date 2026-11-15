import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import template_rendered

from app import APP_TIMEZONE, deadline_info, unassigned
from test_support import create_app, authenticated_client

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=APP_TIMEZONE)


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'tickets.sqlite3'
        self.app = create_app(dict(TESTING=True, DATABASE=str(self.path), SECRET_KEY='test'))
        self.client = authenticated_client(self.app)
        self.clock = patch('app.application_now', return_value=NOW)
        self.mock_clock = self.clock.start()
        self.addCleanup(self.clock.stop)

    def insert(self, manager='Анна', status='В работе', deadline='2026-11-12T11:59', client='Тест'):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                manager_id = None
                if not unassigned(manager):
                    row = db.execute('SELECT id FROM managers WHERE name = ?', (manager,)).fetchone()
                    manager_id = row[0] if row else db.execute('INSERT INTO managers(name) VALUES (?)', (manager,)).lastrowid
                cursor = db.execute('''INSERT INTO tickets
                    (created_at,client,description,manager,manager_id,status,priority,next_action,submission_key)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    ('2026-11-01T10:00:00', client, 'Тестовое описание', manager or '', manager_id, status,
                     'Средний', deadline, f'key-{manager}-{status}-{deadline}-{client}'))
                ticket_id = cursor.lastrowid
                if deadline:
                    db.execute("INSERT INTO tasks(ticket_id,title,manager_id,due_at,state,created_at,submission_key) VALUES (?,?,?,?,?,?,?)",
                               (ticket_id,'Следующее действие',manager_id,deadline,'Отменена' if status=='Закрыта' else 'Открыта',NOW.isoformat(),f'task-{ticket_id}'))
                return ticket_id

    def manager_value(self, name):
        with closing(sqlite3.connect(self.path)) as db:
            return str(db.execute('SELECT id FROM managers WHERE name = ?', (name,)).fetchone()[0])

    def get(self, path='/', query=None):
        contexts = []

        def capture(sender, template, context, **extra):
            contexts.append(context)

        template_rendered.connect(capture, self.app, weak=False)
        try:
            response = self.client.get(path, query_string=query)
        finally:
            template_rendered.disconnect(capture, self.app)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True), contexts[-1]

    def ids(self, query=None):
        return [t['id'] for t in self.get(query=query)[1]['tickets']]

    def seed(self):
        self.past = self.insert(client='Прошедший')
        self.future = self.insert(manager='Борис', deadline='2026-11-12T12:01', client='Будущий')
        self.equal = self.insert(deadline='2026-11-12T12:00', client='Равный')
        self.missing = self.insert(manager='', deadline='', client='Без срока')
        self.closed = self.insert(status='Закрыта', client='Закрытый')
        self.invalid = self.insert(deadline='<script>bad</script>', client='Неверный')
        self.spaces = self.insert(manager=' \t ', status='Новая', client='Без ответственного')

    def test_deadline_rules_and_exact_equality(self):
        for status, value, expected in [
            ('Новая', '2026-11-12T11:59', 'overdue'),
            ('В работе', '2026-11-12T12:01', 'on_time'),
            ('Ожидает клиента', '2026-11-12T12:00', 'on_time'),
            ('Новая', '', 'missing'),
            ('Закрыта', '2026-11-12T11:59', 'closed'),
            ('В работе', '2026-02-30T12:00', 'invalid'),
            ('Закрыта', '', 'closed')]:
            with self.subTest(status=status, value=value):
                info = deadline_info(dict(status=status, next_action=value), NOW)
                self.assertEqual(info['code'], expected)

    def test_timezone_and_naive_values(self):
        # 10:00 UTC is 12:00 Kaliningrad; naive 12:00 keeps its local meaning.
        for value in ('2026-11-12T12:00', '2026-11-12T10:00+00:00'):
            info = deadline_info(dict(status='Новая', next_action=value), NOW)
            self.assertEqual(info['code'], 'on_time')
        self.assertEqual(APP_TIMEZONE.utcoffset(NOW), timedelta(hours=2))
        self.assertEqual(NOW.astimezone(timezone.utc).hour, 10)

    def test_list_and_card_badges_and_invalid_text(self):
        self.seed()
        html, context = self.get()
        self.assertIn('Состояние срока', html)
        for label in ('Просрочена', 'В срок', 'Закрыта', 'Срок не назначен', 'Некорректный срок'):
            self.assertIn(label, html)
        self.assertIn('Некорректный срок', html)
        self.assertNotIn('<script>bad</script>', html)
        self.assertIn('В работе', html)
        for ticket in context['tickets']:
            html, card = self.get(f"/tickets/{ticket['id']}")
            self.assertEqual(card['deadline'], context['deadlines'][ticket['id']])
            self.assertIn(card['deadline']['label'], html)
            if ticket['id'] == self.invalid:
                self.assertIn('&lt;script&gt;bad&lt;/script&gt;', html)

    def test_clock_once_per_request(self):
        self.seed()
        self.get()
        self.mock_clock.assert_called_once_with()
        self.mock_clock.reset_mock()
        self.get(f'/tickets/{self.past}')
        self.mock_clock.assert_called_once_with()

    def test_manager_filter_and_unassigned_spaces(self):
        self.seed()
        self.assertEqual(self.ids({'manager': self.manager_value('Борис')}), [self.future])
        self.assertEqual(self.ids({'manager': 'unassigned'}), [self.spaces, self.missing])
        for value in (None, '', ' \t\n '):
            self.assertTrue(unassigned(value))

    def test_null_manager_from_legacy_database(self):
        ticket_id = self.insert(manager=None)
        self.assertEqual(self.ids({'manager': 'unassigned'}), [ticket_id])
        self.assertEqual(self.get()[1]['manager_options'], [])

    def test_status_filter(self):
        self.seed()
        self.assertEqual(self.ids({'status': 'Закрыта'}), [self.closed])
        self.assertEqual(self.ids({'status': 'Новая'}), [self.spaces])

    def test_overdue_filter_and_invalid_excluded(self):
        self.seed()
        self.assertEqual(self.ids({'overdue': 'overdue'}), [self.spaces, self.past])
        self.assertEqual(self.ids({'overdue': 'not_overdue'}),
                         [self.closed, self.missing, self.equal, self.future])
        closed_invalid = self.insert(status='Закрыта', deadline='bad', client='Закрыт с ошибкой')
        self.assertIn(closed_invalid, self.ids())
        self.assertIn(closed_invalid, self.ids({'overdue': 'not_overdue'}))

    def test_combined_filters_and_selected_values(self):
        self.seed()
        query = {'manager': self.manager_value('Анна'), 'status': 'В работе', 'overdue': 'overdue'}
        self.assertEqual(self.ids(query), [self.past])
        for _ in range(2):
            html, context = self.get(query=query)
            for field, value in query.items():
                self.assertEqual(context['selected_' + field], value)
                self.assertIn(f'value="{value}" selected', html)
            self.assertIn((self.manager_value('Борис'), 'Борис · №' + self.manager_value('Борис')), context['manager_options'])
            self.assertIn('method="get"', html)
            self.assertIn('Сбросить', html)

    def test_manager_names_remain_distinct_and_escaped(self):
        names = ['Анна', ' Анна ', 'анна', 'unassigned', '<script>manager</script>']
        for name in names:
            ticket_id = self.insert(manager=name)
            self.assertEqual(self.ids({'manager': self.manager_value(name)}), [ticket_id])
        html, context = self.get()
        self.assertEqual({label.rsplit(' · №', 1)[0] for _, label in context['manager_options']}, set(names))
        self.assertIn('&lt;script&gt;manager&lt;/script&gt;', html)
        self.assertNotIn('<script>manager</script>', html)

    def test_unknown_parameters_fall_back_to_all(self):
        self.seed()
        html, context = self.get(query={'manager': "' OR 1=1--", 'status': 'bad',
                                        'overdue': 'bad', 'unknown': 'anything'})
        self.assertEqual([t['id'] for t in context['tickets']], self.ids())
        self.assertFalse(context['has_filters'])
        self.assertEqual(context['selected_manager'], '')
        self.assertEqual(context['selected_status'], '')
        self.assertEqual(context['selected_overdue'], '')

    def test_empty_result_and_empty_database(self):
        html, _ = self.get()
        self.assertIn('Здесь появятся ваши заявки', html)
        html, _ = self.get(query={'status': 'Новая'})
        self.assertIn('По выбранным условиям заявки не найдены', html)
        self.seed()
        html, context = self.get(query={'status': 'Закрыта', 'overdue': 'overdue'})
        self.assertEqual(context['tickets'], [])
        self.assertIn('По выбранным условиям заявки не найдены', html)

    def test_gets_preserve_schema_records_and_order(self):
        self.seed()

        def snapshot():
            with closing(sqlite3.connect(self.path)) as db:
                return (list(db.execute('SELECT * FROM tickets ORDER BY id')),
                        list(db.execute('SELECT * FROM sqlite_master ORDER BY name')))

        before = snapshot()
        self.assertEqual(self.ids(), sorted([row[0] for row in before[0]], reverse=True))
        self.ids({'overdue': 'overdue'})
        self.ids({'manager': 'unassigned', 'status': 'Новая'})
        self.get(f'/tickets/{self.invalid}')
        self.assertEqual(snapshot(), before)


if __name__ == '__main__':
    unittest.main()
