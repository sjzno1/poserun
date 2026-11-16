"""Synthetic invariants only: these do not validate real-video accuracy."""
import copy
import importlib.util
from pathlib import Path
import unittest
import numpy as np

import pytest

_TARGET = Path(__file__).resolve().parents[1] / 'cadence_candidate.py'
if not _TARGET.exists():
    pytest.skip('cadence_candidate.py not present in this distribution',
                allow_module_level=True)
SPEC = importlib.util.spec_from_file_location('cadence_candidate', _TARGET)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
estimate = MODULE.estimate_cadence


def synthetic(f=1.5, fps: float=30., duration=6, harmonic=0., fundamental=1.):
    frames = []
    for i in range(round(fps*duration)):
        t = i/fps
        phase = 2*np.pi*f*t
        # Time-varying camera pan and vertical drift must cancel.
        cx, cy = .5+.04*np.sin(.4*t), .45+.02*t
        lm = []
        for side, sign in [('LEFT', 1), ('RIGHT', -1)]:
            for joint, height, amp in [('HIP', 0., 0.), ('SHOULDER', -.2, 0.),
                                       ('KNEE', .15, .055), ('ANKLE', .3, .1)]:
                wave = sign*fundamental*np.sin(phase)+harmonic*np.sin(2*phase)
                lm.append(dict(name=side+'_'+joint, x=cx+amp*wave,
                               y=cy+height, visibility=.99))
        frames.append(dict(frame_index=i, frame_width=1280, frame_height=720, landmarks=lm))
    return frames


class CadenceTests(unittest.TestCase):
    def assert_frequency(self, frames, fps, f):
        out = estimate(frames, fps)
        self.assertEqual(out['status'], 'estimated', str(out))
        self.assertAlmostEqual(out['cadence_spm'], 120*f, delta=3.)
        self.assertNotIn('ground_contact_time_ms', out)
        self.assertNotIn('score', out)
        return out

    def test_frequencies_without_elite_prior(self):
        for f in (.7, 1.5, 2.7, 4.):
            with self.subTest(f=f):
                self.assert_frequency(synthetic(f=f, fps=60, duration=8), 60, f)

    def test_sampling_rates(self):
        for fps in (24, 29.97, 50, 90):
            with self.subTest(fps=fps):
                self.assert_frequency(synthetic(fps=fps), fps, 1.5)

    def test_mirror_translation_scaling_and_global_side_swap(self):
        original = synthetic()
        expected = estimate(original, 30)
        frames = copy.deepcopy(original)
        for frame in frames:
            for p in frame['landmarks']:
                p['x'] = 1.7*(1-p['x'])+.3
                p['y'] = 1.7*p['y']-.2
                p['name'] = p['name'].replace('LEFT', 'TEMP').replace('RIGHT', 'LEFT').replace('TEMP', 'RIGHT')
        actual = self.assert_frequency(frames, 30, 1.5)
        self.assertEqual(actual['cadence_spm'], expected['cadence_spm'])

    def test_short_sparse_missing_and_zero_visibility_refuse(self):
        for frames in [synthetic(duration=2), synthetic()[::8]]:
            self.assertEqual(estimate(frames, 30)['status'], 'refused')
        frames = synthetic()
        for frame in frames:
            for p in frame['landmarks']:
                if 'ANKLE' in p['name']:
                    p['visibility'] = 0
        self.assertEqual(estimate(frames, 30)['status'], 'refused')

    def test_small_random_missing_no_interpolation(self):
        frames = synthetic()
        rng = np.random.default_rng(100)
        for frame in frames:
            if rng.uniform() < .12:
                for p in frame['landmarks']:
                    if 'ANKLE' in p['name'] or 'KNEE' in p['name']:
                        p['visibility'] = 0
        self.assert_frequency(frames, 30, 1.5)

    def test_long_occlusion_refuses(self):
        frames = synthetic()
        for frame in frames[60:84]:
            for p in frame['landmarks']:
                if 'ANKLE' in p['name'] or 'KNEE' in p['name']:
                    p['visibility'] = 0
        self.assertEqual(estimate(frames, 30)['status'], 'refused')

    def test_strong_common_second_harmonic_does_not_double(self):
        self.assert_frequency(synthetic(harmonic=.4), 30, 1.5)

    def test_fixed_side_offset_is_not_a_new_cycle(self):
        frames = synthetic()
        for frame in frames:
            for p in frame['landmarks']:
                if p['name'] in ('LEFT_ANKLE', 'LEFT_KNEE'):
                    p['x'] += .03
        self.assert_frequency(frames, 30, 1.5)

    def test_dominant_antiphase_harmonic_refuses(self):
        frames = synthetic()
        for i, frame in enumerate(frames):
            phase = 2*np.pi*1.5*i/30
            for p in frame['landmarks']:
                if 'ANKLE' in p['name'] or 'KNEE' in p['name']:
                    amp = .1 if 'ANKLE' in p['name'] else .055
                    sign = 1 if p['name'].startswith('LEFT') else -1
                    p['x'] += sign*amp*(-.5*np.sin(phase)+np.sin(2*phase))
        self.assertEqual(estimate(frames, 30)['status'], 'refused')

    def test_only_common_harmonic_refuses_not_halved_or_doubled(self):
        self.assertEqual(estimate(synthetic(harmonic=1., fundamental=0.), 30)['status'], 'refused')

    def test_half_cycle_side_swaps_refuse(self):
        frames = synthetic()
        for i, frame in enumerate(frames):
            if np.sin(2*np.pi*1.5*i/30) < 0:
                for p in frame['landmarks']:
                    if 'KNEE' in p['name'] or 'ANKLE' in p['name']:
                        p['name'] = p['name'].replace('LEFT', 'TEMP').replace('RIGHT', 'LEFT').replace('TEMP', 'RIGHT')
        self.assertEqual(estimate(frames, 30)['status'], 'refused')

    def test_random_side_swaps_refuse(self):
        frames = synthetic()
        rng = np.random.default_rng(18)
        for frame in frames:
            if rng.uniform() < .5:
                for p in frame['landmarks']:
                    p['name'] = p['name'].replace('LEFT', 'TEMP').replace('RIGHT', 'LEFT').replace('TEMP', 'RIGHT')
        self.assertEqual(estimate(frames, 30)['status'], 'refused')

    def test_determinism_and_input_unchanged(self):
        frames = synthetic()
        before = copy.deepcopy(frames)
        self.assertEqual(estimate(frames, 30), estimate(frames, 30))
        self.assertEqual(frames, before)

    def test_static_and_duplicate_times_refuse(self):
        frames = synthetic(f=0)
        self.assertEqual(estimate(frames, 30)['status'], 'refused')
        frames[2]['frame_index'] = frames[1]['frame_index']
        self.assertEqual(estimate(frames, 30)['status'], 'refused')


if __name__ == '__main__':
    unittest.main()
