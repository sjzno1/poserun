"""Fresh offline stage-7 evidence run; does not enable/modify live tracking."""
import collections
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def source_hashes():
    names=('app.py','target_tracker.py','calibrated_analyzer.py','video_pipeline.py','measurement_safety.py')
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}


def main():
    out=ROOT/'calibration/results-overlap-stage7';out.mkdir(exist_ok=True)
    before=source_hashes()
    for name,script in [('regression','verify_report_adapter.py'),('fresh-detection-evidence','collect_overlap_evidence.py'),('local-review-pack','build_overlap_review.py')]:
        r=subprocess.run([sys.executable,str(ROOT/'calibration'/script)],cwd=ROOT,text=True,capture_output=True,timeout=480)
        (out/(name+'.log')).write_text(r.stdout+r.stderr)
        if r.returncode:
            print(r.stdout+r.stderr);raise SystemExit(r.returncode)
        print(name,'OK',flush=True)
    assert source_hashes()==before,'Offline diagnostics changed live source files'
    rows=json.loads((out/'summary.json').read_text())
    failures=json.loads((out/'failure-pair-evidence.json').read_text())
    cases=json.loads((out/'review-cases.json').read_text())
    tests=json.loads((ROOT/'calibration/results-report-stage3/verification.json').read_text())
    counts=collections.Counter()
    for row in rows:counts.update(row['screen_counts'])
    assert len(rows)==len({r['sample'] for r in rows})==2
    assert len(cases)==len({c['case_id'] for c in cases})
    assert all(c['identity_relation']=='unknown' and c['review_label']=='unknown' for c in cases)
    assert all((out/p).is_file() for c in cases for p in c['images'])
    report={
        'python_tests':tests['total_tests'],
        'decoded_frames':sum(r['decoded_frames'] for r in rows),
        'detections':sum(r['detection_count'] for r in rows),
        'overlap_pairs':sum(r['overlap_pairs'] for r in rows),
        'screen_counts':dict(counts),'review_cases':len(cases),
        'review_images':sum(len(c['images']) for c in cases),
        'expert_labels':0,'production_source_hashes_unchanged':before,
        'production_rules_changed':False,'deployed':False,
        'failure_details':{name:{
            'frame_index':r['frame_index'],
            'joint_max_distance_percent_of_min_box_diagonal':round(r['screen']['max_normalized_distance']*100,2),
            'max_disagreement_joint_index':max(r['screen']['joint_distances'],key=lambda x:x['distance_over_min_box_diagonal'] if x['distance_over_min_box_diagonal'] is not None else -1)['joint_index'],
            'detector_confidences':r['detector_confidences'],
        } for name,r in failures.items()},
        'limitations':'Discovery only, no independent identity/duplicate truth. No automatic merge or accuracy claim. Real browser checks are recorded separately, not rerun by this script.',
    }
    (out/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
