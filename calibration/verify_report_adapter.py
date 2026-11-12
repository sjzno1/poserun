"""Reproduce offline stage-3 tests, sample API roundtrips and report exports."""
import tempfile
import json
import re
import subprocess
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from calibration.report_adapter import build_payload, export_reports, create_preview_app


def main():
    out = ROOT/'calibration/results-report-stage3'
    out.mkdir(exist_ok=True)
    checks = {}
    # Avoid project profile.py shadowing stdlib profile while importing torch.
    scratch = Path(tempfile.gettempdir())
    for name, folder, pattern in [('base', 'calibration', 'test_candidate.py'),
                                  ('extended', 'calibration/tests', 'test_*.py')]:
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s',
                                 str(ROOT/folder), '-p', pattern, '-v'],
                                cwd=scratch, capture_output=True, text=True)
        (out/(name+'-tests.log')).write_text(result.stdout+result.stderr)
        count = re.search(r'Ran (\d+) tests?', result.stderr)
        checks[name] = {'returncode': result.returncode,
                        'tests': int(count.group(1)) if count else None}
        if result.returncode:
            print(result.stdout+result.stderr)
            raise SystemExit(result.returncode)
    checks['total_tests'] = sum(item['tests'] for item in checks.values())
    checks['real_samples'] = {}
    client = create_preview_app().test_client()
    for person, expected in [('kipchoge', 250), ('kiptum', 150)]:
        frames = json.loads((ROOT/'calibration/results-candidate-stage2'/
                             (person+'-candidate-frames.json')).read_text())
        payload = build_payload(frames, expected_frames=expected)
        response = client.post('/offline/report-preview',
                               json={'frames': frames, 'expected_frames': expected})
        assert response.status_code == 200 and response.get_json() == payload
        paths = export_reports(payload, out/person)
        checks['real_samples'][person] = {'frames': len(frames), 'api_status': response.status_code,
                                         'paths': paths, 'metrics': payload['metrics']}
        import fitz
        doc = fitz.open(paths['pdf'])
        text = ''.join(page.get_text() for page in doc)
        assert '未知' in text and '触地步频' in text and '基本正常' not in text
        doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).save(str(out/(person+'-preview.png')))
        doc.close()
    (out/'verification.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps({'total_tests': checks['total_tests'], 'samples': {
        name: {'frames': row['frames'], 'api_status': row['api_status']}
        for name, row in checks['real_samples'].items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
