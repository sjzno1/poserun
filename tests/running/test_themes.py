from flask import Flask, g
from running.themes import register_themes


def test_theme_default_persistence_isolation_and_validation(tmp_path):
    app = Flask(__name__)
    register_themes(app, str(tmp_path / 'profiles.db'))
    uid = [1]
    @app.before_request
    def user():
        g.current_user = {'id': uid[0]}
    client = app.test_client()
    assert client.get('/api/profile/theme').json['theme'] == 'clarion'
    assert client.put('/api/profile/theme', json={'theme': 'nightlab'}).status_code == 200
    assert client.get('/api/profile/theme').json['theme'] == 'nightlab'
    uid[0] = 2
    assert client.get('/api/profile/theme').json['theme'] == 'clarion'
    assert client.put('/api/profile/theme', json={'theme': 'invalid'}).status_code == 400
    uid[0] = 1
    assert client.get('/api/profile/theme').json['theme'] == 'nightlab'
