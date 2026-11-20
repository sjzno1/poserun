"""Decode the full available timeline; preserve pose failures as unknown rows.

OpenCV EOF and decode failure both return False. Container frame count is a
hint, not proof of file integrity; a count mismatch invalidates full coverage.
"""
import math
import os
import time
from compute_budget import Budget, current_budget
from collections import Counter
import cv2
from single_person import SinglePersonVideoRequired, SCOPE, PRIMARY_SUBJECT_FAILURE_STATUSES


def positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _process_video_remote(analyzer, path, provider):
    """Offload path: NAS local decode -> lossless compressed frame batches ->
    pinned remote YOLO -> keypoints back -> NAS local selection/smoothing/measurement.

    Two decode passes over the same file: pass 1 decodes+uploads (counting frames),
    pass 2 re-decodes, verifies each frame is bit-identical (sha256) to what was
    uploaded, then runs local primary-subject selection + analyze_frame with the
    returned landmarks as override. Selection/smoothing/measurement stay local and
    see contiguous frame indices exactly as the single-pass local path. The output
    row/metadata schema is identical to the local path (decoded-timeline-v1).
    """
    from remote_detect import RemoteDetectError
    analyzer._reset_temporal()
    analyzer.last_video_metadata = None
    if hasattr(analyzer, '_begin_video'):
        analyzer._begin_video()
    budget = current_budget() or Budget()
    provider.budget = budget
    cap = None
    rows = []
    decode_s = 0.0
    select_s = 0.0
    try:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise ValueError('无法打开视频文件')
        raw_fps = float(cap.get(cv2.CAP_PROP_FPS))
        fps = raw_fps if positive_number(raw_fps) else None
        raw_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        declared = int(raw_count) if positive_number(raw_count) and raw_count.is_integer() else None
        provider.set_video(path, fps)
        # pass 1: decode + compress + upload (count frames)
        while True:
            budget.check()
            t = time.perf_counter(); ok, image = cap.read(); decode_s += time.perf_counter() - t
            if not ok:
                break
            if image is None or not hasattr(image, 'shape') or len(image.shape) < 2:
                raise ValueError('解码器返回无效视频帧')
            index = provider.frame_count
            budget.frame(index, image.shape, fps)
            provider.upload_frame(image, index)
        total = provider.finish_upload()
        cap.release(); cap = None
        # pass 2: re-decode + verify + local selection + smoothing/measurement
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise ValueError('无法打开视频文件')
        previous_shape = None
        shape_changes = 0
        select_s = 0.0
        while True:
            budget.check()
            t = time.perf_counter(); ok, image = cap.read(); decode_s += time.perf_counter() - t
            if not ok:
                break
            if image is None or not hasattr(image, 'shape') or len(image.shape) < 2:
                raise ValueError('解码器返回无效视频帧')
            if previous_shape is not None and image.shape[:2] != previous_shape:
                analyzer._reset_temporal()
                shape_changes += 1
            previous_shape = image.shape[:2]
            index = len(rows)
            provider.verify_frame(index, image)
            analyzer._video_frame_index = index
            t = time.perf_counter()
            override = provider.landmarks_for(image, index, fps)
            if fps is None:
                result = analyzer.analyze_frame(image, landmarks_override=override)
            else:
                result = analyzer.analyze_frame(image, frame_index=index, fps=fps, landmarks_override=override)
            select_s += time.perf_counter() - t
            budget.check()
            status = getattr(analyzer, '_detection_status', None)
            if status in PRIMARY_SUBJECT_FAILURE_STATUSES:
                selector = getattr(analyzer, '_primary_subject', None)
                raise SinglePersonVideoRequired(index, status, selector.reason if selector else None)
            if result is None:
                analyzer._reset_temporal()
                result = analyzer._calculate_metrics([], image.shape)
                result.update(landmarks=[], temporal_smoothing='reset_no_pose', pose_detected=False)
            else:
                result['pose_detected'] = True
            result.update(frame_index=index, frame_number=index, source_fps=fps,
                          timestamp_sec=index/fps if fps else None)
            selector = getattr(analyzer, '_primary_subject', None)
            result['unknown_reason'] = selector.reason if selector is not None and not result['pose_detected'] else None
            result['detection_status'] = getattr(analyzer, '_detection_status',
                                                 'detected' if result['pose_detected'] else 'no_pose')
            rows.append(result)
        selector = getattr(analyzer, '_primary_subject', None)
        if selector is not None:
            selector.finish()
            if selector.state == 'failed':
                raise SinglePersonVideoRequired(max(0, len(rows) - 1), selector.status, selector.reason)
        if not rows:
            raise ValueError('视频没有可解码帧')
        decoded = len(rows)
        if decoded != total:
            raise RemoteDetectError('count_mismatch', '二次解码帧数 %d != 上传帧数 %d' % (decoded, total))
        count_matches = declared is not None and declared == decoded
        metadata = {
            'schema_version': 'decoded-timeline-v1', 'decoded_frames': decoded,
            'declared_frames': declared, 'count_matches': count_matches,
            'coverage_expected_frames': decoded if count_matches else None,
            'coverage_denominator': 'all_decoded_frames_including_pose_failures',
            'pose_detected_frames': sum(bool(r['pose_detected']) for r in rows),
            'detection_status_counts': dict(Counter(r['detection_status'] for r in rows)),
            'fps': fps, 'frame_shape_changes': shape_changes,
            'fps_source': 'container_metadata_not_verified_capture_clock',
            'read_termination': 'read_returned_false_eof_or_decode_error',
            'analysis_scope': SCOPE,
            'identity_policy': 'no_identity_verified',
            'single_person_scene_verified': False,
        }
        selector = getattr(analyzer, '_primary_subject', None)
        if selector is not None:
            metadata['subject_selection'] = selector.metadata()
        analyzer.last_video_metadata = metadata
        for row in rows:
            row['video_timeline'] = dict(metadata)
        return rows
    finally:
        provider.stats['decode_s'] = round(decode_s, 3)
        provider.stats['select_analyze_s'] = round(select_s, 3)
        provider.stats['decoded_frame_count'] = len(rows)
        try:
            if cap is not None:
                cap.release()
        finally:
            analyzer._reset_temporal()
            if hasattr(analyzer, '_end_video'):
                analyzer._end_video()


