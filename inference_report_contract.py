"""Fresh real inference + generated non-human video + unscored report contract.
No supplied private footage; this is NOT human accuracy/identity validation.
Run only in the NAS isolated container.
"""
import json
from pathlib import Path
import tempfile
import zipfile
import cv2
import numpy as np
from calibrated_analyzer import CalibratedPoseAnalyzer
from measurement_safety import safe_metrics
from measurement_report import export_reports

if True:
    tmp='/scratch/fresh-contract'
    Path(tmp).mkdir(exist_ok=True)
    root=Path(tmp)
    path=root/'generated-no-human.avi'
    writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),10,(64,64))
    if not writer.isOpened():raise RuntimeError('VideoWriter unavailable')
    try:
        for _ in range(6):writer.write(np.zeros((64,64,3),dtype=np.uint8))
    finally:writer.release()
    a=CalibratedPoseAnalyzer()
    try:rows=a.process_video(str(path))
    finally:a.close()
    assert len(rows)==6
    assert [r['frame_index'] for r in rows]==list(range(6))
    assert all(r['pose_detected'] is False for r in rows)
    metrics=safe_metrics(rows)
    payload=metrics['measurement_payload']
    assert metrics['total'] is None and metrics['grade'] is None
    assert payload['metrics']['ground_contact_time']['value'] is None
    assert payload['metrics']['foot_contact']['value'] is None
    assert payload['expected_frames']==6
    outputs=export_reports(payload,root/'report',user_info={'name':'隔离无人合成输入','coach':'软件契约验证'})
    assert json.loads(Path(outputs['json']).read_text())['score'] is None
    assert '未知' in Path(outputs['html']).read_text()
    with zipfile.ZipFile(outputs['docx']) as z:
        xml=z.read('word/document.xml').decode()
        assert '未知' in xml
    print('INFERENCE_PASS='+json.dumps({'frames':len(rows),'fresh_model_inference':True,'human_accuracy_verified':False}),flush=True)
    assert Path(outputs['pdf']).read_bytes().startswith(b'%PDF-')
    Path('/scratch/report-outputs.json').write_text(json.dumps(outputs))
    print('REPORT_EXPORT_PASS='+json.dumps(outputs),flush=True)
