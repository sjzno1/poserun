"""Unscored numerical geometry candidate; no validated contact/scoring model.

No baseline classes/functions are replaced. Without explicit frame_index AND
fps, smoothing is disabled. Video decoding supplies explicit contiguous time
context and retains failed detections as unknown rows in the timeline.
"""
from __future__ import annotations
import math
import cv2
import numpy as np
from pose_analyzer import PoseAnalyzer, Landmark, YOLO_POSE_LANDMARK_NAMES
from gait_analyzer import GaitAnalyzer, MIN_VISIBILITY


def _finite(value):
    return isinstance(value, (int, float, np.number)) and not isinstance(value, (bool, np.bool_)) and math.isfinite(value)


def _valid(point):
    return (point is not None and all(_finite(getattr(point, k, None)) for k in ('x', 'y', 'visibility'))
            and 0 <= point.x <= 1 and 0 <= point.y <= 1
            and MIN_VISIBILITY <= point.visibility <= 1)


def _dimensions(shape):
    if shape is None or len(shape) < 2:
        return None
    h, w = shape[:2]
    return (float(h), float(w)) if all(_finite(v) and v > 0 for v in (h, w)) else None


def _points(landmarks):
    # Duplicate names are ambiguous, not a license to silently select a point.
    points, seen = {}, set()
    for lm in landmarks or []:
        name = lm.name
        points[name] = None if name in seen else lm
        seen.add(name)
    return {name: lm for name, lm in points.items() if _valid(lm)}


def _vector(a, b, w, h):
    if not (_valid(a) and _valid(b)):
        return None
    dx, dy = (b.x-a.x)*w, (b.y-a.y)*h
    if not all(_finite(v) for v in (dx, dy)):
        return None
    length = math.hypot(dx, dy)
    return (dx/length, dy/length) if _finite(length) and length > 0 else None


class DeterministicSmoother:
    def __init__(self, hidden_size=32, num_steps=5):
        self.num_steps = num_steps
        self._buffer = []

    def add(self, point):
        self._buffer.append(np.asarray(point, dtype=float).copy())
        del self._buffer[:-self.num_steps]
        if len(self._buffer) < 3:
            return self._buffer[-1].copy()
        weights = np.array([.7**(len(self._buffer)-1-i) for i in range(len(self._buffer))])
        weights /= weights.sum()
        return (np.array(self._buffer)*weights[:, None]).sum(axis=0)

    def reset(self):
        self._buffer = []


