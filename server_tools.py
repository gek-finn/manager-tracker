"""Explicit server maintenance. No service/DNS changes and no shell execution."""
import argparse
from contextlib import closing
import getpass
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
from werkzeug.security import generate_password_hash
from app import ROOT,application_now,migrate_database,check_schema
from config_features import load_settings,ConfigurationError
from sqlite_maintenance import (MaintenanceError,FileLock,lock_path,database_path,check_database,
                                managed_backup,restore_copy,private_json)


def settings(config):
    result=load_settings(ROOT,{'SERVER_CONFIG_PATH':str(Path(config).absolute())},environ={})
    if result['APP_MODE']!='production':raise ConfigurationError('Команда требует production-конфигурацию.')
    return result


def migrate(config,backup_dir):
    values=settings(config);path=database_path(values['DATABASE'])
    with FileLock(lock_path(path)):
        if path.exists() and path.stat().st_size:managed_backup(path,backup_dir,kind='before-migrate',now=application_now())
        if not path.exists():
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        with closing(sqlite3.connect(path,timeout=10)) as db:
            db.execute('PRAGMA foreign_keys=ON');migrate_database(db);check_schema(db)
        check_database(path)
    return path


def setup(config,database,trust_proxy,replace=False,input_fn=None,password_fn=None):
    input_fn=input_fn or input;password_fn=password_fn or getpass.getpass
    config=Path(config).expanduser().absolute()
    if config.is_symlink() or config.name!='server-owner.json':raise ConfigurationError('Нужен закрытый файл server-owner.json без символической ссылки.')
    if any(config.resolve().is_relative_to(ROOT/name) for name in ('static','templates')):raise ConfigurationError('Секреты не могут находиться в static/templates.')
    config.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with FileLock(config.parent/'.server-setup.lock'):
        if config.exists() and not replace:raise ConfigurationError('Конфигурация уже существует. Для явной замены используйте --replace-config при остановленной службе.')
        path=database_path(database)
        if not path.parent.is_dir():raise ConfigurationError('Сначала создайте отдельный каталог базы с нужными правами.')
        with FileLock(lock_path(path)):
            login=input_fn('Логин владельца: ').strip()
            if not re.fullmatch(r'[\w.@-]{1,100}',login):raise ConfigurationError('Недопустимый формат логина.')
            password=password_fn('Пароль (12–1024 символа, ввод скрыт): ')
            if password!=password_fn('Повторите пароль: '):raise ConfigurationError('Пароли не совпали; конфигурация не изменена.')
            if not 12<=len(password)<=1024:raise ConfigurationError('Пароль должен содержать 12–1024 символа.')
            value=dict(APP_MODE='production',SECRET_KEY=secrets.token_hex(32),OWNER_LOGIN=login,
                       OWNER_PASSWORD_HASH=generate_password_hash(password),DATABASE=str(path),
                       TRUSTED_HOSTS=['manager.gilinsky.ru'],TRUST_LOCAL_PROXY=bool(trust_proxy))
            load_settings(ROOT,value,environ={})
            private_json(config,value)
    return config


def main(argv=None):
    parser=argparse.ArgumentParser(description='Manager Tracker: явное серверное обслуживание без публикации.')
    subs=parser.add_subparsers(dest='command',required=True)
    command=subs.add_parser('setup');command.add_argument('--config',required=True);command.add_argument('--database',required=True)
    command.add_argument('--trust-local-proxy',action='store_true');command.add_argument('--replace-config',action='store_true')
    for name in ('migrate','backup'):
        command=subs.add_parser(name);command.add_argument('--config',required=True);command.add_argument('--backup-dir',required=True)
    command=subs.add_parser('verify');command.add_argument('--database',required=True)
    command=subs.add_parser('restore');command.add_argument('--source',required=True);command.add_argument('--destination',required=True)
    command.add_argument('--replace-existing',action='store_true');command.add_argument('--backup-dir')
    args=parser.parse_args(argv)
    try:
        if args.command=='setup':
            if not sys.stdin.isatty():raise ConfigurationError('Запустите setup в интерактивной консоли для скрытого ввода пароля.')
            setup(args.config,args.database,args.trust_local_proxy,args.replace_config)
            print('Серверная конфигурация сохранена. Настройте закрытые права и перезапустите только службу трекера.')
        elif args.command=='migrate':migrate(args.config,args.backup_dir);print('Схема проверена/обновлена. Существующая база зарезервирована перед проверкой.')
        elif args.command=='backup':
            path=settings(args.config)['DATABASE']
            with FileLock(lock_path(path),shared=True):managed_backup(path,args.backup_dir,now=application_now())
            print('Согласованная копия проверена; сохранены до 7 собственных ежедневных копий.')
        elif args.command=='verify':
            path=check_database(args.database)
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:check_schema(db)
            print('Целостность, связи и версия схемы: OK.')
        elif args.command=='restore':
            restore_copy(args.source,args.destination,args.backup_dir,args.replace_existing)
            print('Восстановленная SQLite проверена. Службы не запускались и не останавливались.')
        return 0
    except (ConfigurationError,MaintenanceError) as error:print(str(error),file=sys.stderr);return 1
    except (OSError,sqlite3.Error,ValueError,EOFError,KeyboardInterrupt):
        print('Операция не завершена. Проверьте пути, права и целостность. Секреты не выводятся.',file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
