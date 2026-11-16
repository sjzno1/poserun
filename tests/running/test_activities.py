import io
import struct
import pytest
from flask import Flask, g
from fitparse.records import Crc
from running import register_running


def sample_fit(sport=1):
    # Valid synthetic FIT session: 2026-11-16 UTC, running, 5 km, 30 min.
    fields = [(2,4,0x86),(5,1,0),(9,4,0x86),(8,4,0x86),(16,1,2)]
    definition = bytes([0x40,0,0])+struct.pack('<H',18)+bytes([len(fields)])+b''.join(bytes(f) for f in fields)
    data = definition + bytes([0])+struct.pack('<IBIIB',1163808000,sport,500000,1800000,145)
    header = struct.pack('<BBHI4s',12,0x10,100,len(data),b'.FIT')
    body = header+data
    return body+struct.pack('<H',Crc.calculate(body))


@pytest.fixture
def web(tmp_path):
    app = Flask(__name__, static_folder='../../static', static_url_path='')
    app.config['TESTING'] = True
    uid = [1]
    @app.before_request
    def user():
        if uid[0] is not None:
            g.current_user = {'id': uid[0]}
    register_running(app, data_dir=tmp_path)
    return app.test_client(), uid, app


def test_import_detail_dedup_and_user_isolation(web):
    c, uid, app = web
    def upload():
        return c.post('/api/running/activities/import', data={'file':(io.BytesIO(sample_fit()),'sample.fit')})
    r = upload()
    assert r.status_code == 201, r.json
    activity = r.json['activity']
    assert activity['distance_m'] == 5000
    assert activity['duration_s'] == 1800
    assert activity['avg_hr'] == 145
    assert activity['source'] == 'fit'
    assert upload().status_code == 200
    assert len(c.get('/api/running/activities').json['activities']) == 1
    assert c.get('/api/running/activities/'+str(activity['id'])).status_code == 200
    uid[0] = 2
    assert c.get('/api/running/activities').json['activities'] == []
    assert c.get('/api/running/activities/'+str(activity['id'])).status_code == 404
    assert upload().status_code == 201
    uid[0] = None
    assert c.get('/api/running/activities').status_code == 401


@pytest.mark.parametrize('body,name', [(b'bad','bad.fit'),(sample_fit()[:-1],'broken.fit'),(sample_fit(),'x.txt')])
def test_reject_invalid_fit(web, body, name):
    c, _, _ = web
    assert c.post('/api/running/activities/import', data={'file':(io.BytesIO(body),name)}).status_code == 400
    assert c.get('/api/running/activities').json['activities'] == []
