import subprocess
import sys
from pathlib import Path
from running.preview import create_app

ROOT = Path(__file__).resolve().parents[2]


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
    # Verify inference-import isolation in a clean subprocess: under joint pytest
    # collection other suites import torch/pose_analyzer into sys.modules, which
    # would make an in-process check false-positive or false-negative.
    check = (
        "import sys;"
        "from running.preview import create_app;"
        "create_app();"
        "leaked=[m for m in ('torch','mediapipe','pose_analyzer') if m in sys.modules];"
        "sys.stderr.write(str(leaked));"
        "sys.exit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, '-c', check], cwd=str(ROOT),
                       capture_output=True, text=True)
    assert r.returncode == 0, f"preview leaked inference imports: {r.stderr}"
