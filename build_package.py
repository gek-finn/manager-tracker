"""Allowlisted source-only distribution; never traverse runtime/instance/backups."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parent
FILES=(
'app.py','task_features.py','event_features.py','automation_features.py','report_features.py',
'auth_features.py','config_features.py','sqlite_maintenance.py','server_runtime.py','server_tools.py','server_demo.py','wsgi.py',
'requirements.txt','requirements-production.txt',
'static/style.css','static/app.js',
'templates/base.html','templates/index.html','templates/form.html','templates/detail.html','templates/delete.html','templates/error.html',
'templates/managers.html','templates/manager_form.html','templates/task_form.html','templates/tasks_section.html','templates/history_section.html',
'templates/automation_settings.html','templates/proposal_form.html','templates/dashboard.html','templates/summaries.html','templates/summary_detail.html','templates/login.html',
'deploy/gunicorn.conf.py','deploy/manager-tracker.service','deploy/manager-tracker-backup.service','deploy/manager-tracker-backup.timer',
'deploy/nginx-http.conf','deploy/nginx-https.conf','deploy/certbot-deploy-hook.sh','deploy/server-config.example.json','deploy/INSTALL.md')


def build_archive(output,root=ROOT):
    output=Path(output).absolute();root=Path(root).resolve()
    if output.exists():raise FileExistsError('Архив уже существует; выберите новое имя.')
    entries=[]
    for name in FILES:
        source=root/name
        if source.is_symlink() or not source.resolve().is_relative_to(root) or not source.is_file():raise ValueError('Не найден разрешённый исходник: '+name)
        data=source.read_bytes().replace(b'\r\n',b'\n')  # Unix shebangs and deployment files must use LF
        entries.append((name,data,hashlib.sha256(data).hexdigest()))
    output.parent.mkdir(parents=True,exist_ok=True)
    try:
        with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
            for name,data,checksum in entries:archive.writestr(name,data)
            archive.writestr('PACKAGE-MANIFEST.json',json.dumps(dict(format=1,files=[dict(path=n,sha256=h,bytes=len(d)) for n,d,h in entries]),ensure_ascii=False,indent=2))
        with zipfile.ZipFile(output) as archive:
            assert set(archive.namelist())==set(FILES)|{'PACKAGE-MANIFEST.json'}
            assert archive.testzip() is None
    except BaseException:
        output.unlink(missing_ok=True);raise
    output.with_suffix('.contents.txt').write_text('\n'.join(n for n,_,_ in entries)+'\nPACKAGE-MANIFEST.json\n',encoding='utf-8')
    return output


def main(argv=None):
    parser=argparse.ArgumentParser(description='Собрать только разрешённые исходники без данных и секретов.')
    parser.add_argument('--output',required=True,type=Path);args=parser.parse_args(argv)
    try:build_archive(args.output);print('Архив проверен: '+str(len(FILES)+1)+' разрешённых файлов, без SQLite и секретов.');return 0
    except (OSError,ValueError):print('Не удалось собрать архив: проверьте исходники и новое имя назначения.');return 1
if __name__=='__main__':raise SystemExit(main())
