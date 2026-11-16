from datetime import date, timedelta
import os
import pytest
from test_activities import web
from running.key_vault import KeyVault
from running.garmin_client import GarminClient, GarminError, validate_key


def test_demo_connect_sync_disconnect_never_masquerades_as_real(web):
    c, uid, app = web
    expiry=(date.today()+timedelta(days=30)).isoformat()
    assert c.post('/api/running/garmin/connect',json={'mode':'demo','expiry':expiry}).status_code == 200
    status=c.get('/api/running/garmin').json
    assert status['mode']=='demo' and status['status']=='demo'
    assert 'key' not in status and 'key_encrypted' not in status
    assert c.post('/api/running/garmin/sync').json['added']==1
    assert c.post('/api/running/garmin/sync').json['added']==0
    assert c.get('/api/running/activities').json['activities'][0]['source']=='demo'
    assert c.get('/api/running/dashboard').json['weekly_km']==0
    uid[0]=2
    assert c.get('/api/running/garmin').json['status']=='disconnected'
    uid[0]=1
    assert c.delete('/api/running/garmin').status_code==200
    assert c.get('/api/running/garmin').json['status']=='disconnected'
    assert len(c.get('/api/running/activities').json['activities'])==1
    assert c.post('/api/running/garmin/sync').status_code==409


@pytest.mark.parametrize('key',['','a b','a\nb',' a','密钥',None])
def test_key_validation(key):
    with pytest.raises(ValueError): validate_key(key)


def test_encrypted_fallback_reopens_and_permissions(tmp_path):
    vault=KeyVault(tmp_path, use_keyring=False)
    secret='SYNTHETIC-TEST-KEY-DO-NOT-USE'
    encrypted=vault.encrypt(secret)
    assert secret not in encrypted
    assert KeyVault(tmp_path,use_keyring=False).decrypt(encrypted)==secret
    assert os.stat(tmp_path/'garmin-vault.key').st_mode & 0o777 == 0o600


def test_secure_keyring_master_is_reused_without_file(tmp_path,monkeypatch):
    import keyring
    saved={}
    class SecureBackend:
        __module__='keyring.backends.macOS'
        def get_password(self,service,name): return saved.get((service,name))
        def set_password(self,service,name,value): saved[(service,name)]=value
    monkeypatch.setattr(keyring,'get_keyring',lambda:SecureBackend())
    vault=KeyVault(tmp_path)
    encrypted=vault.encrypt('SYNTHETIC-ONLY')
    assert vault.backend=='os-keyring'
    assert KeyVault(tmp_path).decrypt(encrypted)=='SYNTHETIC-ONLY'
    assert not (tmp_path/'garmin-vault.key').exists()
    assert len(saved)==1