def process_video(analyzer, path, remote_provider=None):
    # Remote offload is opt-in via POSERUN_REMOTE_DETECT_URL. Empty/unset => the
    # pure-local path below is byte-identical to production (remote_detect is not
    # even imported). Misconfigured-but-enabled => fail-closed via config_from_env.
    if remote_provider is None and os.environ.get('POSERUN_REMOTE_DETECT_URL', '').strip():
        from remote_detect import RemoteBatchLandmarks, config_from_env
        cfg = config_from_env()
        if cfg.enabled:
            remote_provider = RemoteBatchLandmarks(analyzer, cfg, current_budget() or Budget())
    if remote_provider is not None:
        return _process_video_remote(analyzer, path, remote_provider)
    analyzer._reset_temporal()
    analyzer.last_video_metadata = None
    if hasattr(analyzer, '_begin_video'):
        analyzer._begin_video()
    cap = None
    budget = current_budget() or Budget()
    rows = []
    try:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise ValueError('无法打开视频文件')
        raw_fps = float(cap.get(cv2.CAP_PROP_FPS))
        fps = raw_fps if positive_number(raw_fps) else None
        raw_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        declared = int(raw_count) if positive_number(raw_count) and raw_count.is_integer() else None
        previous_shape = None
        shape_changes = 0
        while True:
            budget.check()
            ok, image = cap.read()
            if not ok:
                break
            if image is None or not hasattr(image, 'shape') or len(image.shape) < 2:
                raise ValueError('解码器返回无效视频帧')
            if previous_shape is not None and image.shape[:2] != previous_shape:
                analyzer._reset_temporal()
                shape_changes += 1
            previous_shape = image.shape[:2]
            index = len(rows)
            budget.frame(index, image.shape, fps)
            analyzer._video_frame_index = index
            if fps is None:
                result = analyzer.analyze_frame(image)  # No fabricated fps or temporal mixing.
            else:
                result = analyzer.analyze_frame(image, frame_index=index, fps=fps)
            budget.check()
            status = getattr(analyzer, '_detection_status', None)
            if status in PRIMARY_SUBJECT_FAILURE_STATUSES:
                selector = getattr(analyzer, '_primary_subject', None)
                raise SinglePersonVideoRequired(index, status, selector.reason if selector else None)
            if result is None:
                analyzer._reset_temporal()
                result = analyzer._calculate_metrics([], image.shape)
                result.update(landmarks=[], temporal_smoothing='reset_no_pose', pose_detected=False)
            else:
                result['pose_detected'] = True
            result.update(frame_index=index, frame_number=index, source_fps=fps,
                          timestamp_sec=index/fps if fps else None)
            selector = getattr(analyzer, '_primary_subject', None)
            result['unknown_reason'] = selector.reason if selector is not None and not result['pose_detected'] else None
            result['detection_status'] = getattr(analyzer, '_detection_status',
                                                 'detected' if result['pose_detected'] else 'no_pose')
            rows.append(result)
        selector = getattr(analyzer, '_primary_subject', None)
        if selector is not None:
            selector.finish()
            if selector.state == 'failed':
                raise SinglePersonVideoRequired(max(0,len(rows)-1), selector.status, selector.reason)
        if not rows:
            raise ValueError('视频没有可解码帧')
        decoded = len(rows)
        count_matches = declared is not None and declared == decoded
        # Ratios are defined only when both the decoded timeline and the
        # available container hint agree. Still not an integrity certificate.
        metadata = {
            'schema_version': 'decoded-timeline-v1', 'decoded_frames': decoded,
            'declared_frames': declared, 'count_matches': count_matches,
            'coverage_expected_frames': decoded if count_matches else None,
            'coverage_denominator': 'all_decoded_frames_including_pose_failures',
            'pose_detected_frames': sum(bool(r['pose_detected']) for r in rows),
            'detection_status_counts': dict(Counter(r['detection_status'] for r in rows)),
            'fps': fps, 'frame_shape_changes': shape_changes,
            'fps_source': 'container_metadata_not_verified_capture_clock',
            'read_termination': 'read_returned_false_eof_or_decode_error',
            'analysis_scope': SCOPE,
            'identity_policy': 'no_identity_verified',
            'single_person_scene_verified': False,
        }
        selector = getattr(analyzer, '_primary_subject', None)
        if selector is not None:
            metadata['subject_selection'] = selector.metadata()
        analyzer.last_video_metadata = metadata
        for row in rows:
            row['video_timeline'] = dict(metadata)
        return rows
    finally:
        try:
            if cap is not None:
                cap.release()
        finally:
            analyzer._reset_temporal()
            if hasattr(analyzer, '_end_video'):
                analyzer._end_video()
