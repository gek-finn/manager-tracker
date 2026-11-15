from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import APP_TIMEZONE
from test_support import create_app, authenticated_client
import seed_demo


class DemoSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder/'test.sqlite3'
        create_app(dict(TESTING=True,DATABASE=str(self.path)))
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("INSERT INTO managers(name) VALUES ('Прежний тестовый менеджер')")
                db.execute("INSERT INTO tickets(created_at,client,description,manager_id,status,priority,submission_key) VALUES ('2026-11-01T10:00','Прежняя тестовая заявка','Тест',1,'Новая','Средний','old')")
        self.now = datetime(2026,11,12,15,30,tzinfo=APP_TIMEZONE)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {table:list(db.execute(f'SELECT * FROM {table} ORDER BY 1'))
                    for table in ('managers','tickets','schema_migrations','sqlite_master','sqlite_sequence','tasks','ticket_events')}

    def seed(self):
        return seed_demo.seed_database(self.path,self.folder/'backups',self.now)

    def test_exact_counts_coverage_backup_and_originals(self):
        before = self.snapshot()
        result = self.seed()
        after = self.snapshot()
        self.assertFalse(result['already_seeded'])
        self.assertEqual(len(after['managers'])-len(before['managers']),5)
        self.assertEqual(len(after['tickets'])-len(before['tickets']),25)
        self.assertEqual(after['managers'][:1],before['managers'])
        self.assertEqual(after['tickets'][:1],before['tickets'])
        self.assertEqual(after['sqlite_master'],before['sqlite_master'])
        self.assertEqual(after['schema_migrations'],before['schema_migrations'])
        self.assertEqual(result['summary']['statuses'],{'Новая':8,'В работе':7,'Ожидает клиента':5,'Закрыта':5})
        self.assertEqual(result['summary']['deadlines'],{'Просрочена':7,'В срок':8,'Срок не назначен':5,'Закрыта':5})
        self.assertEqual(result['summary']['priorities'],{'Низкий':9,'Средний':8,'Высокий':8})
        self.assertEqual(result['summary']['unassigned'],4)
        with closing(sqlite3.connect(result['backup'])) as db:
            self.assertEqual(list(db.execute('SELECT * FROM tickets')),before['tickets'])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT SUM(active) FROM managers WHERE id IN ('+','.join('?'*5)+')',result['manager_ids']).fetchone()[0],4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tickets WHERE manager_id=?',(result['manager_ids'][-1],)).fetchone()[0],4)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            for client,contact in db.execute('SELECT client,contact FROM tickets WHERE id>1'):
                self.assertTrue(client.startswith('Демо'))
                self.assertTrue(contact.endswith('@example.com'))

    def test_repeat_preserves_user_edits_and_deleted_records(self):
        result=self.seed()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                db.execute('UPDATE managers SET name=?,active=0 WHERE id=?',('Изменено пользователем',result['manager_ids'][0]))
                db.execute('UPDATE tickets SET client=?,manager_id=NULL WHERE id=?',('Изменено',result['ticket_ids'][0]))
                db.execute('DELETE FROM tickets WHERE id=?',(result['ticket_ids'][1],))
        before=self.snapshot()
        receipt=Path(result['receipt']).read_bytes()
        again=self.seed()
        self.assertTrue(again['already_seeded'])
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(Path(result['receipt']).read_bytes(),receipt)
        self.assertEqual(again['missing_ticket_ids'],[result['ticket_ids'][1]])

    def test_filesystem_failure_rolls_back_all_inserts(self):
        before=self.snapshot()
        with patch('seed_demo._write_manifest',side_effect=OSError('test disk failure')):
            with self.assertRaises(OSError):
                self.seed()
        self.assertEqual(self.snapshot(),before)
        self.assertFalse(self.path.with_suffix('.demo-manifest.json').exists())

    def test_duplicate_name_collision_rolls_back_without_reusing_manager(self):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute('INSERT INTO managers(name) VALUES (?)',(seed_demo.NAMES[0].upper(),))
        before=self.snapshot()
        with self.assertRaises(RuntimeError):
            self.seed()
        self.assertEqual(self.snapshot(),before)

    def test_receipt_recovery_by_keys_and_partial_batch_refusal(self):
        result=self.seed()
        receipt=Path(result['receipt'])
        receipt.unlink()
        before=self.snapshot()
        restored=self.seed()
        self.assertTrue(restored['already_seeded'])
        self.assertEqual(restored['ticket_ids'],result['ticket_ids'])
        self.assertEqual(restored['manager_ids'],result['manager_ids'])
        self.assertEqual(self.snapshot(),before)
        receipt.unlink()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                db.execute('DELETE FROM tickets WHERE id=?',(result['ticket_ids'][0],))
        before=self.snapshot()
        with self.assertRaises(RuntimeError):
            self.seed()
        self.assertEqual(self.snapshot(),before)

    def test_default_cli_uses_copy_and_explicit_path_required(self):
        project=self.folder/'project'
        (project/'instance').mkdir(parents=True)
        working=project/'instance/tracker.sqlite3'
        seed_demo.backup_database(self.path,working)
        original=working.read_bytes()
        # Keep real process locking outside this isolated path-selection test.
        from contextlib import nullcontext
        with patch('seed_demo.ROOT',project),patch('seed_demo.stopped_project',return_value=nullcontext()):
            with patch('seed_demo.seed_database', wraps=seed_demo.seed_database) as seeder:
                self.assertEqual(seed_demo.main([]),0)
                self.assertEqual(seeder.call_args[0][0],project/'instance/demo_tracker.sqlite3')
        self.assertEqual(working.read_bytes(),original)

    def test_new_demo_events_are_current_and_repeat_adds_no_history(self):
        result=self.seed()
        with closing(sqlite3.connect(self.path)) as db:
            records=list(db.execute('SELECT event_type,meaningful,occurred_at FROM ticket_events ORDER BY id'))
            self.assertEqual(sum(r[0]=='ticket.created' and r[1]==1 for r in records),25)
            self.assertEqual(sum(r[0]=='task.created' and r[1]==0 for r in records),20)
            self.assertTrue(all(r[2]==self.now.isoformat(timespec='seconds') for r in records))
            self.assertFalse(any(r[0]=='journal.enabled' for r in records))
        before=self.snapshot()
        self.assertTrue(self.seed()['already_seeded'])
        self.assertEqual(self.snapshot(),before)


if __name__=='__main__':
    unittest.main()