def test_fallback_rejects_publicly_readable_master(tmp_path):
    path=tmp_path/'garmin-vault.key'
    path.write_bytes(b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    path.chmod(0o644)
    with pytest.raises(ValueError):KeyVault(tmp_path,use_keyring=False).encrypt('SYNTHETIC-ONLY')


def test_client_token_exchange_retry_and_secret_safe_errors():
    calls=[]
    answers=[(200,b'{"access_token":"MOCK-TOKEN","expires_in":3600}'),(429,b'error'),(200,b'[]')]
    def transport(method,url,headers):
        calls.append((method,url,headers));return answers.pop(0)
    client=GarminClient('SYNTHETIC-KEY',transport=transport,sleep=lambda _:None)
    assert client.list_activities()==[]
    assert calls[0][0]=='POST'
    assert calls[0][2]['Authorization']=='ApiKey SYNTHETIC-KEY'
    assert calls[1][2]['Authorization']=='Bearer MOCK-TOKEN'
    assert calls[0][1].startswith('https://personal.connectapi.garmin.com/')
    def failing(*args): raise RuntimeError('SYNTHETIC-KEY MOCK-TOKEN')
    client=GarminClient('SYNTHETIC-KEY',transport=failing,sleep=lambda _:None)
    with pytest.raises(GarminError) as exc: client.test_key()
    assert 'SYNTHETIC-KEY' not in str(exc.value)
    assert 'MOCK-TOKEN' not in str(exc.value)


def test_mock_live_encrypts_tests_before_save_sync_and_disconnect(web, tmp_path, caplog):
    c, uid, app = web
    secret='SYNTHETIC-LIVE-KEY-NOT-A-CREDENTIAL'
    calls=[]
    class MockClient:
        def __init__(self,key):
            assert key==secret
        def test_key(self): calls.append('test')
        def list_activities(self,**kwargs):
            calls.append('list')
            return [dict(activityId=10,startTimeGMT='2026-11-16 06:00:00',activityType={'typeKey':'running'},distance=5000,duration=1800)]
    app.config.update(GARMIN_LIVE_ENABLED=True,GARMIN_CLIENT_FACTORY=MockClient)
    # Keep all tests independent of the host's real OS keychain.
    import running.garmin_routes as routes
    from unittest.mock import patch
    with patch.object(KeyVault,'_master',lambda self: b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='):
        response=c.post('/api/running/garmin/connect',json={'mode':'live','key':secret,'expiry':(date.today()+timedelta(days=30)).isoformat()})
        assert response.status_code==200
        assert calls==['test']
        assert secret not in response.get_data(as_text=True)
        assert secret not in app.extensions['running_store'].path.read_bytes().decode('latin1')
        assert c.get('/api/running/garmin').json['status']=='active'
        assert c.post('/api/running/garmin/sync').json['added']==1
        assert c.post('/api/running/garmin/sync').json['added']==0
        assert c.get('/api/running/activities').json['activities'][0]['source']=='api'
        assert c.delete('/api/running/garmin').status_code==200
        assert c.get('/api/running/garmin').json['status']=='disconnected'
    assert secret not in caplog.text


def test_failed_token_never_saves_connection(web):
    c,uid,app=web
    class Refused:
        def __init__(self,key): pass
        def test_key(self): raise GarminError('echo SYNTHETIC-SECRET')
    app.config.update(GARMIN_LIVE_ENABLED=True,GARMIN_CLIENT_FACTORY=Refused)
    r=c.post('/api/running/garmin/connect',json={'mode':'live','key':'SYNTHETIC-SECRET','expiry':(date.today()+timedelta(days=1)).isoformat()})
    assert r.status_code==502
    assert 'SYNTHETIC-SECRET' not in r.get_data(as_text=True)
    assert c.get('/api/running/garmin').json['status']=='disconnected'


def test_client_401_refreshes_once_and_redirects_fail_closed():
    answers=[(200,b'FIRST-TOKEN'),(401,b''),(200,b'NEXT-TOKEN'),(200,b'[]')]
    c=GarminClient('SYNTHETIC-KEY',transport=lambda *args: answers.pop(0))
    assert c.list_activities()==[]
    c=GarminClient('SYNTHETIC-KEY',transport=lambda *args:(302,b'echo SYNTHETIC-KEY'))
    with pytest.raises(GarminError) as exc:c.test_key()
    assert exc.value.status==302
    assert 'SYNTHETIC-KEY' not in str(exc.value)


def test_live_disabled_and_bad_expiry_do_not_save(web):
    c,uid,app=web
    assert c.post('/api/running/garmin/connect',json={'mode':'live','key':'SYNTHETIC-KEY','expiry':(date.today()+timedelta(days=30)).isoformat()}).status_code==409
    for expiry in ['bad','2020-01-01',(date.today()+timedelta(days=367)).isoformat()]:
        assert c.post('/api/running/garmin/connect',json={'mode':'demo','expiry':expiry}).status_code==400
    assert c.get('/api/running/garmin').json['status']=='disconnected'