class CalibratedPoseAnalyzer(PoseAnalyzer):
    """Unscored same-scale geometry with explicit video-time context.

    Detection conservatively selects and continuously locks one primary runner.
    Heuristic geometry/appearance correspondence is not identity or contact inference.
    """
    def __init__(self):
        super().__init__()
        if self.backend == 'unavailable':
            raise RuntimeError('姿态检测模型不可用；不生成替代测量')
        self.backend = 'yolo_geometry_unscored'
        import pose_analyzer as baseline
        baseline.LAST_POSE_BACKEND = self.backend  # Existing health telemetry only.
        self._reset_temporal()

    def configure_target(self, selection):
        if selection is not None:
            raise ValueError('自动主跑者模式不支持手动框选（非仅限单人视频），请移除目标参数')
        self._target_selection = None

    def _begin_video(self):
        from primary_subject import PrimarySubjectSelector
        self._primary_subject = PrimarySubjectSelector()
        self._subject_frame_counter = 0
        self._subject_requested_index = None
        self._target_selection = None
        self._target_tracker = None  # Retired explicit-selection library is never called.
        self._video_frame_index = None
        self._detection_status = 'no_pose'

    def _end_video(self):
        self._primary_subject = None
        self._subject_frame_counter = 0
        self._subject_requested_index = None
        self._target_tracker = None
        self._video_frame_index = None
        self._detection_status = 'no_pose'

    def _reject_target(self, reason):
        self._detection_status = reason
        return None, 0

    def process_video(self, video_path):
        from video_pipeline import process_video
        return process_video(self, video_path)

    def _detect_landmarks(self, frame):
        from primary_subject import PrimarySubjectSelector
        if getattr(self, '_primary_subject', None) is None:
            self._primary_subject = PrimarySubjectSelector()
            self._subject_frame_counter = 0
        selector = self._primary_subject
        if self._yolo_model is None:
            raise RuntimeError('姿态检测模型不可用')
        # Preserve the detector threshold, including background/competing people.
        results = self._yolo_model.predict(frame, conf=0.05, verbose=False)
        points = results[0].keypoints if results else None
        boxes = getattr(results[0], 'boxes', None) if results else None
        index = getattr(self, '_subject_requested_index', None)
        if index is None:
            index = getattr(self, '_video_frame_index', None)
        if index is None:
            index = self._subject_frame_counter
        self._subject_frame_counter += 1
        selected = selector.select(frame, getattr(points, 'xy', None),
                                   getattr(points, 'conf', None),
                                   getattr(boxes, 'xyxy', None), index,
                                   fps=getattr(self,'_subject_requested_fps',None))
        self._detection_status = selector.status
        if selected is None:
            return None, 0
        xy, confidence = points.xy[selected], points.conf[selected]
        h, w = frame.shape[:2]
        masked_joints = set(selector.masked_joints)
        landmarks = [Landmark(name, float(xy[i][0])/w, float(xy[i][1])/h,
                              0., 0. if i in masked_joints else float(confidence[i]))
                     for i, name in enumerate(YOLO_POSE_LANDMARK_NAMES)]
        return landmarks, 0

    def _reset_temporal(self):
        self._smoothers = {}
        self._last_context = None

    def _smooth_landmarks(self, landmarks, *, frame_index=None, fps=None):
        if frame_index is None and fps is None:
            self._reset_temporal()
            return list(landmarks or [])
        if not (_finite(frame_index) and frame_index >= 0 and int(frame_index) == frame_index
                and _finite(fps) and fps > 0):
            self._reset_temporal()
            raise ValueError('smoothing requires finite nonnegative integer frame_index and positive fps')
        context = (frame_index, float(fps))
        previous = getattr(self, '_last_context', None)
        if previous is None or previous[1] != fps or frame_index != previous[0]+1:
            self._reset_temporal()
        self._last_context = context
        valid = _points(landmarks)
        for name in list(self._smoothers):
            if name not in valid:
                del self._smoothers[name]
        out = []
        for lm in landmarks or []:
            if lm.name in valid:
                smoother = self._smoothers.setdefault(lm.name, DeterministicSmoother())
                x, y = smoother.add([lm.x, lm.y])
                out.append(Landmark(lm.name, float(x), float(y), lm.z, lm.visibility))
            else:
                out.append(lm)
        return out

    def analyze_frame(self, frame, *, frame_index=None, fps=None):
        self._subject_requested_index = frame_index
        self._subject_requested_fps = fps
        try:
            landmarks, _ = self._detect_landmarks(frame)
        finally:
            self._subject_requested_index = None
            self._subject_requested_fps = None
        if not landmarks:
            self._reset_temporal()
            return None
        landmarks = self._smooth_landmarks(landmarks, frame_index=frame_index, fps=fps)
        result = self._calculate_metrics(landmarks, frame.shape)
        result['landmarks'] = landmarks
        result['frame_index'] = frame_index
        result['temporal_smoothing'] = ('contiguous_index_weighted' if frame_index is not None
                                        else 'disabled_no_time_metadata')
        return result

    def _calculate_metrics(self, landmarks, frame_shape):
        dims = _dimensions(frame_shape)
        self._metric_shape = frame_shape
        points = _points(landmarks) if dims else {}
        h, w = dims or (None, None)
        metrics = {'frame_width': w, 'frame_height': h}
        for side in ('LEFT', 'RIGHT'):
            hip, knee, ankle = (points.get(side+'_'+n) for n in ('HIP', 'KNEE', 'ANKLE'))
            prefix = side.lower()
            metrics[prefix+'_knee_angle'] = self._calculate_angle(hip, knee, ankle) if dims else None
            metrics[prefix+'_shin_angle'] = self._calculate_angle_with_ground(knee, ankle, w, h) if dims else None
            metrics[prefix+'_shin_signed'] = self._calculate_shin_angle_signed(knee, ankle, w, h) if dims else None
        for aggregate, name in (('knee_flexion', 'knee_angle'), ('shin_angle', 'shin_angle')):
            a, b = (metrics[s+'_'+name] for s in ('left', 'right'))
            metrics[aggregate] = (a+b)/2 if a is not None and b is not None else None
        metrics['body_leang'] = self._calculate_body_leang(points) if dims else None
        # Camera projection, contact side and running direction are unvalidated.
        # Explicit nulls prevent report consumers from defaulting to normal values.
        metrics['stride_analysis'] = dict.fromkeys(('stride_distance', 'center_of_mass_x', 'landing_foot_x'))
        metrics['stride_analysis']['stride_level'] = '未验证，不判跨步'
        metrics['symmetry'] = dict.fromkeys(('knee_distance', 'shoulder_distance', 'symmetry_score'))
        metrics['symmetry']['symmetry_level'] = '未验证'
        metrics['running_phase'] = None
        metrics['score'] = None
        metrics['measurement_status'] = 'unscored_2d_geometry_only'
        return metrics

    def _calculate_angle(self, a, b, c):
        dims = _dimensions(self._metric_shape)
        if not dims:
            return None
        h, w = dims
        va, vc = _vector(b, a, w, h), _vector(b, c, w, h)
        if va is None or vc is None:
            return None
        return math.degrees(math.acos(max(-1., min(1., va[0]*vc[0]+va[1]*vc[1]))))

    def _calculate_angle_with_ground(self, knee, ankle, width, height):
        v = _vector(knee, ankle, width, height)
        return math.degrees(math.atan2(abs(v[1]), abs(v[0]))) if v else None

    def _calculate_shin_angle_signed(self, knee, ankle, width, height):
        v = _vector(knee, ankle, width, height)
        return (1 if v[0] < 0 else -1)*math.degrees(math.atan2(abs(v[1]), abs(v[0]))) if v else None

    def _calculate_body_leang(self, points):
        need = ['LEFT_SHOULDER', 'RIGHT_SHOULDER', 'LEFT_HIP', 'RIGHT_HIP']
        if any(not _valid(points.get(n)) for n in need):
            return None
        dims = _dimensions(self._metric_shape)
        if not dims:
            return None
        h, w = dims
        shoulder, hip = [Landmark('MID', points['LEFT_'+n].x/2+points['RIGHT_'+n].x/2,
                                  points['LEFT_'+n].y/2+points['RIGHT_'+n].y/2, 0, 1)
                         for n in ('SHOULDER', 'HIP')]
        v = _vector(hip, shoulder, w, h)
        return math.degrees(math.atan2(abs(v[0]), abs(v[1]))) if v else None


