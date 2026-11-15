"""Create a fresh synthetic demonstration database; never read the working DB."""
import argparse
from contextlib import closing
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from app import application_now,migrate_database,STATUSES,PRIORITIES
from event_features import append_event,ticket_snapshot,task_snapshot
from sqlite_maintenance import database_path,check_database,digest,private_json,MaintenanceError

LABEL='[СИНТЕТИЧЕСКИЙ ДЕМО-ПРИМЕР; не реальное обращение] '


def create_demo(output,now=None):
    output=database_path(output);now=now or application_now()
    if output.exists():raise MaintenanceError('База уже существует. Перезапись запрещена; выберите новое имя для нового демонабора.')
    manifest=output.with_suffix('.manifest.json')
    if manifest.exists():raise MaintenanceError('Манифест уже существует; выберите новое имя.')
    output.parent.mkdir(parents=True,exist_ok=True)
    temp=output.with_name('new-demo-'+secrets.token_hex(8)+'.sqlite3')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    try:
        with closing(sqlite3.connect(temp)) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');migrate_database(db)
            with db:
                managers=[db.execute('INSERT INTO managers(name) VALUES (?)',('Демо Менеджер '+name,)).lastrowid for name in ('Север','Восток','Юг','Запад','Архив')]
                ticket_ids=[];task_ids=[]
                needs=['Запрос цены на пакет сопровождения','Консультация по выбору услуги','Подбор услуги для команды','Повторное обращение по условиям','Ожидание решения клиента']
                for i in range(25):
                    status=STATUSES[i%len(STATUSES)];closed=status=='Закрыта'
                    slot=i%7;manager=managers[slot] if slot<5 else None
                    if status=='КП отправлено':manager=managers[(i//5)%4]
                    created=now-timedelta(days=3+i%4);instant=now-timedelta(hours=30) if i%3==0 else now-timedelta(hours=1)
                    key=hashlib.sha256(('server-demo-ticket:'+str(i)).encode()).hexdigest()
                    ticket=db.execute("""INSERT INTO tickets(created_at,client,contact,description,manager,status,priority,next_action,submission_key,manager_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",(created.replace(tzinfo=None).isoformat(timespec='seconds'),'Демо Компания '+str(i+1),
                        'demo'+str(i+1)+'@example.com',LABEL+needs[i%5],'',status,PRIORITIES[i%3],'',key,manager)).lastrowid
                    ticket_ids.append(ticket)
                    row=db.execute('SELECT * FROM tickets WHERE id=?',(ticket,)).fetchone()
                    if i in (7,17):
                        append_event(db,ticket,'journal.enabled',now,{'text':LABEL+'Пример карточки без достоверной содержательной истории'})
                    else:
                        append_event(db,ticket,'ticket.created',created,{'ticket':ticket_snapshot(db,row),'text':LABEL+'Синтетическое создание для интерфейса'})
                        append_event(db,ticket,'manual.comment',instant,{'text':LABEL+needs[i%5]+'; результат и следующий шаг показаны для демонстрации.'})
                    if status=='КП отправлено':append_event(db,ticket,'proposal.sent',instant,{'text':LABEL+'Макет предложения отмечен отправленным; реальная отправка не выполнялась.'})
                    if i%4==0 and not closed:continue
                    due=(now+timedelta(days=7+i%4)) if i%2 else (now-timedelta(days=2+i%3))
                    state='Выполнена' if closed and i%2 else 'Отменена' if closed else 'Открыта'
                    title='Демо: '+('Связаться по предложению' if status=='КП отправлено' else 'Уточнить потребность клиента')
                    task=db.execute("""INSERT INTO tasks(ticket_id,title,manager_id,due_at,state,created_at,finished_at,result,submission_key)
                        VALUES (?,?,?,?,?,?,?,?,?)""",(ticket,title,manager,due.replace(tzinfo=None).isoformat(timespec='seconds'),state,
                        created.isoformat(timespec='seconds'),instant.isoformat(timespec='seconds') if closed else None,
                        LABEL+'Синтетический результат' if closed else '',hashlib.sha256(('server-demo-task:'+str(i)).encode()).hexdigest())).lastrowid
                    task_ids.append(task);row=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
                    append_event(db,ticket,'task.created',created,{'task':task_snapshot(db,row),'text':LABEL+'Макет задачи'},task_id=task)
                    if closed:append_event(db,ticket,'task.completed' if state=='Выполнена' else 'task.cancelled',instant,{'task':task_snapshot(db,row),'text':LABEL+'Пример завершения'},task_id=task)
                db.execute('UPDATE managers SET active=0 WHERE id=?',(managers[-1],))
                assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]==25
                assert db.execute('SELECT COUNT(*) FROM managers').fetchone()[0]==5
                assert db.execute('SELECT COUNT(*) FROM daily_summaries').fetchone()[0]==0
                assert db.execute('SELECT COUNT(*) FROM auth_attempts').fetchone()[0]==0
                assert db.execute('SELECT COUNT(*) FROM auth_blocks').fetchone()[0]==0
            check_database(temp)
        # Atomic no-clobber publication on NTFS/Linux local filesystems.
        os.link(temp,output)
        private_json(manifest,dict(kind='manager-tracker-clean-synthetic-demo-v1',prepared_at=now.isoformat(),database=output.name,
            sha256=digest(output),managers=5,active_managers=4,tickets=25,synthetic=True,credentials=False,manager_ids=managers,ticket_ids=ticket_ids,task_ids=task_ids,
            warning='Все события синтетические, не история реальных обращений. Сроки естественно устаревают при задержке публикации.'))
        return output
    finally:temp.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Создать только новую чистую вымышленную демобазу.')
    parser.add_argument('--output',required=True,type=Path);args=parser.parse_args(argv)
    try:
        create_demo(args.output);print('Создана новая синтетическая база: 5 менеджеров, 25 заявок. Секретов и входов владельца нет.');return 0
    except (MaintenanceError,OSError,sqlite3.Error,ValueError):print('Не удалось создать новую демобазу. Проверьте путь; существующие базы не перезаписываются.',file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
