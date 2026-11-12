"""Video-time/coverage boundary tests; synthetic inputs test mechanics, not accuracy."""
import cProfile
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from calibrated_analyzer import CalibratedPoseAnalyzer
from pose_analyzer import Landmark
from measurement_safety import safe_metrics


def points(x=.5):
    return [Landmark(side+'_'+joint, x+dx, y, 0., 1.)
            for side, dx in [('LEFT', -.05), ('RIGHT', .05)]
            for joint, y in [('SHOULDER', .2), ('HIP', .4), ('KNEE', .6), ('ANKLE', .8)]]


class Capture:
    def __init__(self, count=5, fps=30., declared=None, opened=True):
        self.count, self.fps = count, fps
        self.declared = count if declared is None else declared
        self.index, self.released, self.opened = 0, False, opened
    def isOpened(self):
        return self.opened
    def get(self, key):
        import cv2
        return self.fps if key == cv2.CAP_PROP_FPS else self.declared
    def read(self):
        if self.index >= self.count:
            return False, None
        self.index += 1
        return True, np.zeros((100, 200, 3), dtype=np.uint8)
    def release(self):
        self.released = True


class VideoPipelineTests(unittest.TestCase):
    def analyzer(self, detections):
        a = CalibratedPoseAnalyzer.__new__(CalibratedPoseAnalyzer)
        a._reset_temporal()
        a._detect_landmarks = unittest.mock.Mock(side_effect=[(row, 0) for row in detections])
        return a

    def run_video(self, a, cap):
        with patch('video_pipeline.cv2.VideoCapture', return_value=cap):
            return a.process_video('mechanics-test.mp4')

    def test_full_timeline_preserves_pose_failures(self):
        a = self.analyzer([points(), None, points(), None, points()])
        cap = Capture()
        rows = self.run_video(a, cap)
        self.assertEqual([r['frame_index'] for r in rows], list(range(5)))
        self.assertEqual([r['pose_detected'] for r in rows], [True, False, True, False, True])
        self.assertIsNone(rows[1]['knee_flexion'])
        m = safe_metrics(rows)
        self.assertEqual(m['measurement_coverage']['knee_flexion']['coverage'], 3/5)
        self.assertEqual(m['video_timeline']['pose_detected_frames'], 3)
        self.assertIsNone(m['total'])
        self.assertTrue(cap.released)

    def test_pose_loss_and_new_video_reset_smoothing(self):
        a = self.analyzer([points(.2), points(.2), points(.2), None, points(.8)])
        rows = self.run_video(a, Capture())
        self.assertAlmostEqual(rows[4]['landmarks'][0].x, .75)
        self.assertIsNone(a._last_context)
        self.assertEqual(a._smoothers, {})
        a._detect_landmarks.side_effect = [(points(.1), 0)]
        rows2 = self.run_video(a, Capture(count=1))
        self.assertAlmostEqual(rows2[0]['landmarks'][0].x, .05)
        self.assertEqual(rows2[0]['frame_index'], 0)

    def test_contiguous_frames_get_explicit_fps_and_index(self):
        a = self.analyzer([points(.2), points(.4), points(.6)])
        rows = self.run_video(a, Capture(count=3, fps=50.))
        self.assertEqual([r['timestamp_sec'] for r in rows], [0, .02, .04])
        self.assertEqual(rows[2]['temporal_smoothing'], 'contiguous_index_weighted')
        self.assertLess(rows[2]['landmarks'][0].x, .55)

    def test_resolution_change_resets_smoothing(self):
        cap = Capture(count=3)
        read = cap.read
        def changing_shape():
            ok, image = read()
            if ok and cap.index == 3:
                image = np.zeros((200, 100, 3), dtype=np.uint8)
            return ok, image
        cap.read = changing_shape
        rows = self.run_video(self.analyzer([points(.2), points(.2), points(.8)]), cap)
        self.assertAlmostEqual(rows[-1]['landmarks'][0].x, .75)
        self.assertEqual(rows[-1]['video_timeline']['frame_shape_changes'], 1)

    def test_invalid_fps_disables_smoothing_not_geometry(self):
        for fps in (0., float('nan'), float('inf'), -30.):
            with self.subTest(fps=fps):
                a = self.analyzer([points(.2), points(.4), points(.6)])
                rows = self.run_video(a, Capture(count=3, fps=fps))
                self.assertEqual(rows[2]['temporal_smoothing'], 'disabled_no_time_metadata')
                self.assertIsNone(rows[2]['timestamp_sec'])
                self.assertAlmostEqual(rows[2]['landmarks'][0].x, .55)
                self.assertIsNotNone(rows[2]['knee_flexion'])

    def test_count_mismatch_or_unknown_never_claims_full_coverage(self):
        for declared in (0., float('nan'), 6., 2., 3.5):
            with self.subTest(declared=declared):
                a = self.analyzer([points()]*3)
                rows = self.run_video(a, Capture(count=3, declared=declared))
                self.assertIsNone(safe_metrics(rows)['measurement_coverage']['shin_angle']['coverage'])
                self.assertEqual(rows[0]['video_timeline']['decoded_frames'], 3)

    def test_inconsistent_metadata_or_indices_rejected_as_denominator(self):
        a = self.analyzer([points()]*3)
        original = self.run_video(a, Capture(count=3))
        for field, value in [('frame_index', 0), ('frame_index', True), ('video_timeline', {})]:
            rows = copy.deepcopy(original)
            rows[1][field] = value
            self.assertIsNone(safe_metrics(rows)['measurement_coverage']['shin_angle']['coverage'])

    def test_all_no_pose_has_zero_coverage_not_zero_angles(self):
        rows = self.run_video(self.analyzer([None]*3), Capture(count=3))
        m = safe_metrics(rows)
        self.assertIsNone(m['avg_knee_flexion'])
        self.assertEqual(m['measurement_coverage']['knee_flexion']['coverage'], 0.)
        self.assertIsNone(m['total'])

    def test_empty_unopened_and_inference_errors_release_capture(self):
        for cap in (Capture(count=0), Capture(opened=False)):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                self.run_video(self.analyzer([]), cap)
            self.assertTrue(cap.released)
        a = self.analyzer([])
        a._detect_landmarks.side_effect = RuntimeError('inference failed')
        cap = Capture(count=1)
        with self.assertRaisesRegex(RuntimeError, 'inference failed'):
            self.run_video(a, cap)
        self.assertTrue(cap.released)
        self.assertEqual(a._smoothers, {})

    def test_failed_pose_cannot_be_counted_via_stale_values(self):
        rows = self.run_video(self.analyzer([points(), None, points()]), Capture(count=3))
        rows[1].update(knee_flexion=170, shin_angle=90, body_leang=0)
        m = safe_metrics(rows)
        self.assertEqual(m['measurement_coverage']['knee_flexion']['valid_frames'], 2)
        self.assertEqual(m['measurement_coverage']['knee_flexion']['coverage'], 2/3)

    def test_outside_image_points_are_unknown_not_clipped(self):
        p = points()
        next(lm for lm in p if lm.name == 'LEFT_ANKLE').y = 1.2
        rows = self.run_video(self.analyzer([p]), Capture(count=1))
        self.assertIsNone(rows[0]['left_knee_angle'])
        self.assertIsNone(rows[0]['knee_flexion'])
        self.assertIsNone(rows[0]['left_shin_angle'])
        self.assertEqual(safe_metrics(rows)['measurement_coverage']['knee_flexion']['coverage'], 0)

    def test_no_model_is_an_explicit_error(self):
        with patch('pose_analyzer.PoseAnalyzer._init_backend', return_value='unavailable'):
            with self.assertRaises(RuntimeError):
                CalibratedPoseAnalyzer()

    def test_detector_requires_unambiguous_subject_and_visible_geometry(self):
        class Keypoints:
            def __init__(self, count=1, confidence=True):
                self.xy = np.full((count, 17, 2), 25.)
                self.conf = np.ones((count, 17)) if confidence else None
            def __len__(self):
                return len(self.xy)
        a = CalibratedPoseAnalyzer.__new__(CalibratedPoseAnalyzer)
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        for count, confidence, status in [(2, True, 'primary_subject_ambiguous'),
                                          (1, False, 'primary_subject_unknown')]:
            a._primary_subject = None
            a._yolo_model = SimpleNamespace(predict=lambda *args, **kwargs: [SimpleNamespace(keypoints=Keypoints(count, confidence))])
            found, _ = a._detect_landmarks(frame)
            self.assertIsNone(found)
            self.assertEqual(a._detection_status, status)
        a._primary_subject = None
        valid = Keypoints()
        valid.xy[0,5:7] = [[20.,20.],[40.,20.]]
        valid.xy[0,11:13] = [[22.,50.],[38.,50.]]
        valid.xy[0,15:17] = [[22.,90.],[38.,90.]]
        a._yolo_model = SimpleNamespace(predict=lambda *args, **kwargs: [SimpleNamespace(keypoints=valid)])
        found, _ = a._detect_landmarks(frame)
        self.assertEqual(len(found), 17)
        self.assertEqual(found[0].x, .125)
        self.assertEqual(found[0].visibility, 1.)


if __name__ == '__main__':
    unittest.main()
