"""Reproduce stage-6 safety, upload UI and fresh seeded/unseeded video tests."""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT/'calibration/results-target-stage6'
    output.mkdir(exist_ok=True)
    commands = [
        ('python-regression', [sys.executable, 'calibration/verify_report_adapter.py']),
        ('replay-api-regression', [sys.executable, 'calibration/verify_production_safety.py']),
        ('results-ui', ['node', 'calibration/tests/test_results_ui.js']),
        ('trend-ui', ['node', 'calibration/tests/test_trend_ui.js']),
        ('upload-ui', ['node', 'calibration/tests/test_upload_target_ui.js']),
        ('fresh-unseeded-video', [sys.executable, 'calibration/verify_video_pipeline.py',
                                  '--output', 'calibration/results-unseeded-stage6']),
        ('fresh-selected-video', [sys.executable, 'calibration/verify_video_pipeline.py',
                                  '--targets', 'calibration/target_seeds_stage6.json',
                                  '--output', 'calibration/results-target-stage6']),
        ('target-api-replay', [sys.executable, 'calibration/verify_target_api.py']),
        ('report-exports', [sys.executable, 'calibration/verify_target_reports.py']),
    ]
    checks = {}
    for name, command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=480)
        (output/(name+'.log')).write_text(result.stdout+result.stderr)
        checks[name] = {'exit_code': result.returncode}
        if result.returncode:
            print(result.stdout+result.stderr)
            raise SystemExit(result.returncode)
        if name.endswith('-ui'):
            match = re.search(r'(\d+) tests passed', result.stdout) or re.search(r'^# tests (\d+)$', result.stdout, re.M)
            assert match, (name, result.stdout)
            checks[name]['tests'] = int(match.group(1))
        print(name, 'OK', flush=True)
    python = json.loads((ROOT/'calibration/results-report-stage3/verification.json').read_text())
    replay = json.loads((ROOT/'calibration/results-production-stage4/api-verification.json').read_text())
    target_api = json.loads((output/'target-api-verification.json').read_text())
    selected = json.loads((output/'real-video-verification.json').read_text())
    unseeded = json.loads((ROOT/'calibration/results-unseeded-stage6/real-video-verification.json').read_text())
    for rows in (selected, unseeded):
        assert len(rows) == 2 and len({row['sample'] for row in rows}) == 2
        assert all(row['fresh_inference'] and row['http_status'] == 200 and row['score'] is None for row in rows)
    comparison = {}
    for row in selected:
        old = next(item for item in unseeded if item['sample'] == row['sample'])
        assert row['model_sha256'] == old['model_sha256'] and row['video_sha256'] == old['video_sha256']
        timeline = row['video_timeline']
        tracking = timeline['target_tracking']
        assert tracking['identity_verified'] is False and tracking['automatic_reacquisition'] is False
        comparison[row['sample']] = {
            'decoded_frames': timeline['decoded_frames'],
            'selected_associated_frames': tracking['associated_frames'],
            'selected_coverage_percent': round(100*row['metrics']['knee_flexion']['coverage'], 2),
            'first_lost_frame': tracking['first_lost_frame'],
            'lost_reason': tracking['lost_reason'],
            'unseeded_pose_frames': old['video_timeline']['pose_detected_frames'],
            'identity_verified': False,
        }
    summary = {
        'checks': checks,
        'python_tests': python['total_tests'],
        'js_dom_stub_tests': sum(item.get('tests', 0) for item in checks.values()),
        'consumer_replay_requests': len(replay['requests'])+replay['history_training_requests']+1,
        'target_replay_valid_requests': len(target_api['valid_requests']),
        'target_replay_invalid_requests': len(target_api['invalid_requests']),
        'fresh_model_flask_requests': len(selected)+len(unseeded),
        'distinct_source_video_frames': sum(row['video_timeline']['decoded_frames'] for row in selected),
        'decoded_frames_all_fresh_requests': sum(row['video_timeline']['decoded_frames'] for row in selected+unseeded),
        'samples': comparison,
        'deployed': False,
        'usable_continuous_multi_person_analysis': False,
        'limitations': 'Seed rectangles are AI visual choices, not expert identity ground truth. Early ambiguous overlapping detections stop permanently; no threshold relaxation, scoring, automatic identity reassignment or accuracy claim.',
    }
    (output/'verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
