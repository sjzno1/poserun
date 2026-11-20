"""测量不变量回归测试（fix-contracts 2026-11-13，只增不减）。

覆盖：画幅纵横比/均匀缩放/平移/镜像不变、静止守恒、跨实例确定性、
时间缺口重置、x/y 独立、visibility=0 遮挡契约、小腿角地面约定（竖直=90°）、
前倾角竖直=0°。不修改 test_measurement_contract.py 的任何断言。
"""
import math
import unittest

import numpy as np

from pose_analyzer import PoseAnalyzer, Landmark, LSTMSmoother
from gait_analyzer import GaitAnalyzer


def lm(name, x, y, visibility=1.0):
    return Landmark(name, x, y, 0.0, visibility)


PIX = {
    'LEFT_HIP': (100, 100), 'RIGHT_HIP': (110, 100),
    'LEFT_KNEE': (150, 200), 'RIGHT_KNEE': (160, 200),
    'LEFT_ANKLE': (250, 250), 'RIGHT_ANKLE': (240, 250),
    'LEFT_SHOULDER': (90, 20), 'RIGHT_SHOULDER': (100, 20),
}


def landmarks_for(w, h, transform=None, visibility=1.0):
    out = []
    for name, (x, y) in PIX.items():
        if transform is not None:
            x, y = transform(x, y)
        out.append(lm(name, x / w, y / h, visibility))
    return out


def metrics_for(w, h, transform=None):
    analyzer = PoseAnalyzer.__new__(PoseAnalyzer)
    return analyzer._calculate_metrics(landmarks_for(w, h, transform), (h, w, 3))


ANGLE_KEYS = ('left_knee_angle', 'right_knee_angle', 'left_shin_angle',
              'right_shin_angle', 'left_shin_signed',
              'right_shin_signed', 'body_leang')


class GeometryInvariance(unittest.TestCase):
    def test_aspect_invariance(self):
        ref = metrics_for(400, 400)
        for w, h in ((800, 400), (400, 800), (1920, 1080)):
            m = metrics_for(w, h)
            for k in ANGLE_KEYS:
                self.assertAlmostEqual(m[k], ref[k], places=10, msg=f'{k} {w}x{h}')

    def test_uniform_scale_and_padding_invariance(self):
        ref = metrics_for(400, 400)
        m = metrics_for(1000, 1000, transform=lambda x, y: (x * 2.5 + 100, y * 2.5 + 50))
        for k in ANGLE_KEYS:
            self.assertAlmostEqual(m[k], ref[k], places=10, msg=k)

    def test_mirror_invariance(self):
        ref = metrics_for(400, 400)
        m = metrics_for(400, 400, transform=lambda x, y: (400 - x, y))
        for k in ('left_knee_angle', 'right_knee_angle', 'left_shin_angle',
                  'right_shin_angle', 'body_leang'):
            self.assertAlmostEqual(m[k], ref[k], places=10, msg=k)
        for k in ('left_shin_signed', 'right_shin_signed'):
            self.assertAlmostEqual(m[k], -ref[k], places=10, msg=k)

    def test_metrics_carry_canvas_dimensions(self):
        m = metrics_for(800, 400)
        self.assertEqual(m['frame_width'], 800)
        self.assertEqual(m['frame_height'], 400)

    def test_body_leang_vertical_is_zero(self):
        analyzer = PoseAnalyzer.__new__(PoseAnalyzer)
        lms = [lm('LEFT_HIP', 0.4, 0.6), lm('RIGHT_HIP', 0.6, 0.6),
               lm('LEFT_SHOULDER', 0.4, 0.2), lm('RIGHT_SHOULDER', 0.6, 0.2)]
        m = analyzer._calculate_metrics(lms, (400, 800, 3))
        self.assertAlmostEqual(m['body_leang'], 0.0, places=10)


