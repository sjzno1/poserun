"""Bounded public resource download, retaining provenance and content hashes."""
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'offline-foot-training'
ROOT.mkdir(parents=True, exist_ok=True)
SOURCES = {
 'rtmpose_s_wholebody.zip': 'https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/rtmpose-s_simcc-ucoco_dw-ucoco_270e-256x192-3fd922c8_20230728.zip',
 'cmu_foot_val.zip': 'https://posefs1.perception.cs.cmu.edu/OpenPose/datasets/foot/person_keypoints_val2017_foot_v1.zip',
}
records = []
for name, url in SOURCES.items():
    record = {'url': url, 'purpose': 'offline research evaluation only; not training clearance'}
    path = ROOT / name
    try:
        if not path.exists():
            with urllib.request.urlopen(url, timeout=45) as response, path.with_suffix('.part').open('wb') as out:
                n = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > 128 * 1024 * 1024:
                        raise ValueError('resource exceeds 128MiB limit')
                    out.write(chunk)
            path.with_suffix('.part').rename(path)
        record.update(path=str(path), bytes=path.stat().st_size,
                      sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if sum(x.file_size for x in members) > 256 * 1024 * 1024:
                raise ValueError('expanded archive too large')
            record['members'] = [x.filename for x in members]
            for item in members:
                target = (ROOT / name.replace('.zip', '') / item.filename).resolve()
                if not target.is_relative_to((ROOT / name.replace('.zip', '')).resolve()):
                    raise ValueError('unsafe archive path')
            archive.extractall(ROOT / name.replace('.zip', ''))
        record['status'] = 'downloaded'
    except Exception as exc:
        record.update(status='blocked', error=str(exc))
    records.append(record)
    print(json.dumps(record, ensure_ascii=False))
(ROOT/'download-manifest.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
