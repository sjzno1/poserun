"""Exercise real Flask routes in a copied-source, temporary SQLite sandbox.

The detector/keyframe extraction are replaced with saved candidate frames;
this verifies consumers/auth/report/history, not fresh model inference.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WORKER = r'''
import cProfile
import copy
import importlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
sandbox, source, output = map(Path, sys.argv[1:])
os.chdir(sandbox)
sys.path.insert(0, str(sandbox))
import pose_analyzer
# Keep cProfile's reference intact while allowing app's own profile module.
sys.modules.pop('profile', None)
import app as module
module.limiter.enabled = False
module.app.config['TESTING'] = True
client = module.app.test_client()
user = {'id': 1, 'username': os.environ['ADMIN_USERNAME'], 'role': 'admin'}
headers = {'Authorization': 'Bearer ' + module.auth_manager.create_token(user)}
results = []
frames = []
class ReplayAnalyzer:
    def process_video(self, path):
        return copy.deepcopy(frames)
    def close(self):
        pass
class NoKeyframes:
    def extract_key_frames(self, *args):
        return []
    def save_annotations(self, directory, *args):
        Path(directory).mkdir(parents=True, exist_ok=True)
        return []
def forbidden(*args, **kwargs):
    raise AssertionError('unscored request reached legacy inference or personalization')
with patch.object(module, 'PoseAnalyzer', ReplayAnalyzer), \
     patch.object(module, 'KeyFrameAnalyzer', NoKeyframes), \
     patch.object(module, '_video_quality', forbidden), \
     patch.object(module.profile_engine, 'generate_personalized_suggestions', forbidden):
    for person, count in [('kipchoge', 250), ('kiptum', 150)]:
        frames = json.loads((source/'calibration/results-candidate-stage2'/
                            (person+'-candidate-frames.json')).read_text())
        video = person+'.mp4'
        Path('uploads', video).write_bytes(b'replay placeholder: not a decoded input')
        for route in ['/api/analyze', '/api/analyze/advanced', '/api/batch']:
            request = {'video_files': [video]} if route == '/api/batch' else {'video_file': video}
            response = client.post(route, json=request, headers=headers)
            assert response.status_code == 200, (route, response.status_code, response.get_data(as_text=True))
            data = response.get_json()
            if route == '/api/batch':
                assert data['failed'] == 0 and data['successful'] == 1, data
                data = data['results'][0]
            assert data['score'] is None and data['grade'] is None, data
            assert data['measurement_status'] == 'unscored_2d_geometry_only'
            assert all(row['coverage'] is None for row in data['measurement_coverage'].values())
            assert data['training_plan']['weekly_plan'] == []
            assert data.get('gait') is None
            assert data.get('training_session', {}).get('weekly_plan', []) == []
            path = sandbox/'reports'/data['report_file']
            assert path.is_file()
            shutil_path = output/(person+'-'+route.rsplit('/',1)[-1]+'.pdf')
            shutil_path.write_bytes(path.read_bytes())
            results.append({'sample': person, 'frames': count, 'route': route,
                            'status': response.status_code, 'score': data['score']})
    # Legacy finite values and invalid candidate angles must not restore scoring.
    cases = [('legacy', [{'knee_flexion': 170, 'shin_angle': 90, 'body_leang': 5}]),
             ('missing', [{'measurement_status': 'unscored_2d_geometry_only'}]),
             ('nan', [{'measurement_status': 'unscored_2d_geometry_only', 'shin_angle': float('nan')}])]
    for label, frames in cases:
        response = client.post('/api/analyze', json={'video_file': 'kipchoge.mp4'}, headers=headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        data = response.get_json()
        assert data['score'] is None and data['metrics']['avg_shin_angle'] is None
        json.loads(response.get_data(as_text=True), parse_constant=lambda x: (_ for _ in ()).throw(AssertionError(x)))
        results.append({'case': label, 'status': response.status_code, 'score': None})
    # Verify the exact persisted target, then exercise the history->training route.
    records = module.history_store.list_records()
    assert len(records) == len(results), (len(records), len(results))
    for record in records:
        assert record['score'] is None and record['grade'] is None
        assert record['metrics']['gait'] is None
        response = client.post('/api/training/session', json={'record_id': record['id']}, headers=headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.get_json()['session']['weekly_plan'] == []
    response = client.get('/api/trend', headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    # Check the real Flask provider separately (nested NaN/Inf -> strict null).
    wire = module.app.json.dumps({'bad': [float('nan'), float('inf')], 'zero': 0})
    assert json.loads(wire) == {'bad': [None, None], 'zero': 0}
    assert 'NaN' not in wire and 'Infinity' not in wire
(output/'api-verification.json').write_text(json.dumps({
    'requests': results, 'persisted_records': len(records),
    'history_training_requests': len(records), 'trend_status': response.status_code,
    'detector': 'saved-frame replay; no fresh inference',
}, ensure_ascii=False, indent=2))
print(json.dumps({'analysis_requests': len(results), 'persisted_records': len(records),
                  'history_training_requests': len(records), 'trend_status': response.status_code}))
'''


def main():
    output = ROOT/'calibration/results-production-stage4'
    output.mkdir(exist_ok=True)
    scratch = Path(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix='poserun-source-sandbox-', dir=scratch) as directory:
        sandbox = Path(directory)
        for source in ROOT.glob('*.py'):
            shutil.copy2(source, sandbox/source.name)
        env = dict(os.environ, ADMIN_USERNAME='offline-test-admin', ADMIN_PASSWORD=os.urandom(24).hex(),
                   AUTH_SECRET=os.urandom(32).hex(), AUTH_DB_PATH=str(sandbox/'data/auth.db'))
        result = subprocess.run([sys.executable, '-c', WORKER, str(sandbox), str(ROOT), str(output)],
                                cwd=scratch, env=env, capture_output=True, text=True, timeout=120)
        (output/'api-test.log').write_text(result.stdout+result.stderr)
        print(result.stdout)
        if result.returncode:
            print(result.stderr)
            raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
