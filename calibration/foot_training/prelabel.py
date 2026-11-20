"""Offline image pre-labeler. Never called by the production application."""
import argparse
import hashlib
import json
from pathlib import Path
import time

from annotation import make_record, validate_dataset, write_review_page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--images', nargs='+', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--threshold', type=float, default=.5)
    args = parser.parse_args()
    # Explicit local paths only: no model downloads or network requests here.
    if not args.model.is_file() or any(not p.is_file() for p in args.images):
        raise ValueError('all model/image files must already exist locally')
    import cv2
    from rtmlib import RTMPose
    pose = RTMPose(str(args.model), model_input_size=(192, 256),
                   backend='opencv', device='cpu', to_openpose=False)
    model_hash = hashlib.sha256(args.model.read_bytes()).hexdigest()
    records, timings = [], []
    args.output.mkdir(parents=True, exist_ok=True)
    image_dir = args.output / 'images'
    image_dir.mkdir(exist_ok=True)
    for path in args.images:
        image = cv2.imread(str(path))
        if image is None:
            raise ValueError('cannot decode image: ' + str(path))
        start = time.perf_counter()
        # Each input is an explicitly selected single-person crop, not auto-tracking.
        points, scores = pose(image)
        timings.append(time.perf_counter() - start)
        if points.shape != (1, 133, 2) or scores.shape != (1, 133):
            raise ValueError('unexpected model output')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        saved_image = image_dir / (digest + path.suffix.lower())
        saved_image.write_bytes(path.read_bytes())
        record = make_record(saved_image, points[0], scores[0], threshold=args.threshold,
                             model_sha256=model_hash)
        record['source_path'] = str(path.resolve())
        record['proposal']['roi'] = 'explicitly supplied full-image person crop; no identity selection'
        records.append(record)
    validate_dataset(records)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'prelabels.json').write_text(json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False))
    write_review_page(records, args.output/'review.html')
    result = {'images': len(records), 'points_per_image': len(records[0]['points']),
              'model_sha256': model_hash, 'seconds_per_image': timings,
              'training_started': False, 'human_reviewed': 0,
              'notice': '单张裁剪图预标注运行验证；不是精度、接触事件或视频跟踪验收。'}
    (args.output/'run.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
