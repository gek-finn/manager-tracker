"""One owner, absolute session lifetime and shared SQLite login throttling."""
import ipaddress
import secrets
import time
from urllib.parse import urlsplit,unquote
from flask import abort,redirect,render_template,request,session,url_for
from werkzeug.security import check_password_hash

AUTH_MIGRATION='stage_6a_login_limits_v1'
WINDOW=15*60
SESSION_SECONDS=8*60*60


def auth_now():return time.time()


def migrate_auth(connection,now):
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=?',(AUTH_MIGRATION,)).fetchone():return
    connection.execute("""CREATE TABLE auth_attempts (
        id TEXT PRIMARY KEY,client_ip TEXT NOT NULL,created_at REAL NOT NULL,
        failed INTEGER NOT NULL DEFAULT 0 CHECK(failed IN (0,1)))""")
    connection.execute('CREATE INDEX auth_attempts_ip_time ON auth_attempts(client_ip,created_at)')
    connection.execute('CREATE TABLE auth_blocks (client_ip TEXT PRIMARY KEY,blocked_until REAL NOT NULL)')
    connection.execute('INSERT INTO schema_migrations VALUES (?,?)',(AUTH_MIGRATION,now.isoformat()))


def safe_next(value):
    if not isinstance(value,str) or len(value)>2048 or not value.startswith('/'):return None
    decoded=value
    for _ in range(4):
        new=unquote(decoded)
        if new==decoded:break
        decoded=new
    else:return None
    if decoded.startswith('//') or '\\' in decoded or any(ord(c)<32 or ord(c)==127 for c in decoded):return None
    parts=urlsplit(decoded)
    if parts.scheme or parts.netloc or any(p in ('.','..') for p in parts.path.split('/')):return None
    if parts.path in ('/login','/logout','/health'):return None
    return value


def reserve_attempt(connection,ip,instant):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('DELETE FROM auth_attempts WHERE created_at<=?',(instant-WINDOW,))
        connection.execute('DELETE FROM auth_blocks WHERE blocked_until<=?',(instant,))
        blocked=connection.execute('SELECT blocked_until FROM auth_blocks WHERE client_ip=?',(ip,)).fetchone()
        if blocked is not None:return None
        count=connection.execute('SELECT COUNT(*) FROM auth_attempts WHERE client_ip=?',(ip,)).fetchone()[0]
        if count>=5:return None  # includes in-flight checks; do not start unlimited hashes
        token=secrets.token_hex(32)
        connection.execute('INSERT INTO auth_attempts(id,client_ip,created_at) VALUES (?,?,?)',(token,ip,instant))
        return token


def finish_attempt(connection,token,ip,valid,instant):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        attempt=connection.execute('SELECT 1 FROM auth_attempts WHERE id=? AND client_ip=?',(token,ip)).fetchone()
        if attempt is None:return False
        if valid:
            connection.execute('DELETE FROM auth_attempts WHERE id=?',(token,))
            block=connection.execute('SELECT 1 FROM auth_blocks WHERE client_ip=? AND blocked_until>?',(ip,instant)).fetchone()
            return block is None
        connection.execute('UPDATE auth_attempts SET failed=1,created_at=? WHERE id=?',(instant,token))
        count=connection.execute('SELECT COUNT(*) FROM auth_attempts WHERE client_ip=? AND failed=1 AND created_at>?',(ip,instant-WINDOW)).fetchone()[0]
        if count>=5:
            connection.execute("""INSERT INTO auth_blocks VALUES (?,?) ON CONFLICT(client_ip)
                DO UPDATE SET blocked_until=MAX(blocked_until,excluded.blocked_until)""",(ip,instant+WINDOW))
        return False


def register_auth(app,db):
    @app.before_request
    def require_owner():
        if request.routing_exception is not None:raise request.routing_exception
        if request.endpoint in ('login','static','health'):return
        issued=session.get('owner_issued_at')
        instant=auth_now()
        if session.get('owner_authenticated') is True and isinstance(issued,(int,float)) and 0<=instant-issued<SESSION_SECONDS:return
        session.clear()
        if request.method in ('GET','HEAD'):
            return redirect(url_for('login',next=safe_next(request.full_path.rstrip('?')) or '/dashboard'),code=303)
        return render_template('error.html',code=401,message='Войдите в приложение. Операция не выполнена; повторите её после входа.'),401

    @app.after_request
    def private_responses(response):
        if request.endpoint!='static':response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    @app.get('/health')
    def health():return {'status':'ok'}

    @app.route('/login',methods=['GET','POST'])
    def login():
        target=safe_next(request.form.get('next') if request.method=='POST' else request.args.get('next')) or '/dashboard'
        error=None;code=200
        if request.method=='POST':
            try:ip=str(ipaddress.ip_address(request.remote_addr or ''))
            except ValueError:ip='unknown'
            token=reserve_attempt(db(),ip,auth_now())
            if token is None:
                error='Слишком много попыток входа. Повторите позже; блокировка после 5 ошибок действует 15 минут.';code=429
            else:
                password=request.form.get('password','')
                # No SQLite write transaction is held during the expensive hash check.
                checked=check_password_hash(app.config['OWNER_PASSWORD_HASH'],password if len(password)<=1024 else '')
                valid=checked and secrets.compare_digest(request.form.get('login','').encode('utf-8'),app.config['OWNER_LOGIN'].encode('utf-8')) and len(password)<=1024
                allowed=finish_attempt(db(),token,ip,valid,auth_now())
                if valid and allowed:
                    session.clear();session.permanent=True
                    session['owner_authenticated']=True;session['owner_issued_at']=auth_now()
                    session['csrf_token']=secrets.token_urlsafe(32)
                    return redirect(target,code=303)
                error='Неверный логин или пароль.';code=401
        return render_template('login.html',error=error,next_path=target),code

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'),code=303)
