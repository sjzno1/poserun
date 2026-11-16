import sys
from running.preview import create_app


def test_real_auth_static_and_blueprint_integration_without_models(tmp_path):
    app=create_app(tmp_path)
    c=app.test_client()
    assert c.get('/api/health').json['pose_inference'] is False
    for path in ['/','/activities.html','/garmin.html','/settings.html','/gait.html','/themes/theme-system.css','/themes/theme-system.js']:
        assert c.get(path).status_code==200
    assert c.get('/api/running/activities').status_code==401
    auth=app.extensions['poserun_auth']
    user,err=auth.register_user('runner_test','SYNTHETIC-test-password','测试跑者')
    assert err is None
    token=auth.create_token(user)
    headers={'Authorization':'Bearer '+token}
    assert c.get('/api/running/activities',headers=headers).json['activities']==[]
    assert c.put('/api/profile/theme',headers=headers,json={'theme':'fieldnote'}).status_code==200
    assert c.get('/api/profile/theme',headers=headers).json['theme']=='fieldnote'
    assert auth.delete_user(user['id']) is None
    assert c.get('/api/running/activities',headers=headers).status_code == 401
    assert not any(name in sys.modules for name in ['torch','mediapipe','pose_analyzer'])
