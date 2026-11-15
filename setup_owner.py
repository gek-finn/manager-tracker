"""Interactive local setup; never accept passwords in command-line arguments."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
from werkzeug.security import generate_password_hash
from config_features import ConfigurationError,validate_local_file
from launcher import InstanceLock

ROOT=Path(__file__).resolve().parent


def configure_owner(path,replace=False,input_fn=None,password_fn=None):
    input_fn=input_fn or input;password_fn=password_fn or getpass.getpass
    path=validate_local_file(path,ROOT)
    if path.exists() and not replace:
        raise ConfigurationError('Конфигурация уже существует. Для явной смены владельца/пароля остановите приложение и используйте --replace.')
    login=input_fn('Логин владельца: ').strip()
    if not re.fullmatch(r'[\w.@-]{1,100}',login):raise ConfigurationError('Логин: 1–100 букв, цифр или . @ _ - без пробелов.')
    password=password_fn('Пароль (не менее 12 символов, ввод скрыт): ')
    confirm=password_fn('Повторите пароль: ')
    if password!=confirm:raise ConfigurationError('Пароли не совпали. Конфигурация не изменена.')
    if not 12<=len(password)<=1024:raise ConfigurationError('Пароль должен содержать от 12 до 1024 символов.')
    database='instance/tracker.sqlite3'
    if replace and path.exists():
        try:database=json.loads(path.read_text(encoding='utf-8')).get('DATABASE',database)
        except (ValueError,OSError,AttributeError):raise ConfigurationError('Не удалось прочитать существующую конфигурацию; файл не изменён.') from None
    values=dict(SECRET_KEY=secrets.token_hex(32),OWNER_LOGIN=login,
                OWNER_PASSWORD_HASH=generate_password_hash(password),DATABASE=database)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+secrets.token_hex(8)+'.tmp')
    try:
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            json.dump(values,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        if path.exists() and not replace:raise ConfigurationError('Конфигурация уже существует; перезапись отменена.')
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)
    return path


def main(argv=None):
    parser=argparse.ArgumentParser(description='Первоначальная настройка владельца. Пароль вводится скрыто.')
    parser.add_argument('--replace',action='store_true',help='Явно заменить локального владельца/пароль и ключ сессии.')
    parser.add_argument('--config',type=Path,default=ROOT/'instance/local-owner.json',help='Путь закрытой локальной JSON-конфигурации.')
    args=parser.parse_args(argv)
    lock=InstanceLock(ROOT/'.runtime/launcher.lock')
    try:
        if not lock.acquire():raise ConfigurationError('Приложение работает. Остановите его через Ctrl+C перед настройкой.')
        import sys
        if not sys.stdin.isatty():raise ConfigurationError('Запустите python setup_owner.py в интерактивной консоли: пароль вводится только скрыто.')
        configure_owner(args.config,args.replace)
        print('Локальный владелец настроен. Запустите запуск.bat. При смене пароля ключ сессии также заменён; перезапустите приложение.')
        return 0
    except (ConfigurationError,OSError,EOFError,KeyboardInterrupt):
        # ConfigurationError messages contain no submitted values.
        import sys
        error=sys.exc_info()[1]
        print(str(error) if isinstance(error,ConfigurationError) else 'Настройка не завершена. Проверьте доступ к файлу и повторите ввод.')
        return 1
    finally:lock.close()

if __name__=='__main__':raise SystemExit(main())