class CalibratedGaitAnalyzer(GaitAnalyzer):
    """Coverage only: deliberately no filtering, contacts, cycles or diagnostics."""
    def __init__(self, fps=30.):
        if not _finite(fps) or fps <= 0:
            raise ValueError('fps must be finite and positive')
        self.fps = float(fps)

    def _frame_features(self, result):
        dims = _dimensions((result.get('frame_height'), result.get('frame_width')))
        points = _points(result.get('landmarks')) if dims else {}
        h, w = dims or (1., 1.)
        index = result.get('frame_index')
        t = index/self.fps if _finite(index) and index >= 0 and int(index) == index else None
        f = dict.fromkeys(('l_shin', 'r_shin', 'l_shin_s', 'r_shin_s', 'hip_y', 'l_ax', 'r_ax', 'leg_len'))
        f['t'] = t
        for side, short in (('LEFT', 'l'), ('RIGHT', 'r')):
            knee, ankle = (points.get(side+'_'+n) for n in ('KNEE', 'ANKLE'))
            v = _vector(knee, ankle, w, h)
            if v:
                f[short+'_shin'] = math.degrees(math.atan2(abs(v[1]), abs(v[0])))
                f[short+'_shin_s'] = (1 if v[0] < 0 else -1)*f[short+'_shin']
            if ankle:
                f[short+'_ax'] = ankle.x*w/h
        if all(n in points for n in ('LEFT_HIP', 'RIGHT_HIP')):
            f['hip_y'] = points['LEFT_HIP'].y/2+points['RIGHT_HIP'].y/2
        return f

    def _detect_events(self, shin_series, t_series):
        """Compatibility hook: contact inference is disabled, not zero steps."""
        if len(shin_series) != len(t_series) or any(not _finite(t) for t in t_series):
            raise ValueError('invalid event timestamps')
        if any(b <= a for a, b in zip(t_series, t_series[1:])):
            raise ValueError('event timestamps must be strictly increasing')
        return [], []

    def analyze(self, results, body_height_m=None):
        # Do not call base.analyze: even debug contact counts would inherit its
        # cross-gap filtering and turn unsupported proxy events into evidence.
        rows = list(results or [])
        features = [self._frame_features(r) for r in rows]
        count = len(rows)
        both = sum(f['l_shin'] is not None and f['r_shin'] is not None for f in features)
        times = [f['t'] for f in features]
        time_valid = bool(times) and all(t is not None for t in times) and all(b > a for a, b in zip(times, times[1:]))
        g = dict.fromkeys(('cadence', 'total_steps', 'left_steps', 'right_steps', 'duration_sec',
                           'vertical_oscillation', 'stance_ratio', 'ground_contact_time_ms',
                           'swing_time_ms', 'stride_length_legs', 'stride_length_m', 'step_symmetry',
                           'running_phase', 'score'))
        for key in ('cadence_level', 'vertical_oscillation_level', 'step_symmetry_level'):
            g[key] = '未验证'
        g['strike_posture'] = dict.fromkeys(('left_forward_deg', 'left_backward_deg',
                                             'right_forward_deg', 'right_backward_deg', 'over_stride_ratio'))
        g['strike_posture']['over_stride_level'] = '未验证'
        g.update(event_method='disabled_coverage_only', valid_frame_ratio=both/count if count else None,
                 input_frame_count=count, valid_frame_count=both,
                 left_valid_frame_count=sum(f['l_shin'] is not None for f in features),
                 right_valid_frame_count=sum(f['r_shin'] is not None for f in features),
                 coverage_denominator='input_rows_not_video_frames', time_metadata_valid=time_valid,
                 measurement_limitations=['No contact inference or validated physical gait metrics',
                                         'Coverage is not accuracy; omitted video frames are unknown',
                                         'Unscored 2D geometry; not compatible with production scoring'])
        return g
