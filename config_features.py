"""Explicit local/production settings. Never print configuration values."""
import json
import os
from pathlib import Path
import re

class ConfigurationError(RuntimeError):
    pass

ENV_KEYS={'APP_MODE':'TRACKER_MODE','SECRET_KEY':'TRACKER_SECRET_KEY','OWNER_LOGIN':'TRACKER_OWNER_LOGIN',
          'OWNER_PASSWORD_HASH':'TRACKER_OWNER_PASSWORD_HASH','DATABASE':'TRACKER_DATABASE','TRUSTED_HOSTS':'TRACKER_ALLOWED_HOSTS','TRUST_LOCAL_PROXY':'TRACKER_TRUST_LOCAL_PROXY'}


def valid_password_hash(value):
    if not isinstance(value,str): return False
    try:
        method,salt,digest=value.split('$')
        if not re.fullmatch(r'[A-Za-z0-9]{8,64}',salt): return False
        if method.startswith('scrypt:'):
            _,n,r,p=method.split(':')
            return int(n) in (32768,65536) and int(r)==8 and int(p)==1 and bool(re.fullmatch(r'[0-9a-f]{128}',digest))
        if method.startswith('pbkdf2:sha256:'):
            return 600000<=int(method.rsplit(':',1)[1])<=2000000 and bool(re.fullmatch(r'[0-9a-f]{64}',digest))
    except (ValueError,TypeError): pass
    return False


def validate_local_file(path,root):
    path=Path(path).expanduser().absolute()
    if path.is_symlink() or path.name!='local-owner.json':
        raise ConfigurationError('Файл локальной конфигурации должен называться local-owner.json и не быть символической ссылкой.')
    resolved=path.resolve()
    if any(resolved.is_relative_to(root/name) for name in ('static','templates')):
        raise ConfigurationError('Конфигурация не может находиться в static или templates.')
    return resolved


