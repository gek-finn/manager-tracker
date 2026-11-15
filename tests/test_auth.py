from contextlib import closing
from datetime import timedelta
import concurrent.futures
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from werkzeug.security import check_password_hash
from app import create_app,migrate_database
from auth_features import AUTH_MIGRATION,safe_next,reserve_attempt,finish_attempt
from config_features import ConfigurationError,load_settings
from setup_owner import configure_owner,main as setup_main
from test_support import TEST_LOGIN,TEST_PASSWORD,TEST_HASH


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent);self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.path=self.folder/'test.sqlite3';self.instant=1800000000.0
        clock=patch('auth_features.auth_now',side_effect=lambda:self.instant);clock.start();self.addCleanup(clock.stop)
        self.config=dict(APP_MODE='local',TESTING=True,SECRET_KEY='test-configuration-key-'*3,OWNER_LOGIN=TEST_LOGIN,
                         OWNER_PASSWORD_HASH=TEST_HASH,DATABASE=str(self.path),LOCAL_CONFIG_PATH=self.folder/'local-owner.json')
        self.app=create_app(self.config);self.client=self.app.test_client()

    def token(self,client=None):
        client=client or self.client
        html=client.get('/login').get_data(as_text=True)
        return re.search('name="csrf_token" value="([^"]+)"',html)[1]

    def login(self,client=None,**overrides):
        client=client or self.client
        data=dict(csrf_token=self.token(client),login=TEST_LOGIN,password=TEST_PASSWORD,next='/')
        data.update(overrides);return client.post('/login',data=data)

    def rows(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:return db.execute(sql,args).fetchall()

    def business_dump(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {t:db.execute(f'SELECT * FROM {t}').fetchall() for t in ('tickets','tasks','managers','ticket_events','daily_summaries','control_settings','automation_settings')}

    def production(self):
        return dict(APP_MODE='production',SECRET_KEY=self.config['SECRET_KEY'],OWNER_LOGIN=TEST_LOGIN,
                    OWNER_PASSWORD_HASH=TEST_HASH,DATABASE=str(self.path),TRUSTED_HOSTS='manager.gilinsky.ru')

    def test_configuration_required_production_validation_and_no_secret_errors(self):
        cfg=self.production();self.assertEqual(create_app(cfg).config['APP_MODE'],'production')
        for name in ('SECRET_KEY','OWNER_LOGIN','OWNER_PASSWORD_HASH','DATABASE','TRUSTED_HOSTS'):
            values=cfg.copy();values.pop(name)
            with patch.dict(os.environ,{},clear=True):
                with self.assertRaises(ConfigurationError) as error:create_app(values)
            self.assertNotIn(TEST_HASH,str(error.exception));self.assertNotIn(self.config['SECRET_KEY'],str(error.exception))
        for hosts in ('','*','.startcomplect.ru','https://manager.gilinsky.ru','host:443','a..b','a,','-bad.host'):
            with self.assertRaises(ConfigurationError):create_app({**cfg,'TRUSTED_HOSTS':hosts})
        for name,value in (('DATABASE','relative.sqlite3'),('DATABASE',str(self.folder)),('DATABASE',':memory:'),
                           ('DATABASE',str(self.folder/'missing'/'db.sqlite3')),('DEBUG',True),('TESTING',True),
                           ('SECRET_KEY','short'),('OWNER_PASSWORD_HASH','plain-password')):
            with self.assertRaises(ConfigurationError):create_app({**cfg,name:value})
        bad=self.folder/'bad.sqlite3';bad.write_text('not a database')
        with self.assertRaises(ConfigurationError):create_app({**cfg,'DATABASE':str(bad)})

    def test_environment_configuration_and_local_setup_instruction(self):
        env=dict(TRACKER_MODE='production',TRACKER_SECRET_KEY=self.config['SECRET_KEY'],TRACKER_OWNER_LOGIN=TEST_LOGIN,
                 TRACKER_OWNER_PASSWORD_HASH=TEST_HASH,TRACKER_DATABASE=str(self.path),TRACKER_ALLOWED_HOSTS='manager.gilinsky.ru')
        values=load_settings(self.folder,environ=env)
        self.assertEqual(values['DATABASE'],str(self.path));self.assertTrue(values['SESSION_COOKIE_SECURE'])
        with self.assertRaises(ConfigurationError) as error:load_settings(self.folder,environ={})
        self.assertIn('python setup_owner.py',str(error.exception))
        self.assertFalse((self.folder/'instance').exists())
        with self.assertRaises(ConfigurationError):load_settings(self.folder,environ={'TRACKER_MODE':'unknown'})
        with self.assertRaises(ConfigurationError):create_app({**self.config,'DATABASE':str(Path.cwd()/'static/unsafe.sqlite3')})

    def test_login_real_password_generic_error_and_session_rotation(self):
        initial=self.token()
        with self.client.session_transaction() as session:session['old_value']='must disappear'
        self.assertEqual(self.login().status_code,303)
        with self.client.session_transaction() as session:
            self.assertTrue(session['owner_authenticated']);self.assertNotIn('old_value',session)
            self.assertNotEqual(session['csrf_token'],initial)
            self.assertEqual(session['owner_issued_at'],self.instant)
            self.assertNotIn('password',session);self.assertNotIn('OWNER_PASSWORD_HASH',session)
        self.assertEqual(self.client.get('/').status_code,200)
        first=self.login(self.app.test_client(),login='wrong').get_data(as_text=True)
        second=self.login(self.app.test_client(),password='wrong').get_data(as_text=True)
        self.assertIn('Неверный логин или пароль.',first);self.assertIn('Неверный логин или пароль.',second)
        self.assertEqual(first.count('Неверный логин или пароль.'),second.count('Неверный логин или пароль.'))

    def test_csrf_login_logout_and_get_logout_has_no_effect(self):
        self.assertEqual(self.client.post('/login',data=dict(login=TEST_LOGIN,password=TEST_PASSWORD)).status_code,400)
        self.assertEqual(self.rows('SELECT * FROM auth_attempts'),[])
        self.login()
        self.assertEqual(self.client.get('/logout').status_code,405)
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertEqual(self.client.post('/logout',data={'csrf_token':'bad'}).status_code,400)
        with self.client.session_transaction() as session:token=session['csrf_token']
        self.assertEqual(self.client.post('/logout',data={'csrf_token':token}).status_code,303)
        self.assertEqual(self.client.get('/').status_code,303)
        self.assertEqual(self.client.post('/tickets/new',data={'csrf_token':token}).status_code,401)

    def test_absolute_eight_hour_lifetime_no_sliding(self):
        self.login();issued=self.instant
        self.instant+=7*3600
        self.assertEqual(self.client.get('/').status_code,200)
        with self.client.session_transaction() as session:self.assertEqual(session['owner_issued_at'],issued)
        self.instant=issued+8*3600-0.01;self.assertEqual(self.client.get('/settings/automation').status_code,200)
        self.instant=issued+8*3600;self.assertEqual(self.client.get('/').status_code,303)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM daily_summaries')[0][0],0)

    def test_safe_next_rejects_external_encoded_and_backslash_redirects(self):
        bad=['https://evil.example','//evil.example','/\\evil.example','/%2fevil.example','/%252fevil.example',
             '/%5cevil.example','/\n/evil','/..//evil','/login','/logout','/health','javascript:alert(1)',
             ' /tickets/1','/%0d%0aLocation:evil','/%252525252fexample']
        for value in bad:
            with self.subTest(value=value):self.assertIsNone(safe_next(value))
        self.assertEqual(safe_next('/?scope=active&status=Новая'),'/?scope=active&status=Новая')
        self.assertEqual(self.login(next='//evil.example').headers['Location'],'/dashboard')
        other=self.app.test_client();self.assertEqual(self.login(other,next='/settings/automation').headers['Location'],'/settings/automation')

    def test_every_business_route_denied_before_handlers_and_no_side_effects(self):
        paths=['/','/tickets/new','/tickets/1','/tickets/1/edit','/tickets/1/delete','/managers','/managers/new',
               '/managers/1/edit','/managers/1/activity','/tickets/1/tasks/new','/tickets/1/tasks/1/edit',
               '/tickets/1/tasks/1/complete','/tickets/1/tasks/1/cancel','/tickets/1/tasks/1/reschedule',
               '/tickets/1/actions','/tickets/1/proposal/send','/tickets/1/proposal/repeat','/settings/automation',
               '/dashboard','/summaries','/summaries/1','/tickets/export.csv','/summaries/1/export.csv']
        before=self.business_dump()
        for path in paths:
            methods=['GET']
            # Test every registered supported method, without auth or valid CSRF.
            adapter=self.app.url_map.bind('localhost')
            for method in ('GET','POST'):
                try:adapter.match(path,method=method)
                except Exception:continue
                with self.subTest(path=path,method=method):
                    response=self.client.open(path,method=method,data={'client':'Must never be inserted','password':'wrong'})
                    self.assertIn(response.status_code,(303,401))
                    self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(before,self.business_dump());self.assertEqual(self.rows('SELECT * FROM auth_attempts'),[])
        self.assertEqual(self.client.get('/health').json,{'status':'ok'})
        self.assertNotIn('Set-Cookie',self.client.get('/health').headers)
        response=self.client.get('/static/style.css');self.assertEqual(response.status_code,200);response.close()

    def test_rate_limit_persistent_expiry_cleanup_and_forged_proxy_headers(self):
        for _ in range(5):self.assertEqual(self.login(password='wrong').status_code,401)
        self.assertEqual(self.login().status_code,429)
        restarted=create_app(self.config).test_client()
        self.assertEqual(self.login(restarted).status_code,429)
        token=self.token(restarted)
        self.assertEqual(restarted.post('/login',data=dict(csrf_token=token,login=TEST_LOGIN,password=TEST_PASSWORD),headers={'X-Forwarded-For':'203.0.113.99'}).status_code,429)
        self.assertEqual(self.rows('SELECT client_ip FROM auth_blocks'),[('127.0.0.1',)])
        self.instant+=900
        self.assertEqual(self.login(restarted).status_code,303)
        self.assertEqual(self.rows('SELECT * FROM auth_blocks'),[])
        self.assertEqual(self.rows('SELECT * FROM auth_attempts'),[])

    def test_hash_runs_without_sqlite_lock_and_parallel_attempts_are_bounded(self):
        from werkzeug.security import check_password_hash as original
        def check(digest,password):
            with closing(sqlite3.connect(self.path,timeout=0.1)) as db:
                db.execute('BEGIN IMMEDIATE');db.rollback()
            return original(digest,password)
        with patch('auth_features.check_password_hash',side_effect=check):self.assertEqual(self.login().status_code,303)
        barrier=threading.Barrier(7)
        def visit(_):
            client=self.app.test_client();token=self.token(client);barrier.wait(timeout=10)
            return client.post('/login',data=dict(csrf_token=token,login=TEST_LOGIN,password='wrong')).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:codes=list(pool.map(visit,range(7)))
        self.assertTrue(all(c in (401,429) for c in codes));self.assertEqual(codes.count(401),5)
        self.assertEqual(self.login().status_code,429);self.assertEqual(len(self.rows('SELECT * FROM auth_blocks')),1)

    def test_cookies_no_store_hosts_and_no_proxy_trust(self):
        response=self.login();cookie=response.headers['Set-Cookie']
        self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Lax',cookie)
        self.assertNotIn('Secure;',cookie);self.assertNotIn('Domain=',cookie)
        prod=create_app(self.production());client=prod.test_client()
        response=client.get('/login',base_url='https://manager.gilinsky.ru')
        cookie=response.headers['Set-Cookie'];self.assertIn('Secure',cookie);self.assertNotIn('Domain=',cookie)
        self.assertEqual(client.get('/login',base_url='https://evil.example').status_code,400)
        response=client.get('/login',base_url='https://evil.example',headers={'X-Forwarded-Host':'manager.gilinsky.ru','X-Forwarded-Proto':'https'})
        self.assertEqual(response.status_code,400)
        for path in ('/login','/','/tickets/export.csv','/summaries/1/export.csv','/health'):
            self.assertEqual(self.client.get(path).headers['Cache-Control'],'no-store')

    def test_local_tool_no_overwrite_hidden_input_key_rotation_and_restarts(self):
        path=self.folder/'local-owner.json'
        def configure(replace=False,password=TEST_PASSWORD):
            entries=iter([password,password])
            return configure_owner(path,replace,input_fn=lambda prompt:TEST_LOGIN,password_fn=lambda prompt:next(entries))
        configure();original=path.read_bytes();values=json.loads(original)
        self.assertNotIn(TEST_PASSWORD,original.decode());self.assertTrue(check_password_hash(values['OWNER_PASSWORD_HASH'],TEST_PASSWORD))
        first=create_app({**self.config,'SECRET_KEY':values['SECRET_KEY']});client=first.test_client()
        self.assertEqual(self.login(client).status_code,303)
        second=create_app({**self.config,'SECRET_KEY':values['SECRET_KEY']})
        # Same saved key and real signed cookie survive application recreation.
        second_client=second.test_client();cookie=client.get_cookie('manager_tracker_session')
        second_client.set_cookie('manager_tracker_session',cookie.value)
        self.assertEqual(second_client.get('/').status_code,200)
        with self.assertRaises(ConfigurationError):configure()
        self.assertEqual(path.read_bytes(),original)
        configure(True,password='Another-test-password-2026');new=json.loads(path.read_bytes())
        self.assertNotEqual(new['SECRET_KEY'],values['SECRET_KEY'])
        self.assertTrue(check_password_hash(new['OWNER_PASSWORD_HASH'],'Another-test-password-2026'))
        third=create_app({**self.config,'SECRET_KEY':new['SECRET_KEY'],'OWNER_PASSWORD_HASH':new['OWNER_PASSWORD_HASH']})
        third_client=third.test_client();third_client.set_cookie('manager_tracker_session',cookie.value)
        self.assertEqual(third_client.get('/').status_code,303)
        local=load_settings(self.folder,overrides={'LOCAL_CONFIG_PATH':path},environ={})
        self.assertEqual(local['SECRET_KEY'],new['SECRET_KEY'])
        again=load_settings(self.folder,overrides={'LOCAL_CONFIG_PATH':path},environ={});self.assertEqual(again['SECRET_KEY'],local['SECRET_KEY'])

    def test_setup_mismatch_refusal_static_location_and_tty_requirement(self):
        path=self.folder/'local-owner.json';passwords=iter(['first-test-password','second-test-password'])
        with self.assertRaises(ConfigurationError):configure_owner(path,input_fn=lambda _:TEST_LOGIN,password_fn=lambda _:next(passwords))
        self.assertFalse(path.exists())
        with self.assertRaises(ConfigurationError):configure_owner(Path.cwd()/'static/local-owner.json')
        with patch('sys.stdin.isatty',return_value=False),patch('builtins.print'):
            self.assertEqual(setup_main(['--config',str(path)]),1)
        self.assertFalse(path.exists())

    def test_auth_migration_repeat_preservation_rollback_and_empty_database(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE auth_attempts');db.execute('DROP TABLE auth_blocks')
            db.execute('DELETE FROM schema_migrations WHERE version=?',(AUTH_MIGRATION,));db.commit()
            before=self.business_dump()
            def authorizer(action,a,b,c,d):return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TABLE and a=='auth_blocks' else sqlite3.SQLITE_OK
            db.set_authorizer(authorizer)
            with self.assertRaises(sqlite3.DatabaseError):migrate_database(db)
            db.set_authorizer(None)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='auth_attempts'").fetchone())
            migrate_database(db);migrate_database(db)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auth_attempts').fetchone()[0],0)
        self.assertEqual(before,self.business_dump())
        self.assertEqual(self.rows('SELECT COUNT(*) FROM daily_summaries')[0][0],0)

    def test_interactive_setup_main_and_running_instance_guard(self):
        from launcher import InstanceLock
        path=self.folder/'local-owner.json'
        with patch('setup_owner.ROOT',self.folder),patch('sys.stdin.isatty',return_value=True),patch('builtins.input',return_value=TEST_LOGIN),patch('getpass.getpass',side_effect=[TEST_PASSWORD,TEST_PASSWORD]) as hidden,patch('builtins.print'):
            self.assertEqual(setup_main(['--config',str(path)]),0)
            self.assertEqual(hidden.call_count,2)
        original=path.read_bytes();lock=InstanceLock(self.folder/'.runtime/launcher.lock');self.assertTrue(lock.acquire())
        try:
            with patch('setup_owner.ROOT',self.folder),patch('builtins.print'):
                self.assertEqual(setup_main(['--config',str(path),'--replace']),1)
        finally:lock.close()
        self.assertEqual(original,path.read_bytes())
