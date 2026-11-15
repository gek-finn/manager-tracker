"""Immutable daily snapshots and spreadsheet-safe CSV, standard library only."""
import csv
import io
import json
from flask import Response

REPORT_MIGRATION = 'stage_5b_daily_summaries_v1'
FORMAT_VERSION = 1


def migrate_reports(connection, now):
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=?', (REPORT_MIGRATION,)).fetchone():
        return
    connection.execute("""CREATE TABLE daily_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        local_date TEXT NOT NULL UNIQUE,
        formed_at TEXT NOT NULL,
        format_version INTEGER NOT NULL,
        row_count INTEGER NOT NULL CHECK(row_count>=0),
        payload TEXT NOT NULL)""")
    connection.execute('INSERT INTO schema_migrations VALUES (?,?)', (REPORT_MIGRATION,now.isoformat()))


def date_text(value, parse):
    if not value: return ''
    try: return parse(value).isoformat(timespec='seconds')
    except (ValueError,TypeError,OverflowError): return 'Некорректная дата: '+str(value)


def row_snapshot(ticket, control, schedule, activity, parse):
    assigned = ticket['manager_id'] is not None
    manager_code = ('active' if ticket['manager_active'] else 'inactive') if assigned else 'unassigned'
    manager_label = {'active':'Активен','inactive':'Неактивен','unassigned':'Не назначен'}[manager_code]
    nearest = schedule['nearest']
    return dict(ticket_id=ticket['id'], client=ticket['client'],
        status=dict(code=ticket['status'],label=ticket['status']),
        manager=dict(id=ticket['manager_id'],name=ticket['manager_name'] if assigned else 'Не назначен',
                     inactive=assigned and not bool(ticket['manager_active']),code=manager_code,label=manager_label),
        activity=dict(code=activity['event_type'],label=activity['label'],occurred_at=activity['occurred_at'],
                      display_at=date_text(activity['occurred_at'],parse)) if activity else
                 dict(code='no_activity',label='Нет данных об активности',occurred_at=None,display_at=''),
        task=dict(id=nearest['id'],title=nearest['title'],due_at=nearest['due_at'],
                  display_due=date_text(nearest['due_at'],parse)) if nearest else None,
        deadline=dict(code=schedule['deadline']['code'],label=schedule['deadline']['label']),
        reasons=[dict(item) for item in control['labels']])


def encode_snapshot(instant, metrics, settings, rows):
    return json.dumps(dict(local_date=instant.date().isoformat(),formed_at=instant.isoformat(timespec='seconds'),
        format_version=FORMAT_VERSION,metrics=[dict(code=m['code'],label=m['label'],count=m['count']) for m in metrics],
        thresholds=dict(settings),rows=rows),ensure_ascii=False,separators=(',',':'))


def save_summary(connection, instant, metrics, settings, rows):
    """The caller owns the same transaction used to read all snapshot sources."""
    if not connection.in_transaction: raise RuntimeError('Требуется транзакция снимка.')
    local_date=instant.date().isoformat()
    existing=connection.execute('SELECT id,local_date,formed_at FROM daily_summaries WHERE local_date=?',(local_date,)).fetchone()
    if existing is not None: return existing
    connection.execute("""INSERT INTO daily_summaries(local_date,formed_at,format_version,row_count,payload)
        VALUES (?,?,?,?,?) ON CONFLICT(local_date) DO NOTHING""",
        (local_date,instant.isoformat(timespec='seconds'),FORMAT_VERSION,len(rows),encode_snapshot(instant,metrics,settings,rows)))
    return connection.execute('SELECT id,local_date,formed_at FROM daily_summaries WHERE local_date=?',(local_date,)).fetchone()


def csv_cell(value):
    value='' if value is None else str(value)
    stripped=value.lstrip().lstrip('\ufeff').lstrip()
    prefix=value[:len(value)-len(value.lstrip())]
    if stripped.startswith(('=','+','-','@')) or any(c in prefix for c in '\t\r\n'):
        return "'"+value
    return value


def csv_response(headers, rows, filename):
    out=io.StringIO(newline='')
    writer=csv.writer(out,delimiter=';',lineterminator='\r\n')
    writer.writerow(headers)
    writer.writerows([[csv_cell(cell) for cell in row] for row in rows])
    response=Response(out.getvalue().encode('utf-8-sig'),content_type='text/csv; charset=utf-8')
    response.headers['Content-Disposition']='attachment; filename="'+filename+'"'
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    return response


CURRENT_HEADERS=['Номер заявки','Дата создания','Клиент','Контакт','Статус','Приоритет','Ответственный',
    'Состояние менеджера','Последнее содержательное действие','Дата последнего содержательного действия',
    'Ближайшая открытая задача','Её срок','Состояние срока','Причины контроля']
SUMMARY_HEADERS=['Дата сводки','Время формирования','Номер заявки','Клиент','Сохранённый статус',
    'Сохранённый ответственный','Состояние менеджера','Сохранённое последнее содержательное действие',
    'Дата действия','Сохранённая ближайшая задача','Её срок','Сохранённое состояние срока','Сохранённые причины контроля']


def row_cells(row):
    return [row['ticket_id'],row['client'],row['status']['label'],row['manager']['name'],row['manager']['label'],
        row['activity']['label'],row['activity']['display_at'],row['task']['title'] if row['task'] else '',
        row['task']['display_due'] if row['task'] else '',row['deadline']['label'],
        ' | '.join(item['label'] for item in row['reasons'])]
