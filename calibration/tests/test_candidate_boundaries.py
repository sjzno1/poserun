"""Review counterexamples: isolation, missingness, time, public API, geometry."""
import tempfile
import cProfile  # Must precede project sys.path: project profile.py shadows stdlib.
import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import numpy as np
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'calibration'), str(ROOT)]
from candidate_analyzer import CalibratedPoseAnalyzer as Pose, CalibratedGaitAnalyzer as Gait
from gait_analyzer import GaitAnalyzer
from pose_analyzer import Landmark
import test_candidate as candidate_tests


def points():
    return candidate_tests.CandidateContract.landmarks(400, 400)


def row(pts=None, index=0):
    return dict(landmarks=points() if pts is None else pts,
                frame_index=index, frame_width=400, frame_height=400)


class CandidateBoundaries(unittest.TestCase):
    def setUp(self):
        self.pose = Pose.__new__(Pose)

    def metrics(self, pts):
        return self.pose._calculate_metrics(pts, (400, 400, 3))

    def test_baseline_import_isolation_in_clean_subprocesses(self):
        # Run the same baseline geometry before and after candidate-test import,
        # in both module import orders. A green baseline would be contamination.
        script = '''import cProfile,sys,unittest,io,json
sys.path[:0]=[sys.argv[1]+'/calibration',sys.argv[1]]
if sys.argv[2]=='first': import test_candidate
import test_measurement_contract as base
from pose_analyzer import PoseAnalyzer,LSTMSmoother
from gait_analyzer import GaitAnalyzer
original=(base.PoseAnalyzer,base.GaitAnalyzer,base.LSTMSmoother)
assert original==(PoseAnalyzer,GaitAnalyzer,LSTMSmoother)
names=['test_knee_angle_does_not_depend_on_canvas_aspect','test_gait_shin_uses_pixel_geometry','test_shin_pixel_angle_already_invariant']
def run():
 r=unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite(base.MeasurementContract(n) for n in names))
 return [r.testsRun,[x[0]._testMethodName for x in r.failures],[x[0]._testMethodName for x in r.errors]]
a=run()
import test_candidate
assert original==(base.PoseAnalyzer,base.GaitAnalyzer,base.LSTMSmoother)
b=run()
assert a==b
print(json.dumps(a))
'''
        outputs = []
        for order in ('first', 'last'):
            result = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT), order],
                                    cwd=str(Path(tempfile.gettempdir())), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(json.loads(result.stdout.strip().splitlines()[-1]))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], [3, ['test_knee_angle_does_not_depend_on_canvas_aspect',
                                         'test_gait_shin_uses_pixel_geometry'], []])

    def test_visibility_and_coordinate_missingness_per_metric(self):
        cases = [('visibility', v) for v in (0, None, float('nan'), float('inf'), .49)]
        cases += [(k, v) for k in ('x', 'y') for v in (None, float('nan'), float('inf'), -float('inf'))]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                pts = points()
                setattr(next(p for p in pts if p.name == 'LEFT_KNEE'), field, value)
                m, f = self.metrics(pts), Gait()._frame_features(row(pts))
                for key in ('left_knee_angle', 'left_shin_angle', 'left_shin_signed', 'knee_flexion', 'shin_angle'):
                    self.assertIsNone(m[key])
                self.assertIsNotNone(m['right_knee_angle'])
                self.assertIsNotNone(m['body_leang'])
                self.assertIsNone(f['l_shin'])
                self.assertIsNotNone(f['r_shin'])
                self.assertIsNotNone(f['hip_y'])

    def test_all_invalid_and_empty_never_normal_or_scored(self):
        for value in (0, None, float('nan'), float('inf'), .49):
            pts = points()
            for p in pts:
                p.visibility = value
            for data in (pts, []):
                m = self.metrics(data)
                for k in ('left_knee_angle', 'right_knee_angle', 'knee_flexion', 'shin_angle', 'body_leang', 'running_phase', 'score'):
                    self.assertIsNone(m[k])
                self.assertIsNone(m['symmetry']['symmetry_score'])
                self.assertNotEqual(m['symmetry']['symmetry_level'], '良好')

    def test_missing_joints_and_degenerate_bones(self):
        pts = [p for p in points() if p.name != 'LEFT_KNEE']
        self.assertIsNone(self.metrics(pts)['left_knee_angle'])
        self.assertIsNotNone(self.metrics(pts)['right_knee_angle'])
        pts = points()
        knee = next(p for p in pts if p.name == 'LEFT_KNEE')
        for name in ('LEFT_HIP', 'LEFT_ANKLE'):
            p = next(p for p in pts if p.name == name)
            p.x, p.y = knee.x, knee.y
        m = self.metrics(pts)
        self.assertIsNone(m['left_knee_angle'])
        self.assertIsNone(m['left_shin_angle'])
        self.assertIsNone(Gait()._frame_features(row(pts))['l_shin'])
        for p in pts:
            if p.name.endswith('SHOULDER'):
                hip = next(q for q in pts if q.name == p.name.replace('SHOULDER', 'HIP'))
                p.x, p.y = hip.x, hip.y
        self.assertIsNone(self.metrics(pts)['body_leang'])

    def test_invalid_hip_does_not_discard_valid_shins(self):
        pts = points()
        next(p for p in pts if p.name == 'LEFT_HIP').visibility = None
        f = Gait()._frame_features(row(pts))
        self.assertIsNone(f['hip_y'])
        self.assertIsNotNone(f['l_shin'])
        self.assertIsNone(self.metrics(pts)['body_leang'])

    def test_fallback_cannot_resurrect_geometry(self):
        fallback = dict(frame_index=0, left_shin_angle=80, right_shin_angle=80,
                        metrics=dict(left_shin_angle=80, right_shin_angle=80))
        for pts in ([], points()[:1]):
            r = {**fallback, **row(pts)}
            f = Gait()._frame_features(r)
            self.assertIsNone(f['l_shin'])
            self.assertEqual(Gait().analyze([r]*80)['valid_frame_ratio'], 0)

    def test_coverage_keeps_missing_rows_in_denominator(self):
        rows = [row(index=i) if i % 2 == 0 else {'frame_index': i} for i in range(80)]
        result = Gait().analyze(rows)
        self.assertEqual(result['valid_frame_ratio'], .5)
        self.assertEqual(result['input_frame_count'], 80)
        self.assertEqual(result['valid_frame_count'], 40)
        self.assertEqual(result['coverage_denominator'], 'input_rows_not_video_frames')
        self.assertIsNone(Gait().analyze([])['valid_frame_ratio'])

    def test_coordinate_smoothing_resets_on_every_boundary(self):
        for boundary in ('absent_point', 'empty', 'none', 'gap', 'repeat', 'reverse', 'fps'):
            with self.subTest(boundary=boundary):
                p = Pose.__new__(Pose)
                for i in range(3):
                    p._smooth_landmarks([Landmark('A', .1, .2, 0, 1)], frame_index=i, fps=30)
                index, fps = 4, 30
                if boundary in ('absent_point', 'empty', 'none'):
                    data = [Landmark('B', .3, .4, 0, 1)] if boundary == 'absent_point' else ([] if boundary == 'empty' else None)
                    p._smooth_landmarks(data, frame_index=3, fps=30)
                else:
                    index = {'gap': 10, 'repeat': 2, 'reverse': 1, 'fps': 3}[boundary]
                    fps = 60 if boundary == 'fps' else 30
                result = p._smooth_landmarks([Landmark('A', .9, .8, 0, 1)], frame_index=index, fps=fps)
                self.assertEqual((result[0].x, result[0].y), (.9, .8))

    def test_contiguous_smoothing_positive_control(self):
        for i, x in enumerate((.1, .1, .9)):
            result = self.pose._smooth_landmarks([Landmark('A', x, .8, 0, 1)], frame_index=i, fps=30)
        self.assertGreater(result[0].x, .1)
        self.assertLess(result[0].x, .9)
        self.assertAlmostEqual(result[0].y, .8)

    def test_low_confidence_resets_individual_smoother(self):
        for value in (0, None, float('nan'), float('inf')):
            p = Pose.__new__(Pose)
            for i in range(3):
                p._smooth_landmarks([Landmark('A', .1, .2, 0, 1)], frame_index=i, fps=30)
            p._smooth_landmarks([Landmark('A', .1, .2, 0, value)], frame_index=3, fps=30)
            self.assertEqual(p._smooth_landmarks([Landmark('A', .9, .8, 0, 1)], frame_index=4, fps=30)[0].x, .9)

    def test_no_time_metadata_means_no_temporal_claim_or_mixing(self):
        for _ in range(3):
            self.pose._smooth_landmarks([Landmark('A', .1, .2, 0, 1)])
        self.assertEqual(self.pose._smooth_landmarks([Landmark('A', .9, .8, 0, 1)])[0].x, .9)
        self.assertEqual(self.pose._smoothers, {})
        self.pose._detect_landmarks = lambda _: (points(), 0)
        result = self.pose.analyze_frame(np.zeros((400, 400, 3)))
        self.assertEqual(result['temporal_smoothing'], 'disabled_no_time_metadata')

    def test_analyze_frame_detection_loss_resets_before_early_return(self):
        detections = iter([(points(), 0)]*3+[(None, 0), (points(), 0)])
        self.pose._detect_landmarks = lambda _: next(detections)
        frame = np.zeros((400, 400, 3))
        for i in range(3):
            self.pose.analyze_frame(frame, frame_index=i, fps=30)
        self.assertTrue(self.pose._smoothers)
        self.assertIsNone(self.pose.analyze_frame(frame, frame_index=3, fps=30))
        self.assertEqual(self.pose._smoothers, {})
        self.assertIsNotNone(self.pose.analyze_frame(frame, frame_index=4, fps=30))

    def test_invalid_time_inputs_rejected_or_explicitly_unavailable(self):
        for index, fps in ((None, 30), (0, None), (-1, 30), (.1, 30), (float('nan'), 30), (0, float('inf')), (0, 0)):
            with self.assertRaises(ValueError):
                self.pose._smooth_landmarks(points(), frame_index=index, fps=fps)
        for fps in (None, 0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                Gait(fps)
        for times in ([0, 0], [1, 0], [0, float('nan')], [0, float('inf')]):
            with self.assertRaises(ValueError):
                Gait()._detect_events([30, 80], times)
        for indices in ([0, 0], [1, 0], [0, float('nan')], [0, None]):
            self.assertFalse(Gait().analyze([row(index=i) for i in indices])['time_metadata_valid'])

    def test_seven_frame_gap_counterexample_has_no_contact_path(self):
        angles = [30, 30, 30, 41, 59, 43, 59]
        times = [0, 1/30, 2/30, 1, 1+1/30, 1+2/30, 1+3/30]
        g = Gait()
        self.assertEqual(g._detect_events(angles, times), ([], []))
        self.assertEqual(g._detect_events(angles[3:], times[3:]), ([], []))
        with patch.object(GaitAnalyzer, 'analyze', side_effect=AssertionError('base contact inference forbidden')):
            result = g.analyze([row(index=round(t*30)) for t in times])
        self.assertIsNone(result['total_steps'])
        self.assertEqual(result['event_method'], 'disabled_coverage_only')

    def test_public_output_allowlist_and_no_physical_diagnostics(self):
        rows = []
        for i in range(120):
            pts = points()
            angle = math.radians(30 if i % 12 < 6 else 80)
            for side in ('LEFT', 'RIGHT'):
                k = next(p for p in pts if p.name == side+'_KNEE')
                a = next(p for p in pts if p.name == side+'_ANKLE')
                a.x, a.y = k.x+.2*math.cos(angle), k.y+.2*math.sin(angle)
            rows.append(row(pts, i))
        result = Gait().analyze(rows)
        allowed = {'cadence', 'total_steps', 'left_steps', 'right_steps', 'duration_sec', 'vertical_oscillation',
                   'stance_ratio', 'ground_contact_time_ms', 'swing_time_ms', 'stride_length_legs', 'stride_length_m',
                   'step_symmetry', 'running_phase', 'score', 'cadence_level', 'vertical_oscillation_level',
                   'step_symmetry_level', 'strike_posture', 'event_method', 'valid_frame_ratio', 'input_frame_count',
                   'valid_frame_count', 'left_valid_frame_count', 'right_valid_frame_count', 'coverage_denominator',
                   'time_metadata_valid', 'measurement_limitations'}
        self.assertEqual(set(result), allowed)
        for key in ('cadence', 'total_steps', 'left_steps', 'right_steps', 'step_symmetry', 'running_phase', 'score',
                    'ground_contact_time_ms', 'stance_ratio', 'swing_time_ms', 'vertical_oscillation',
                    'stride_length_legs', 'stride_length_m'):
            self.assertIsNone(result[key])
        self.assertEqual(result['valid_frame_ratio'], 1)
        self.assertNotIn('良好', json.dumps(result, ensure_ascii=False))

    def test_existing_report_aggregation_fails_closed_not_new_scores(self):
        from report_generator import ReportGenerator
        metrics = [self.metrics(points()) for _ in range(20)]
        # The consumer now accepts explicit unknowns without creating scores.
        generator = ReportGenerator.__new__(ReportGenerator)
        summary = generator._calculate_comprehensive_metrics(metrics)
        self.assertIsNone(summary['total'])
        self.assertIsNone(summary['grade'])
        self.assertEqual(summary['breakdown'], [])
        self.assertEqual(summary['problems'], [])
        self.assertIsNone(generator._calculate_score(summary)['total'])
        self.assertTrue(summary['measurement_status'].startswith('unscored'))

    def test_subpixel_geometry_padding_scale_aspect_and_mirror(self):
        original = points()
        for i, p in enumerate(original):
            p.x += .00031*(i+1)
            p.y += .00019*(i+1)
        reference = self.metrics(original)
        for scale, dx, dy, w, h, mirror in ((1, 0, 0, 800, 400, False),
                                           (1.7, 13.13, 21.71, 1100, 800, False),
                                           (.75, 10.31, 5.67, 600, 500, True)):
            transformed = copy.deepcopy(original)
            for p in transformed:
                p.x, p.y = (p.x*400*scale+dx)/w, (p.y*400*scale+dy)/h
                if mirror:
                    p.x = 1-p.x
            m = self.pose._calculate_metrics(transformed, (h, w, 3))
            for key in ('left_knee_angle', 'right_knee_angle', 'left_shin_angle', 'right_shin_angle', 'body_leang'):
                self.assertAlmostEqual(m[key], reference[key], places=10)
            f = Gait()._frame_features(dict(landmarks=transformed, frame_width=w, frame_height=h))
            self.assertAlmostEqual(f['l_shin'], reference['left_shin_angle'], places=10)
            self.assertIsNone(m['stride_analysis']['landing_foot_x'])
            self.assertIsNone(m['stride_analysis']['stride_distance'])
            self.assertIsNone(m['running_phase'])

    def test_invalid_dimensions_and_duplicates_are_not_geometry(self):
        for v in (0, -1, None, float('nan'), float('inf')):
            m = self.pose._calculate_metrics(points(), (400, v, 3))
            self.assertIsNone(m['left_knee_angle'])
            self.assertIsNone(m['body_leang'])
            self.assertIsNone(Gait()._frame_features({**row(), 'frame_width': v})['l_shin'])
        pts = points()
        pts.append(copy.deepcopy(next(p for p in pts if p.name == 'LEFT_KNEE')))
        self.assertIsNone(self.metrics(pts)['left_knee_angle'])


if __name__ == '__main__':
    unittest.main()