def load_settings(root, overrides=None, environ=None):
    overrides=dict(overrides or {});env=os.environ if environ is None else environ
    server_values={}
    server_path=overrides.get('SERVER_CONFIG_PATH',env.get('TRACKER_SERVER_CONFIG'))
    if server_path:
        path=Path(server_path).expanduser()
        if not path.is_absolute() or path.is_symlink() or any(path.resolve().is_relative_to(root/name) for name in ('static','templates')):
            raise ConfigurationError('Недопустимый путь закрытой серверной конфигурации.')
        try:
            server_values=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(server_values,dict) or server_values.get('APP_MODE')!='production':raise ValueError
            allowed={'APP_MODE','SECRET_KEY','OWNER_LOGIN','OWNER_PASSWORD_HASH','DATABASE','TRUSTED_HOSTS','TRUST_LOCAL_PROXY'}
            if set(server_values)-allowed:raise ValueError
        except (ValueError,OSError,UnicodeError):
            raise ConfigurationError('Не удалось прочитать серверную JSON-конфигурацию production.') from None
    mode=overrides.get('APP_MODE',env.get('TRACKER_MODE',server_values.get('APP_MODE','local')))
    if server_path and mode!='production':raise ConfigurationError('Серверная конфигурация требует режима production.')
    if mode not in ('local','production'):
        raise ConfigurationError('TRACKER_MODE: допустимы local или production.')
    values=dict(server_values)
    if mode=='local':
        path=validate_local_file(overrides.get('LOCAL_CONFIG_PATH',env.get('TRACKER_LOCAL_CONFIG',root/'instance/local-owner.json')),root)
        if path.exists():
            try:
                values=json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(values,dict):raise ValueError
            except (ValueError,OSError,UnicodeError):
                raise ConfigurationError('Не удалось прочитать локальную конфигурацию. Проверьте файл или выполните python setup_owner.py --replace.') from None
    for key,name in ENV_KEYS.items():
        if name in env:values[key]=env[name]
    values.update(overrides);values['APP_MODE']=mode
    proxy=values.get('TRUST_LOCAL_PROXY',False)
    if isinstance(proxy,str) and proxy in ('0','1'):proxy=proxy=='1'
    if not isinstance(proxy,bool) or (proxy and mode!='production'):
        raise ConfigurationError('TRUST_LOCAL_PROXY: допустимы 0/1; доверие включается только явно в production.')
    values['TRUST_LOCAL_PROXY']=proxy
    required=['SECRET_KEY','OWNER_LOGIN','OWNER_PASSWORD_HASH']+(['DATABASE','TRUSTED_HOSTS'] if mode=='production' else [])
    missing=[ENV_KEYS[k] for k in required if not values.get(k)]
    if missing:
        instruction=' Выполните python setup_owner.py для первоначальной локальной настройки.' if mode=='local' else ''
        raise ConfigurationError('Не заданы обязательные параметры: '+', '.join(missing)+'.'+instruction)
    if not isinstance(values['SECRET_KEY'],str) or len(values['SECRET_KEY'])<32 or not values['SECRET_KEY'].strip():
        raise ConfigurationError('SECRET_KEY должен содержать не менее 32 символов; используйте криптографически случайный ключ.')
    if not isinstance(values['OWNER_LOGIN'],str) or not re.fullmatch(r'[\w.@-]{1,100}',values['OWNER_LOGIN']):
        raise ConfigurationError('Логин: 1–100 букв, цифр или символов . @ _ - без пробелов.')
    if not valid_password_hash(values['OWNER_PASSWORD_HASH']):
        raise ConfigurationError('Некорректный или недостаточно стойкий хеш пароля владельца. Создайте его инструментом настройки Werkzeug.')
    if mode=='production' and (values.get('DEBUG') or values.get('TESTING')):
        raise ConfigurationError('DEBUG и TESTING запрещены в производственном режиме.')
    raw=values.get('DATABASE',root/'instance/tracker.sqlite3')
    if not isinstance(raw,(str,Path)) or not str(raw) or str(raw)==':memory:' or any(c in str(raw) for c in ('\x00','?','\n','\r')):
        raise ConfigurationError('Укажите обычный файловый путь SQLite.')
    path=Path(raw).expanduser()
    if mode=='production' and not path.is_absolute():
        raise ConfigurationError('В production нужен абсолютный путь SQLite.')
    path=(root/path).resolve() if not path.is_absolute() else path.resolve()
    if path.is_dir() or path.suffix.lower() not in ('.sqlite3','.sqlite','.db') or any(path.is_relative_to(root/name) for name in ('static','templates')):
        raise ConfigurationError('Недопустимое расположение или расширение файла SQLite.')
    if mode=='production' and not path.parent.is_dir():
        raise ConfigurationError('Каталог производственной базы должен существовать заранее.')
    if path.is_file() and path.stat().st_size:
        try:
            with path.open('rb') as stream:valid=stream.read(16)==b'SQLite format 3\x00'
        except OSError:raise ConfigurationError('Нет доступа к файлу SQLite.') from None
        if not valid:raise ConfigurationError('Указанный файл не является SQLite-базой.')
    hosts=values.get('TRUSTED_HOSTS')
    if mode=='production':
        if isinstance(hosts,str):hosts=[h.strip().lower() for h in hosts.split(',')]
        if not isinstance(hosts,(list,tuple)) or not hosts or any(not isinstance(h,str) or len(h)>253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',h) or '..' in h or any(not label or len(label)>63 or label.startswith('-') or label.endswith('-') for label in h.split('.')) for h in hosts):
            raise ConfigurationError('TRACKER_ALLOWED_HOSTS: перечислите точные имена хостов без схем, портов и масок.')
    else:hosts=['127.0.0.1','localhost','[::1]']
    values.update(DATABASE=str(path),TRUSTED_HOSTS=list(hosts),DEBUG=False,
                  SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=mode=='production',
                  SESSION_COOKIE_DOMAIN=None,SESSION_COOKIE_NAME='manager_tracker_session',SESSION_REFRESH_EACH_REQUEST=False)
    return values


class LocalProxy:
    """Trust exactly one XFF/XFP hop, only from the local reverse proxy peer."""
    def __init__(self,application):
        from werkzeug.middleware.proxy_fix import ProxyFix
        self.direct=application
        self.proxied=ProxyFix(application,x_for=1,x_proto=1,x_host=0,x_port=0,x_prefix=0)
    def __call__(self,environ,start_response):
        application=self.proxied if environ.get('REMOTE_ADDR') in ('127.0.0.1','::1') else self.direct
        return application(environ,start_response)
