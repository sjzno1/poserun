"""Run all stage-5 regressions and both fresh real-video API requests."""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    out = ROOT/'calibration/results-video-stage5'
    out.mkdir(exist_ok=True)
    commands = [
        ('python-regression', [sys.executable, 'calibration/verify_report_adapter.py']),
        ('replay-api-regression', [sys.executable, 'calibration/verify_production_safety.py']),
        ('results-ui', ['node', 'calibration/tests/test_results_ui.js']),
        ('trend-ui', ['node', 'calibration/tests/test_trend_ui.js']),
        ('fresh-real-video', [sys.executable, 'calibration/verify_video_pipeline.py']),
    ]
    results = {}
    for name, command in commands:
        r = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=480)
        (out/(name+'.log')).write_text(r.stdout+r.stderr)
        results[name] = {'exit_code': r.returncode}
        if r.returncode:
            print(r.stdout+r.stderr)
            raise SystemExit(r.returncode)
        if name.endswith('-ui'):
            results[name]['tests'] = int(re.search(r'(\d+) tests passed', r.stdout).group(1))
        print(name, 'OK', flush=True)
    tests = json.loads((ROOT/'calibration/results-report-stage3/verification.json').read_text())
    replay = json.loads((ROOT/'calibration/results-production-stage4/api-verification.json').read_text())
    real = json.loads((out/'real-video-verification.json').read_text())
    assert len(real) == 2 and len({row['sample'] for row in real}) == 2
    assert all(row['fresh_inference'] and row['http_status'] == 200 for row in real)
    results.update(
        python_tests=tests['total_tests'],
        js_tests=sum(row.get('tests', 0) for row in results.values()),
        replay_flask_requests=len(replay['requests'])+replay['history_training_requests']+1,
        fresh_video_flask_requests=len(real),
        fresh_video_decoded_frames=sum(row['video_timeline']['decoded_frames'] for row in real),
        samples={row['sample']: {
            'decoded_frames': row['video_timeline']['decoded_frames'],
            'pose_detected_frames': row['video_timeline']['pose_detected_frames'],
            'knee_valid_frames': row['metrics']['knee_flexion']['valid_frames'],
            'knee_coverage_percent': round(row['metrics']['knee_flexion']['coverage']*100, 1),
            'rejected_multiple_people': row['detection_status_counts'].get('rejected_multiple_people', 0),
        } for row in real},
        deployed=False,
        limitations='Single detected person is not verified identity. Multi-person target tracking, contact/gait truth and scoring remain unvalidated.',
    )
    (out/'verification.json').write_text(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
