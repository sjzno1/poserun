"""Real Flask target request validation/routing using saved stage-6 frame fixtures.

This is a replay consumer test, not new model inference. All DBs/reports are
created in a temporary copied-source sandbox, never the production data dir.
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
import copy
import json
from pathlib import Path
import sys
import app as module
source, output = map(Path, sys.argv[1:])
module.app.config['TESTING'] = True
module.limiter.enabled = False
client = module.app.test_client()
headers = {'Authorization': 'Bearer '+module.auth_manager.create_token({
    'id': 1, 'username': 'target-test-admin', 'role': 'admin'})}
seeds = json.loads((source/'calibration/target_seeds_stage6.json').read_text())
configured, constructed = [], []
class ReplayAnalyzer:
    def __init__(self): constructed.append(True)
    def configure_target(self, selection): configured.append(copy.deepcopy(selection))
    def process_video(self, path):
        name = 'kipchoge' if 'kipchoge' in str(path) else 'kiptum'
        return json.loads((source/'calibration/results-target-stage6'/(name+'-frames.json')).read_text())
    def close(self): pass
module.PoseAnalyzer = ReplayAnalyzer
for name in ('kipchoge', 'kiptum'):
    Path('uploads', name+'.mp4').write_bytes(b'consumer test replay placeholder')
valid = []
for route, name in [('/api/analyze', 'kipchoge'), ('/api/analyze/advanced', 'kiptum')]:
    response = client.post(route, json={'video_file': name+'.mp4', 'target_selection': seeds[name]['selection']}, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()
    assert configured[-1] == seeds[name]['selection']
    assert data['video_timeline']['target_tracking']['selection'] == seeds[name]['selection']
    assert data['score'] is None and data['grade'] is None
    rec = module.history_store.get_record(data['history_record'])
    assert rec['metrics']['video_timeline']['target_tracking']['selection'] == seeds[name]['selection']
    valid.append({'route': route, 'status': response.status_code})
response = client.post('/api/batch', json={
    'video_files': ['kipchoge.mp4', 'kiptum.mp4'],
    'target_selections': {name+'.mp4': seeds[name]['selection'] for name in ('kipchoge', 'kiptum')}}, headers=headers)
assert response.status_code == 200, response.get_data(as_text=True)
data = response.get_json()
assert data['successful'] == 2 and data['failed'] == 0, data
assert configured[-2:] == [seeds[name]['selection'] for name in ('kipchoge', 'kiptum')]
assert len({row['report_file'] for row in data['results']}) == 2
valid.append({'route': '/api/batch', 'status': response.status_code, 'files': 2})
invalid = []
bad_selections = [
    {'frame_index': '0', 'box': [0,0,1,1]}, {'frame_index': False, 'box': [0,0,1,1]},
    {'frame_index': 1, 'box': [0,0,1,1]}, {'frame_index': 0, 'box': [.8,0,.2,1]},
    {'frame_index': 0, 'box': [0,0,.001,1]}, {'frame_index': 0, 'box': [0,0,'1',1]},
    {'frame_index': 0, 'box': [0,0,None,1]}, {'frame_index': 0, 'box': [0,0,1,1], 'identity_verified': True}]
count_before = len(constructed)
for route in ('/api/analyze','/api/analyze/advanced'):
    for seed in bad_selections:
        response = client.post(route, json={'video_file': 'kipchoge.mp4','target_selection': seed}, headers=headers)
        assert response.status_code == 400, (route, response.status_code)
        invalid.append({'route': route, 'status': response.status_code})
    response = client.post(route, json={'video_file': 'kipchoge.mp4','target_selections': {}}, headers=headers)
    assert response.status_code == 400
    invalid.append({'route': route, 'status': response.status_code})
for data in [
    {'video_files': ['kipchoge.mp4'], 'target_selection': seeds['kipchoge']['selection']},
    {'video_files': ['kipchoge.mp4'], 'target_selections': {'wrong.mp4': seeds['kipchoge']['selection']}},
    {'video_files': ['kipchoge.mp4'], 'target_selections': {'kipchoge.mp4': bad_selections[0]}},
    {'video_files': 'kipchoge.mp4'},
]:
    response = client.post('/api/batch', json=data, headers=headers)
    assert response.status_code == 400, response.get_data(as_text=True)
    invalid.append({'route': '/api/batch', 'status': response.status_code})
assert len(constructed) == count_before, 'invalid target request initialized an expensive model'
records = module.history_store.list_records()
assert len(records) == 4 and all(row['score'] is None for row in records)
summary = {'mode': 'saved stage-6 fixture replay; real Flask/temporary history/report consumers',
           'valid_requests': valid, 'invalid_requests': invalid, 'model_constructed_for_invalid': False,
           'persisted_records': len(records)}
(output/'target-api-verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps({'valid_requests':len(valid),'invalid_requests':len(invalid),'persisted_records':len(records)}))
'''


def main():
    output = ROOT/'calibration/results-target-stage6'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='poserun-target-api-', dir=Path(tempfile.gettempdir())) as directory:
        sandbox = Path(directory)
        for file in ROOT.glob('*.py'):
            shutil.copy2(file, sandbox/file.name)
        env = dict(os.environ, ADMIN_USERNAME='target-test-admin', ADMIN_PASSWORD=os.urandom(24).hex(),
                   AUTH_SECRET=os.urandom(32).hex(), AUTH_DB_PATH=str(sandbox/'data/auth.db'))
        r = subprocess.run([sys.executable, '-c', WORKER, str(ROOT), str(output)],
                           cwd=sandbox, env=env, text=True, capture_output=True, timeout=120)
        (output/'target-api.log').write_text(r.stdout+r.stderr)
        print(r.stdout)
        if r.returncode:
            print(r.stderr)
            raise SystemExit(r.returncode)


if __name__ == '__main__':
    main()
