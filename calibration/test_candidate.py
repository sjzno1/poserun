"""Independent candidate contracts; never mutate baseline module globals."""
import cProfile
import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate_analyzer import CalibratedPoseAnalyzer, CalibratedGaitAnalyzer, DeterministicSmoother
from pose_analyzer import Landmark


class CandidateContract(unittest.TestCase):
    @staticmethod
    def landmarks(w, h):
        return [Landmark(side+'_'+name, x/w, y/h, 0, 1)
                for side, off in [('LEFT', 0), ('RIGHT', 10)]
                for name, x, y in [('HIP', 100+off, 100), ('KNEE', 150+off, 200),
                                   ('ANKLE', 250+off, 250), ('SHOULDER', 90+off, 20)]]

    def test_stationary_landmark_is_preserved(self):
        smoother = DeterministicSmoother()
        for _ in range(8):
            np.testing.assert_allclose(smoother.add([.5, .75]), [.5, .75], atol=1e-6)

    def test_smoothing_is_reproducible_without_training(self):
        a, b = DeterministicSmoother(), DeterministicSmoother()
        for i in range(8):
            np.testing.assert_allclose(a.add([.2+i*.01, .6]), b.add([.2+i*.01, .6]), atol=1e-7)

    def test_knee_angle_does_not_depend_on_canvas_aspect(self):
        p = CalibratedPoseAnalyzer.__new__(CalibratedPoseAnalyzer)
        a = p._calculate_metrics(self.landmarks(400, 400), (400, 400, 3))
        b = p._calculate_metrics(self.landmarks(800, 400), (400, 800, 3))
        self.assertAlmostEqual(a['left_knee_angle'], b['left_knee_angle'], places=5)

    def test_gait_shin_uses_pixel_geometry(self):
        g = CalibratedGaitAnalyzer()
        a = g._frame_features(dict(landmarks=self.landmarks(400, 400), frame_width=400, frame_height=400))
        b = g._frame_features(dict(landmarks=self.landmarks(800, 400), frame_width=800, frame_height=400))
        self.assertAlmostEqual(a['l_shin'], b['l_shin'], places=5)

    def test_shin_pixel_angle_already_invariant(self):
        p = CalibratedPoseAnalyzer.__new__(CalibratedPoseAnalyzer)
        a = p._calculate_metrics(self.landmarks(400, 400), (400, 400, 3))
        b = p._calculate_metrics(self.landmarks(800, 400), (400, 800, 3))
        self.assertAlmostEqual(a['left_shin_angle'], b['left_shin_angle'], places=5)

    def test_visibility_zero_is_not_valid(self):
        pts = self.landmarks(400, 400)
        for p in pts:
            p.visibility = 0.
        f = CalibratedGaitAnalyzer()._frame_features(dict(landmarks=pts, frame_width=400, frame_height=400))
        self.assertIsNone(f['l_shin'])
        self.assertIsNone(f['hip_y'])

    def test_occlusion_does_not_fabricate_strike(self):
        self.assertEqual(CalibratedGaitAnalyzer()._detect_events([30, None, 80], [0, 1, 2]), ([], []))

    def test_occlusion_does_not_inflate_contact(self):
        self.assertEqual(CalibratedGaitAnalyzer()._detect_events([30, 80, None, None, 30], [0, .1, .2, 2, 3]), ([], []))


if __name__ == '__main__':
    unittest.main()
