"""Real videos -> real YOLO -> real Flask/report/temporary history, no replay.

Copies source and private input videos to scratch; existing local model is
linked read-only by convention. Does not deploy, open a port, download models or write live DBs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WORKER = r'''
import collections
import hashlib
import json
from pathlib import Path
import sys
import time
import shutil
import app as module
sandbox, source, output = map(Path, sys.argv[1:4])
targets = json.loads(Path(sys.argv[4]).read_text()) if len(sys.argv) > 4 else {}
module.limiter.enabled = False
module.app.config['TESTING'] = True
client = module.app.test_client()
user = {'id': 1, 'username': 'video-test-admin', 'role': 'admin'}
headers = {'Authorization': 'Bearer ' + module.auth_manager.create_token(user)}
RealAnalyzer = module.PoseAnalyzer
observations = []
class RecordingAnalyzer(RealAnalyzer):
    def process_video(self, path):
        start = time.perf_counter()
        rows = super().process_video(path)  # Fresh, real model inference; never replaced.
        elapsed = time.perf_counter()-start
        observations.append({'backend': self.backend, 'rows': rows,
                             'timeline': self.last_video_metadata, 'elapsed_sec': elapsed})
        return rows
module.PoseAnalyzer = RecordingAnalyzer
summaries = []
for name, filename, route in [('kipchoge', 'kipchoge_side_14s.mp4', '/api/analyze'),
                               ('kiptum', 'kiptum_side_15s.mp4', '/api/analyze/advanced')]:
    video = source/'calibration/private_samples'/filename
    shutil.copyfile(video,sandbox/'uploads'/filename)
    request_data = {'video_file': filename}
    if name in targets:
        request_data['target_selection'] = targets[name]['selection']
    response = client.post(route, json=request_data, headers=headers)
    assert response.status_code == 200, (response.status_code, response.get_data(as_text=True))
    data = response.get_json()
    actual = observations[-1]
    rows, timeline = actual['rows'], actual['timeline']
    assert actual['backend'] == 'yolo_geometry_unscored'
    assert timeline['decoded_frames'] == len(rows) and timeline['count_matches'] is True
    assert [r['frame_index'] for r in rows] == list(range(len(rows)))
    assert data['video_timeline'] == timeline
    if name in targets:
        tracking = timeline['target_tracking']
        assert tracking['selection'] == targets[name]['selection']
        assert tracking['identity_verified'] is False and tracking['automatic_reacquisition'] is False
        lost_at = tracking['first_lost_frame']
        if lost_at is not None:
            assert all(not row['pose_detected'] for row in rows[lost_at:])
    assert data['score'] is None and data['grade'] is None
    assert data['gait'] is None and data['training_plan']['weekly_plan'] == []
    any_geometry = any(m['value'] is not None for m in data['measurement_coverage'].values())
    assert data['measurement_status'] == ('unscored_2d_geometry_only' if any_geometry else 'insufficient_evidence')
    for key in ['knee_flexion', 'shin_angle', 'body_leang']:
        m = data['measurement_coverage'][key]
        count = sum(isinstance(r.get(key), (int, float)) for r in rows)
        assert m['valid_frames'] == count and m['expected_frames'] == len(rows)
        assert m['coverage'] == count/len(rows)
    for row in rows:
        assert row['score'] is None and row['running_phase'] is None
        if not row['pose_detected']:
            assert all(row[k] is None for k in ['knee_flexion','shin_angle','body_leang'])
    rec = module.history_store.get_record(data['history_record'])
    assert rec['score'] is None and rec['grade'] is None
    assert rec['metrics']['video_timeline'] == timeline
    assert rec['metrics']['gait'] is None
    report = sandbox/'reports'/data['report_file']
    shutil_report = output/(name+'-actual-video.pdf')
    shutil_report.write_bytes(report.read_bytes())
    import fitz
    with fitz.open(shutil_report) as pdf:
        text = ''.join(page.get_text() for page in pdf)
        assert '覆盖率' in text and '未验证' in text
        pdf[0].get_pixmap(matrix=fitz.Matrix(1.5,1.5)).save(str(output/(name+'-preview.png')))
    def serial(row):
        return {key: [vars(lm) for lm in value] if key == 'landmarks' else value
                for key, value in row.items()}
    (output/(name+'-frames.json')).write_text(json.dumps([serial(r) for r in rows], ensure_ascii=False, allow_nan=False))
    (output/(name+'-api.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
    record = {'sample': name, 'video_sha256': hashlib.sha256(video.read_bytes()).hexdigest(),
              'model_sha256': hashlib.sha256((sandbox/'data/models/yolo11n-pose.pt').read_bytes()).hexdigest(),
              'http_status': response.status_code, 'backend': actual['backend'], 'fresh_inference': True,
              'elapsed_sec': actual['elapsed_sec'], 'video_timeline': timeline,
              'detection_status_counts': dict(collections.Counter(r['detection_status'] for r in rows)),
              'metrics': data['measurement_coverage'], 'report': str(shutil_report), 'score': data['score']}
    summaries.append(record)
    (output/'real-video-verification.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps(record, ensure_ascii=False), flush=True)
assert len(summaries) == 2
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--targets', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'calibration/results-video-stage5')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(exist_ok=True)
    scratch = Path(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix='poserun-video-sandbox-', dir=scratch) as directory:
        sandbox = Path(directory)
        for file in ROOT.glob('*.py'):
            shutil.copy2(file, sandbox/file.name)
        (sandbox/'data/models').mkdir(parents=True)
        (sandbox/'data/models/yolo11n-pose.pt').symlink_to(ROOT/'data/models/yolo11n-pose.pt')
        env = dict(os.environ, ADMIN_USERNAME='video-test-admin', ADMIN_PASSWORD=os.urandom(24).hex(),
                   AUTH_SECRET=os.urandom(32).hex(), AUTH_DB_PATH=str(sandbox/'data/auth.db'),
                   OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
        command = [sys.executable, '-c', WORKER, str(sandbox), str(ROOT), str(output)]
        if args.targets:
            command.append(str(args.targets.resolve()))
        result = subprocess.run(command,
                                cwd=sandbox, env=env, capture_output=True, text=True, timeout=420)
        (output/'real-video-test.log').write_text(result.stdout+result.stderr)
        print(result.stdout)
        if result.returncode:
            print(result.stderr)
            raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