class SmoothingInvariants(unittest.TestCase):
    def test_stationary_preserved_with_time_context(self):
        s = LSTMSmoother()
        p = np.array([0.5, 0.75], dtype=np.float32)
        for i in range(8):
            np.testing.assert_allclose(s.add(p, frame_index=i, fps=30.0), p, atol=1e-9)

    def test_deterministic_across_instances_with_time_context(self):
        a, b = LSTMSmoother(), LSTMSmoother()
        for i in range(12):
            p = np.array([0.5 + 0.01 * i, 0.75 - 0.005 * i])
            np.testing.assert_array_equal(a.add(p, frame_index=i, fps=30.0),
                                          b.add(p, frame_index=i, fps=30.0))

    def test_gap_duplicate_fps_change_reset(self):
        s = LSTMSmoother()
        s.add(np.array([0.1, 0.2]), frame_index=0, fps=30.0)
        s.add(np.array([0.9, 0.8]), frame_index=1, fps=30.0)
        # 缺口：index 跳变 → 直通原始点，不混合历史
        np.testing.assert_array_equal(s.add(np.array([0.3, 0.4]), frame_index=5, fps=30.0),
                                      [0.3, 0.4])
        # 连续恢复 → 从新段首重新 EMA
        out = s.add(np.array([0.5, 0.6]), frame_index=6, fps=30.0)
        np.testing.assert_allclose(out, [0.4, 0.5], atol=1e-12)
        # 重复 index → 重置直通
        np.testing.assert_array_equal(s.add(np.array([0.25, 0.25]), frame_index=6, fps=30.0),
                                      [0.25, 0.25])
        # fps 变化 → 重置直通
        np.testing.assert_array_equal(s.add(np.array([0.5, 0.5]), frame_index=7, fps=60.0),
                                      [0.5, 0.5])

    def test_no_time_context_is_passthrough(self):
        s = LSTMSmoother()
        s.add(np.array([0.1, 0.1]), frame_index=0, fps=30.0)
        s.add(np.array([0.9, 0.9]), frame_index=1, fps=30.0)
        np.testing.assert_array_equal(s.add(np.array([0.5, 0.75])), [0.5, 0.75])

    def test_xy_independent(self):
        s = LSTMSmoother()
        for i in range(10):
            out = s.add(np.array([0.1 + 0.05 * i, 0.75]), frame_index=i, fps=30.0)
            self.assertEqual(float(out[1]), 0.75)

    def test_invalid_time_never_smooths(self):
        for idx, fps in ((None, 30.0), (0, None), (0, 0), (0, -1), (-1, 30.0),
                         (0, float('nan')), (0.5, 30.0), (True, 30.0)):
            s = LSTMSmoother()
            s.add(np.array([0.9, 0.9]), frame_index=0, fps=30.0)
            np.testing.assert_array_equal(
                s.add(np.array([0.25, 0.5]), frame_index=idx, fps=fps), [0.25, 0.5])

    def test_landmark_smoothing_stationary_and_no_context(self):
        analyzer = PoseAnalyzer.__new__(PoseAnalyzer)
        analyzer._smoothers = {}
        lms = landmarks_for(400, 400)
        for i in range(6):
            out = analyzer._smooth_landmarks(lms, frame_index=i, fps=30.0)
            for a, b in zip(lms, out):
                self.assertAlmostEqual(a.x, b.x, places=7)
                self.assertAlmostEqual(a.y, b.y, places=7)
        # 无时间上下文：即使有历史也直通原始点
        shifted = [lm(x.name, x.x + 0.01, x.y) for x in lms]
        out = analyzer._smooth_landmarks(shifted)
        for a, b in zip(shifted, out):
            self.assertAlmostEqual(a.x, b.x, places=7)


class GaitInvariants(unittest.TestCase):
    def _result(self, w, h, visibility=1.0, override=None):
        lms = landmarks_for(w, h, visibility=visibility)
        if override:
            lms = [override(x) for x in lms]
        return {'frame_index': 0, 'frame_width': w, 'frame_height': h, 'landmarks': lms}

    def test_shin_pixel_value_and_ground_convention(self):
        f = GaitAnalyzer(fps=30)._frame_features(self._result(400, 400))
        # dx=100, dy=50 → 与地面夹角 atan2(50,100)；竖直=90° 约定
        self.assertAlmostEqual(f['l_shin'], math.degrees(math.atan2(50, 100)), places=10)
        self.assertAlmostEqual(f['l_shin_s'], -math.degrees(math.atan2(50, 100)), places=10)

    def test_vertical_shin_is_90_degrees_to_ground(self):
        def put_ankle_below_knee(x):
            if x.name == 'LEFT_ANKLE':
                return lm(x.name, 150 / 400, 300 / 400)
            return x
        f = GaitAnalyzer(fps=30)._frame_features(
            self._result(400, 400, override=put_ankle_below_knee))
        self.assertAlmostEqual(f['l_shin'], 90.0, places=10)

    def test_aspect_invariance(self):
        g = GaitAnalyzer(fps=30)
        a = g._frame_features(self._result(400, 400))
        b = g._frame_features(self._result(800, 400))
        for k in ('l_shin', 'r_shin', 'l_shin_s', 'r_shin_s'):
            self.assertAlmostEqual(a[k], b[k], places=10, msg=k)
        self.assertAlmostEqual(a['leg_len'] / 400, b['leg_len'] / 400, places=10)
        self.assertAlmostEqual(a['hip_y'] / 400, b['hip_y'] / 400, places=10)

    def test_visibility_zero_is_occlusion_not_default(self):
        f = GaitAnalyzer(fps=30)._frame_features(self._result(400, 400, visibility=0.0))
        self.assertIsNone(f['l_shin'])
        self.assertIsNone(f['r_shin'])
        self.assertIsNone(f['l_shin_s'])

    def test_visibility_none_is_unknown(self):
        f = GaitAnalyzer(fps=30)._frame_features(self._result(400, 400, visibility=None))
        self.assertIsNone(f['l_shin'])

    def test_missing_canvas_dimensions_is_unknown(self):
        r = self._result(400, 400)
        del r['frame_width'], r['frame_height']
        f = GaitAnalyzer(fps=30)._frame_features(r)
        self.assertIsNone(f['l_shin'])
        self.assertIsNone(f['leg_len'])


if __name__ == '__main__':
    unittest.main()
