"""Explicit test credentials and real login; no auth bypass in the application."""
import hashlib
import re
from werkzeug.security import generate_password_hash
from app import create_app as application_factory

TEST_LOGIN='temporary-test-owner'
TEST_PASSWORD='Temporary-test-password-2026'
TEST_HASH=generate_password_hash(TEST_PASSWORD)


def create_app(config=None):
    config=dict(config or {})
    config.update(APP_MODE='local',OWNER_LOGIN=TEST_LOGIN,OWNER_PASSWORD_HASH=TEST_HASH,
                  SECRET_KEY=hashlib.sha256(str(config.get('SECRET_KEY','temporary-test-key')).encode()).hexdigest())
    return application_factory(config)


def authenticated_client(app):
    client=app.test_client()
    html=client.get('/login').get_data(as_text=True)
    token=re.search('name="csrf_token" value="([^"]+)"',html)[1]
    response=client.post('/login',data=dict(csrf_token=token,login=TEST_LOGIN,password=TEST_PASSWORD,next='/'))
    if response.status_code!=303:raise AssertionError('Test owner login failed')
    return client
