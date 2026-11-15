"""Private, consistent backups and maintenance locks; no application credentials."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat

class MaintenanceError(RuntimeError):pass

class FileLock:
    def __init__(self,path,shared=False):self.path=Path(path);self.shared=shared;self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.is_symlink():raise MaintenanceError('Недопустимый файл блокировки.')
        descriptor=os.open(self.path,os.O_RDWR|os.O_CREAT,0o600)
        if hasattr(os,'geteuid') and os.geteuid()==0:
            owner=self.path.parent.stat();os.fchown(descriptor,owner.st_uid,owner.st_gid)
        self.file=os.fdopen(descriptor,'r+b')
        try:
            if os.name=='nt':
                import msvcrt
                self.file.seek(0,2)
                if self.file.tell()==0:self.file.write(b' ');self.file.flush()
                self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_NB|(fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX))
        except OSError:
            self.file.close();self.file=None
            raise MaintenanceError('Трекер или другая операция обслуживания удерживает блокировку. Остановите только службу трекера для миграции/восстановления.') from None
        return self
    def __exit__(self,*args):
        if self.file:self.file.close();self.file=None


def lock_path(database):return Path(database).parent/'.manager-tracker-maintenance.lock'

def database_path(value):
    raw=Path(value).expanduser().absolute()
    if raw.is_symlink():raise MaintenanceError('Символическая ссылка вместо базы запрещена.')
    path=raw.resolve()
    if path.suffix.lower() not in ('.sqlite3','.sqlite','.db') or path.is_dir():raise MaintenanceError('Нужен обычный файл SQLite.')
    return path


def check_database(path):
    path=database_path(path)
    if not path.is_file():raise MaintenanceError('SQLite-файл не найден.')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise MaintenanceError('Проверка целостности или связей SQLite не пройдена.')
    return path


def private_json(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            json.dump(value,output,ensure_ascii=False,indent=2);output.flush();os.fsync(output.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def backup_copy(source,destination):
    source=database_path(source);destination=database_path(destination)
    if destination.exists():raise MaintenanceError('Назначение резервной копии уже существует.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
        check_database(destination)
    except BaseException:
        destination.unlink(missing_ok=True);raise
    return destination


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):h.update(block)
    return h.hexdigest()


def managed_backup(source,directory,kind='daily',now=None,keep=7):
    source=check_database(source);raw=Path(directory).expanduser().absolute();directory=raw.resolve()
    if raw.is_symlink() or directory.parent==directory or source.is_relative_to(directory) or not 1<=keep<=365:
        raise MaintenanceError('Небезопасный каталог резервирования или срок хранения.')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    marker=directory/'.manager-tracker-backups.json'
    with FileLock(directory/'.backup.lock'):
        if marker.is_symlink():raise MaintenanceError('Недопустимый каталог резервных копий.')
        if marker.exists():
            try:
                catalog=json.loads(marker.read_text(encoding='utf-8'))
                if catalog['format']!=1 or catalog['source']!=str(source) or not isinstance(catalog['files'],dict):raise ValueError
            except (ValueError,KeyError,TypeError):raise MaintenanceError('Чужой или повреждённый каталог резервирования.') from None
        else:
            if any(p.name!='.backup.lock' for p in directory.iterdir()):raise MaintenanceError('Непустой каталог без собственного маркера; файлы не тронуты.')
            catalog=dict(format=1,source=str(source),files={});private_json(marker,catalog)
        now=now or datetime.now(timezone.utc)
        if kind=='daily':name='manager-tracker-daily-'+now.date().isoformat()+'.sqlite3'
        elif kind in ('before-migrate','before-restore'):name='manager-tracker-'+kind+'-'+now.strftime('%Y%m%dT%H%M%S')+'-'+secrets.token_hex(4)+'.sqlite3'
        else:raise MaintenanceError('Неизвестный вид копии.')
        target=directory/name
        if target.exists() and (target.is_symlink() or catalog['files'].get(name)!=digest(target)):
            raise MaintenanceError('Существующая копия не принадлежит инструменту или изменена; замена отменена.')
        temp=directory/('pending-'+secrets.token_hex(8)+'.sqlite3')
        try:
            backup_copy(source,temp);os.replace(temp,target)
        finally:temp.unlink(missing_ok=True)
        catalog['files'][name]=digest(target);private_json(marker,catalog)
        daily=[]
        for candidate,checksum in list(catalog['files'].items()):
            if not re.fullmatch(r'manager-tracker-daily-[0-9]{4}-[0-9]{2}-[0-9]{2}\.sqlite3',candidate):continue
            path=directory/candidate
            if path.parent.resolve()!=directory or not path.is_file() or path.is_symlink():continue
            if not stat.S_ISREG(path.lstat().st_mode) or (hasattr(os,'getuid') and path.stat().st_uid!=os.getuid()):continue
            if digest(path)==checksum:daily.append(candidate)
        for old in sorted(daily,reverse=True)[keep:]:
            (directory/old).unlink();catalog['files'].pop(old)
        private_json(marker,catalog)
        return target


def restore_copy(source,destination,backup_dir=None,replace=False):
    source=check_database(source);destination=database_path(destination)
    if source==destination:raise MaintenanceError('Источник и назначение совпадают.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with FileLock(lock_path(destination)):
        if destination.exists():
            if not replace or backup_dir is None:raise MaintenanceError('Для замены нужны --replace-existing и --backup-dir; остановите службу трекера.')
            managed_backup(destination,backup_dir,kind='before-restore')
            with closing(sqlite3.connect(destination)) as db:
                if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:raise MaintenanceError('Есть активный SQLite writer.')
            if any(Path(str(destination)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
                raise MaintenanceError('Сохранились SQLite-журналы; автоматическая замена запрещена.')
        temp=destination.with_name('restore-'+secrets.token_hex(8)+'.sqlite3')
        try:
            backup_copy(source,temp);os.replace(temp,destination)
        finally:temp.unlink(missing_ok=True)
        check_database(destination)
    return destination
