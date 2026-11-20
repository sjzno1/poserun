"""Local-only foot annotation contract. Model proposals are never ground truth."""
import argparse
import base64
import hashlib
import html
import json
import math
from pathlib import Path

POINTS = {
    'left_knee': 13, 'right_knee': 14, 'left_ankle': 15, 'right_ankle': 16,
    'left_big_toe': 17, 'left_small_toe': 18, 'left_heel': 19,
    'right_big_toe': 20, 'right_small_toe': 21, 'right_heel': 22,
}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def make_record(path, points, scores, *, threshold=.5, model_sha256):
    from PIL import Image
    path = Path(path).resolve()
    with Image.open(path) as im:
        width, height = im.size
    if len(points) != 133 or len(scores) != 133:
        raise ValueError('expected COCO-WholeBody 133 points, not Halpe26/OpenPose')
    if not number(threshold) or not 0 <= threshold <= 1:
        raise ValueError('invalid display threshold')
    labels = {}
    for name, index in POINTS.items():
        x, y = map(float, points[index])
        score = float(scores[index])
        valid = all(number(v) for v in (x, y, score)) and 0 <= x < width and 0 <= y < height and score >= threshold
        labels[name] = {'x': x if valid else None, 'y': y if valid else None,
                        'visibility': 2 if valid else 0,
                        'proposal_score': score if number(score) else None}
    return {'id': hashlib.sha256(path.read_bytes()).hexdigest(), 'image_path': str(path),
            'width': width, 'height': height, 'kind': 'isolated_image',
            'subject_id': None, 'session_id': None, 'split': None,
            'reviewed': False, 'reviewer': None,
            'rights': {'training_allowed': False, 'basis': None},
            'proposal': {'model_sha256': model_sha256, 'format': 'coco_wholebody_133',
                         'notice': '模型置信度不是定位准确率；阈值仅控制显示。'},
            'points': labels, 'contact': {'left': 'unknown', 'right': 'unknown'},
            'events': [], 'time_scale_verified': False}


def validate_record(record, *, for_training=False):
    if type(record.get('width')) is not int or record['width'] <= 0 or type(record.get('height')) is not int or record['height'] <= 0:
        raise ValueError('invalid image dimensions')
    if not isinstance(record.get('id'), str) or len(record['id']) != 64 or any(c not in '0123456789abcdef' for c in record['id']):
        raise ValueError('invalid SHA256 source identifier')
    if set(record.get('points', {})) != set(POINTS):
        raise ValueError('wrong landmark names')
    for point in record['points'].values():
        v = point.get('visibility')
        if type(v) is not int or v not in (0, 1, 2):
            raise ValueError('invalid visibility')
        x, y = point.get('x'), point.get('y')
        if v == 0:
            if x is not None or y is not None:
                raise ValueError('unknown points must have null coordinates')
        elif not number(x) or not number(y) or not 0 <= x < record['width'] or not 0 <= y < record['height']:
            raise ValueError('point outside image or nonfinite')
    if record.get('contact') != {'left': 'unknown', 'right': 'unknown'} or record.get('events') != []:
        # This first stage accepts still images only, not temporal event labels.
        raise ValueError('isolated images cannot establish contact events')
    if record.get('kind') != 'isolated_image':
        raise ValueError('video event schema not yet admitted')
    if for_training:
        rights = record.get('rights', {})
        if rights.get('training_allowed') is not True or not isinstance(rights.get('basis'), str) or not rights['basis'].strip():
            raise ValueError('training rights not cleared')
        if record.get('reviewed') is not True or not isinstance(record.get('reviewer'), str) or not record['reviewer'].strip():
            raise ValueError('unreviewed model proposals cannot train')
        if any(not isinstance(record.get(k), str) or not record[k].strip() for k in ('subject_id', 'session_id')):
            raise ValueError('subject/session required for leakage control')
        if record.get('split') not in ('train', 'validation', 'test'):
            raise ValueError('explicit split required')
        if not any(p['visibility'] > 0 for p in record['points'].values()):
            raise ValueError('no annotated keypoints')
    return True


def validate_dataset(records, *, for_training=False):
    seen, subjects, sessions = set(), {}, {}
    if not records:
        raise ValueError('empty annotation set')
    for record in records:
        validate_record(record, for_training=for_training)
        if record['id'] in seen:
            raise ValueError('duplicate image content')
        seen.add(record['id'])
        if for_training:
            for mapping, field in ((subjects, 'subject_id'), (sessions, 'session_id')):
                group = record[field]
                if group in mapping and mapping[group] != record['split']:
                    raise ValueError('subject/session leakage across splits')
                mapping[group] = record['split']
    return {'records': len(records), 'training_admissible': for_training,
            'note': '通过数据契约不代表已训练、测量准确或部署。'}


def write_review_page(records, output):
    """Self-contained static HTML; no server, CDN, upload or telemetry."""
    validate_dataset(records)
    embedded = []
    for record in records:
        row = dict(record)
        path = Path(row['image_path'])
        mime = 'image/png' if path.suffix.lower() == '.png' else 'image/jpeg'
        row['image_data'] = 'data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode()
        embedded.append(row)
    template = Path(__file__).with_name('review.html').read_text(encoding='utf-8')
    # JSON in a script element must not terminate that element.
    data = json.dumps(embedded, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c')
    Path(output).write_text(template.replace('__RECORDS__', data), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('annotations', type=Path)
    parser.add_argument('--for-training', action='store_true')
    args = parser.parse_args()
    print(json.dumps(validate_dataset(json.loads(args.annotations.read_text()),
                                     for_training=args.for_training), ensure_ascii=False))


if __name__ == '__main__':
    main()
