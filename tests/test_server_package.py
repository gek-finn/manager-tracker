from contextlib import closing
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import re
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from app import create_app as actual_app,check_schema
from config_features import ConfigurationError
from server_runtime import create_server_app
from server_tools import setup,migrate,settings
from server_demo import create_demo,LABEL
from sqlite_maintenance import managed_backup,restore_copy,FileLock,lock_path,MaintenanceError,digest,check_database
from build_package import build_archive,FILES
from test_support import create_app,TEST_LOGIN,TEST_PASSWORD,TEST_HASH

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime(2026,11,13,20,0,tzinfo=timezone(timedelta(hours=2)))

class ServerPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tests');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=self.root/'database.sqlite3'
        create_app(dict(TESTING=True,DATABASE=str(self.db)))
        self.value=dict(APP_MODE='production',SECRET_KEY='temporary-server-key-'*4,OWNER_LOGIN=TEST_LOGIN,
            OWNER_PASSWORD_HASH=TEST_HASH,DATABASE=str(self.db),TRUSTED_HOSTS=['manager.gilinsky.ru'],TRUST_LOCAL_PROXY=False)
        self.config=self.root/'server-owner.json';self.config.write_text(json.dumps(self.value),encoding='utf-8')

    def dump(self):
        with closing(sqlite3.connect(self.db)) as db:return '\n'.join(db.iterdump())

    def test_production_worker_only_reads_schema_and_never_initializes(self):
        before=digest(self.db)
        with patch('app.migrate_database',side_effect=AssertionError('worker migration forbidden')):
            app=actual_app({'SERVER_CONFIG_PATH':str(self.config)})
            self.assertEqual(app.test_client().get('/health',base_url='https://manager.gilinsky.ru').status_code,200)
        self.assertEqual(before,digest(self.db))
        with closing(sqlite3.connect(self.db)) as db:
            with db:db.execute("DELETE FROM schema_migrations WHERE version='stage_6a_login_limits_v1'")
        before=digest(self.db)
        with self.assertRaises(ConfigurationError) as error:actual_app({'SERVER_CONFIG_PATH':str(self.config)})
        self.assertIn('server_tools.py migrate',str(error.exception));self.assertEqual(before,digest(self.db))
        self.value['DATABASE']=str(self.root/'missing.sqlite3');self.config.write_text(json.dumps(self.value))
        with self.assertRaises(ConfigurationError):actual_app({'SERVER_CONFIG_PATH':str(self.config)})
        self.assertFalse((self.root/'missing.sqlite3').exists())

    def test_explicit_migrate_backup_repeat_and_running_worker_lock(self):
        before=self.dump();backups=self.root/'backups'
        app=create_server_app({'SERVER_CONFIG_PATH':str(self.config)})
        try:
            with self.assertRaises(MaintenanceError):migrate(self.config,backups)
        finally:app.extensions['maintenance_lock'].__exit__(None,None,None)
        migrate(self.config,backups);migrate(self.config,backups)
        self.assertEqual(before,self.dump())
        self.assertEqual(len(list(backups.glob('manager-tracker-before-migrate-*.sqlite3'))),2)
        self.value['DATABASE']=str(self.root/'new.sqlite3');self.config.write_text(json.dumps(self.value))
        migrate(self.config,self.root/'new-backups')
        with closing(sqlite3.connect(self.value['DATABASE'])) as db:check_schema(db)

    def login(self,app,ip,forwarded=None,password='wrong'):
        client=app.test_client();headers={'X-Forwarded-For':forwarded,'X-Forwarded-Proto':'https','X-Forwarded-Host':'evil.example'} if forwarded else {}
        response=client.get('/login',base_url='https://manager.gilinsky.ru',environ_overrides={'REMOTE_ADDR':ip},headers=headers)
        token=re.search('name="csrf_token" value="([^"]+)"',response.get_data(as_text=True))[1]
        return client.post('/login',base_url='https://manager.gilinsky.ru',environ_overrides={'REMOTE_ADDR':ip},headers=headers,
            data=dict(csrf_token=token,login=TEST_LOGIN,password=password,next='/'))

    def test_proxy_disabled_ignores_headers_and_enabled_trusts_one_local_peer(self):
        app=actual_app(self.value)
        self.assertEqual(self.login(app,'127.0.0.1','203.0.113.7').status_code,401)
        with closing(sqlite3.connect(self.db)) as db:self.assertEqual(db.execute('SELECT client_ip FROM auth_attempts').fetchone()[0],'127.0.0.1')
        app=actual_app({**self.value,'TRUST_LOCAL_PROXY':True})
        self.assertEqual(self.login(app,'127.0.0.1','203.0.113.8').status_code,401)
        self.assertEqual(self.login(app,'198.51.100.9','203.0.113.99').status_code,401)
        with closing(sqlite3.connect(self.db)) as db:
            ips={r[0] for r in db.execute('SELECT client_ip FROM auth_attempts')}
        self.assertEqual(ips,{'127.0.0.1','203.0.113.8','198.51.100.9'})
        # Scheme is trusted, Host is not replaced from X-Forwarded-Host.
        with app.test_client() as client:
            client.get('/health',base_url='http://manager.gilinsky.ru',headers={'X-Forwarded-Proto':'https'},environ_overrides={'REMOTE_ADDR':'127.0.0.1'})
        with self.assertRaises(ConfigurationError):actual_app({**self.value,'TRUST_LOCAL_PROXY':'yes'})

    def test_proxy_real_ip_is_used_for_persistent_login_limit(self):
        app=actual_app({**self.value,'TRUST_LOCAL_PROXY':True})
        for _ in range(5):self.assertEqual(self.login(app,'127.0.0.1','203.0.113.10').status_code,401)
        self.assertEqual(self.login(app,'127.0.0.1','203.0.113.10',TEST_PASSWORD).status_code,429)
        self.assertEqual(self.login(app,'127.0.0.1','203.0.113.11',TEST_PASSWORD).status_code,303)
        with closing(sqlite3.connect(self.db)) as db:self.assertEqual(db.execute('SELECT client_ip FROM auth_blocks').fetchone()[0],'203.0.113.10')

    def test_proxy_scheme_boundary_without_exposing_test_route(self):
        from config_features import LocalProxy
        observed=[]
        def capture(environ,start_response):
            observed.append((environ.get('REMOTE_ADDR'),environ.get('wsgi.url_scheme'),environ.get('HTTP_HOST')))
            start_response('200 OK',[]);return [b'ok']
        proxy=LocalProxy(capture)
        for peer in ('127.0.0.1','198.51.100.2'):
            proxy(dict(REMOTE_ADDR=peer,HTTP_X_FORWARDED_FOR='spoof, 203.0.113.2',HTTP_X_FORWARDED_PROTO='https',HTTP_X_FORWARDED_HOST='evil',HTTP_HOST='manager.gilinsky.ru',**{'wsgi.url_scheme':'http'}),lambda *args:None)
        self.assertEqual(observed,[('203.0.113.2','https','manager.gilinsky.ru'),('198.51.100.2','http','manager.gilinsky.ru')])

    def test_new_demo_is_fresh_synthetic_complete_and_no_credentials(self):
        output=self.root/'demo.sqlite3';original=self.dump()
        create_demo(output,NOW);self.assertEqual(original,self.dump());check_database(output)
        with closing(sqlite3.connect(output)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0],25)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM managers WHERE active=1').fetchone()[0],4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM managers WHERE active=0').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(DISTINCT status) FROM tickets').fetchone()[0],5)
            self.assertEqual(db.execute('SELECT COUNT(DISTINCT priority) FROM tickets').fetchone()[0],3)
            self.assertTrue(all(r[0].startswith('Демо') and r[1].endswith('@example.com') and r[2].startswith(LABEL) for r in db.execute('SELECT client,contact,description FROM tickets')))
            self.assertTrue(all(LABEL in json.loads(r[0])['text'] for r in db.execute('SELECT details FROM ticket_events')))
            self.assertGreater(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)
            for t in ('daily_summaries','auth_attempts','auth_blocks'):self.assertEqual(db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0],0)
        from event_features import control_info
        from automation_features import get_control_settings
        from app import TICKET_SELECT,parse_deadline
        with closing(sqlite3.connect(output)) as db:
            db.row_factory=sqlite3.Row;settings_value=get_control_settings(db)
            controls=[control_info(t,db.execute('SELECT * FROM tasks WHERE ticket_id=?',(t['id'],)).fetchall(),db.execute('SELECT * FROM ticket_events WHERE ticket_id=?',(t['id'],)).fetchall(),NOW,parse_deadline,settings_value) for t in db.execute(TICKET_SELECT).fetchall()]
        self.assertTrue(any(c['problems'] for c in controls));self.assertTrue(any(c['data'] and not c['problems'] for c in controls))
        manifest=json.loads(output.with_suffix('.manifest.json').read_text());self.assertEqual(manifest['sha256'],digest(output))
        before=digest(output)
        with self.assertRaises(MaintenanceError):create_demo(output,NOW)
        self.assertEqual(before,digest(output))

    def test_backup_retains_seven_own_days_preserves_other_and_changed_files(self):
        directory=self.root/'copies'
        first=managed_backup(self.db,directory,now=NOW)
        unrelated=directory/'not-our-file.txt';unrelated.write_text('keep')
        rogue=directory/'manager-tracker-daily-2000-01-01.sqlite3';rogue.write_text('untracked')
        # A modified old backup no longer matches its recorded digest and is not deleted.
        first.write_bytes(b'modified by user')
        for day in range(1,10):managed_backup(self.db,directory,now=NOW+timedelta(days=day))
        self.assertEqual(first.read_bytes(),b'modified by user');self.assertEqual(unrelated.read_text(),'keep');self.assertEqual(rogue.read_text(),'untracked')
        catalog=json.loads((directory/'.manager-tracker-backups.json').read_text())
        valid=[n for n,h in catalog['files'].items() if (directory/n).exists() and digest(directory/n)==h]
        self.assertEqual(len(valid),7)
        for name in valid:check_database(directory/name)
        foreign=self.root/'foreign';foreign.mkdir();(foreign/'keep').write_text('x')
        with self.assertRaises(MaintenanceError):managed_backup(self.db,foreign)
        self.assertEqual((foreign/'keep').read_text(),'x')
        with self.assertRaises(MaintenanceError):managed_backup(self.db,self.root)

    def test_backup_same_day_and_restore_separate_or_explicit_existing(self):
        directory=self.root/'backups';backup=managed_backup(self.db,directory,now=NOW)
        managed_backup(self.db,directory,now=NOW+timedelta(minutes=5))
        self.assertEqual(len(list(directory.glob('manager-tracker-daily-*.sqlite3'))),1)
        restored=self.root/'trial'/'restore.sqlite3';restore_copy(backup,restored)
        self.assertEqual(self.dump(),self.dump_file(restored))
        with self.assertRaises(MaintenanceError):restore_copy(backup,restored)
        # Real replacement first saves the target's current state.
        with closing(sqlite3.connect(restored)) as db:
            with db:db.execute("INSERT INTO managers(name) VALUES ('Synthetic changed manager')")
        restore_copy(backup,restored,self.root/'restore-backups',replace=True)
        self.assertEqual(self.dump(),self.dump_file(restored))
        self.assertEqual(len(list((self.root/'restore-backups').glob('manager-tracker-before-restore-*.sqlite3'))),1)

    def dump_file(self,path):
        with closing(sqlite3.connect(path)) as db:return '\n'.join(db.iterdump())

    def test_server_json_setup_special_password_no_shell_or_plaintext(self):
        config=self.root/'private'/'server-owner.json';password='Special-$-quote-\"-пароль-2026'
        entries=iter([password,password])
        setup(config,self.db,True,input_fn=lambda _:TEST_LOGIN,password_fn=lambda _:next(entries))
        text=config.read_text(encoding='utf-8');self.assertNotIn(password,text)
        loaded=settings(config);self.assertTrue(loaded['TRUST_LOCAL_PROXY']);self.assertIn('$',loaded['OWNER_PASSWORD_HASH'])
        from werkzeug.security import check_password_hash
        self.assertTrue(check_password_hash(loaded['OWNER_PASSWORD_HASH'],password))
        with self.assertRaises(ConfigurationError):setup(config,self.db,True)
        self.assertEqual(config.read_text(encoding='utf-8'),text)
        other=actual_app({'SERVER_CONFIG_PATH':str(config)})
        self.assertTrue(other.config['SESSION_COOKIE_SECURE'])

    def test_archive_allowlist_and_extracted_wsgi_smoke(self):
        archive=self.root/'server.zip';build_archive(archive)
        with zipfile.ZipFile(archive) as package:
            names=set(package.namelist());self.assertEqual(names,set(FILES)|{'PACKAGE-MANIFEST.json'})
            self.assertTrue(package.read('deploy/certbot-deploy-hook.sh').startswith(b'#!/bin/sh\n'))
            self.assertNotIn(b'\r',package.read('deploy/certbot-deploy-hook.sh'))
            for forbidden in ('instance/','backups/','.venv/','tests/','.git/','.env','local-owner.json','server-owner.json'):
                self.assertFalse(any(n==forbidden or n.startswith(forbidden) for n in names))
            self.assertFalse(any(n.endswith(('.sqlite3','.csv','.png')) for n in names))
            manifest=json.loads(package.read('PACKAGE-MANIFEST.json'))
            import hashlib
            for item in manifest['files']:self.assertEqual(hashlib.sha256(package.read(item['path'])).hexdigest(),item['sha256'])
            extracted=self.root/'unpacked';package.extractall(extracted)
        env={k:v for k,v in os.environ.items() if not k.startswith('TRACKER_') and k!='PYTHONPATH'}
        env['TRACKER_SERVER_CONFIG']=str(self.config)
        result=subprocess.run([sys.executable,'-B','-c',"import wsgi; a=wsgi.application; assert a.config['APP_MODE']=='production'; assert a.test_client().get('/health',base_url='https://manager.gilinsky.ru').status_code==200; assert a.test_client().get('/dashboard',base_url='https://manager.gilinsky.ru').status_code==303; print('WSGI package OK')"],cwd=extracted,env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('WSGI package OK',result.stdout)
        with self.assertRaises(FileExistsError):build_archive(archive)

    def test_deployment_samples_loopback_one_worker_two_tls_stages(self):
        config=runpy.run_path(str(ROOT/'deploy/gunicorn.conf.py'))
        self.assertEqual(config['workers'],1);self.assertEqual(config['bind'],'127.0.0.1:8001')
        self.assertEqual(config['forwarded_allow_ips'],'');self.assertEqual(config['secure_scheme_headers'],{})
        http=(ROOT/'deploy/nginx-http.conf').read_text();https=(ROOT/'deploy/nginx-https.conf').read_text()
        self.assertNotIn('ssl_certificate',http);self.assertIn('ssl_certificate',https)
        self.assertNotIn('includeSubDomains',https);self.assertIn('proxy_set_header X-Forwarded-For $remote_addr;',https)
        self.assertNotIn('$proxy_add_x_forwarded_for',https);self.assertIn('server_name manager.gilinsky.ru',https)
        unit=(ROOT/'deploy/manager-tracker.service').read_text();self.assertIn('User=manager-tracker',unit)
        self.assertNotIn('EnvironmentFile=',unit);self.assertIn('TRACKER_SERVER_CONFIG=',unit);self.assertIn('ProtectSystem=strict',unit)

    def test_wsgi_fails_closed_without_production_config(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ConfigurationError) as error:create_server_app()
        self.assertIn('TRACKER_DATABASE',str(error.exception))
        self.assertNotIn('setup_owner.py',str(error.exception))
