"""Production WSGI factory; shared lock excludes migrations and restores."""
from pathlib import Path
from app import create_app,ROOT
from config_features import load_settings,ConfigurationError
from sqlite_maintenance import FileLock,lock_path

def create_server_app(overrides=None):
    options={'APP_MODE':'production'};options.update(overrides or {})
    values=load_settings(ROOT,options)
    if values['APP_MODE']!='production':raise ConfigurationError('WSGI требует явной конфигурации production.')
    guard=FileLock(lock_path(values['DATABASE']),shared=True)
    guard.__enter__()
    try:app=create_app(values)
    except BaseException:guard.__exit__(None,None,None);raise
    app.extensions['maintenance_lock']=guard
    return app
