"""Independent, deterministic bilateral pose-periodicity candidate (NumPy only).

Not contact detection: no touchdown, stance, speed, score or athlete priors.
Input is cached COCO named normalized landmarks, dimensions and frame_index;
fps must be supplied from media metadata, never inferred from athlete identity.
A positive result is conditional on correct bilateral tracking and playback time.
Search bounds are observation length (>=3 stride cycles) and sampling (>=8
samples/stride), NOT a population cadence interval. Unresolved harmonics refuse.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any
import numpy as np


def _detrend(t, y):
    a = np.column_stack([np.ones(len(t)), t - np.mean(t)])
    return y - a @ np.linalg.lstsq(a, y, rcond=None)[0]


def _power(t, y, frequencies):
    """Fraction detrended variance explained by a sinusoid; no interpolation."""
    y = _detrend(t, y)
    den = float(y @ y)
    if den < 1e-12:
        return np.zeros(len(frequencies))
    out = []
    for f in frequencies:
        a = np.column_stack([np.sin(2*np.pi*f*t), np.cos(2*np.pi*f*t)])
        a = np.column_stack([_detrend(t, a[:, 0]), _detrend(t, a[:, 1])])
        fit = a @ np.linalg.lstsq(a, y, rcond=None)[0]
        out.append(max(0., min(1., float(fit @ fit / den))))
    return np.asarray(out)


def _gap(t, start, end):
    return float(np.max(np.diff(np.r_[start, t, end])))


def estimate_cadence(frames, fps, min_visibility=0.5):
    """Return evidence and cadence_spm or explicit refusal. No labels consumed.

    Confidence is a conservative evidence category, NOT calibrated probability.
    Uses hip-centered coordinates divided by shoulder/hip torso length in pixel
    geometry; global image translation, reflection and uniform scaling cancel.
    Side swaps are not silently repaired: contradictory signed/unsigned periods
    or phase evidence cause refusal. Correlated model errors remain undetectable.
    """
    result: dict[str, Any] = dict(method='bilateral_pose_periodicity_v1', status='refused',
                  cadence_spm=None, confidence='insufficient', reasons=[],
                  evidence=[], fps=float(fps), frame_count=len(frames),
                  limitations=['Pose cycles, not observed foot contacts.',
                               'Conditional on playback speed and target identity.',
                               'Confidence is not a probability or accuracy claim.',
                               'Coherent bilateral detector errors can survive these checks.',
                               'Pure harmonics with no observable fundamental are not identifiable from pose alone.',
                               'Conservative alternation gate can reject real asymmetric/projected motion.'])
    def refuse(reason):
        result['reasons'].append(reason)
        return result
    if not np.isfinite(fps) or fps <= 0:
        return refuse('invalid_fps')
    if len(frames) < 24:
        return refuse('too_few_frames')
    try:
        t = np.array([float(f['frame_index']) / fps for f in frames])
    except (KeyError, ValueError, TypeError):
        return refuse('missing_or_invalid_frame_indices')
    if not np.all(np.isfinite(t)) or np.any(np.diff(t) <= 0):
        return refuse('non_monotonic_frame_indices')
    t -= t[0]
    duration = float(t[-1])
    result['duration_s'] = duration
    if duration < 3:
        return refuse('short_clip_less_than_3_seconds')
    observed_fps = min(fps, 1/float(np.median(np.diff(t))))
    low, high = 3/duration, observed_fps/8
    result['search_stride_hz'] = [low, high]
    if high <= low:
        return refuse('insufficient_sampling_for_three_cycles')
    frequencies = np.arange(low, high, 1/(duration*30))
    points = {j: np.full((len(frames), 2, 2), np.nan) for j in ('ANKLE', 'KNEE')}
    for i, frame in enumerate(frames):
        lm = {p['name']: p for p in frame.get('landmarks', [])}
        w, h = frame.get('frame_width'), frame.get('frame_height')
        if not w or not h:
            continue
        def point(name):
            p = lm.get(name, {})
            v = p.get('visibility')
            if v is None or not np.isfinite(v) or v < min_visibility:
                return None
            xy = np.array([p.get('x', np.nan)*w, p.get('y', np.nan)*h])
            return xy if np.all(np.isfinite(xy)) else None
        hips = [point(s+'_HIP') for s in ('LEFT', 'RIGHT')]
        shoulders = [point(s+'_SHOULDER') for s in ('LEFT', 'RIGHT')]
        if any(p is None for p in hips+shoulders):
            continue
        center = np.mean(hips, axis=0)
        scale = np.linalg.norm(np.mean(shoulders, axis=0)-center)
        if scale <= 1e-6:
            continue
        for joint in points:
            for side, name in enumerate(('LEFT', 'RIGHT')):
                p = point(name+'_'+joint)
                if p is not None:
                    points[joint][i, side] = (p-center)/scale
    accepted = []
    for joint, xy in points.items():
        for axis in range(2):
            valid = np.all(np.isfinite(xy[:, :, axis]), axis=1)
            coverage = float(np.mean(valid))
            ev: dict[str, Any] = dict(joint=joint, axis='xy'[axis], coverage=coverage)
            result['evidence'].append(ev)
            if coverage < .7:
                ev['rejection'] = 'bilateral_coverage_below_0.7'
                continue
            times = t[valid]
            left, right = xy[valid, 0, axis], xy[valid, 1, axis]
            diff = left-right
            amp = float(np.std(_detrend(times, diff)))
            ev['difference_std_torso_units'] = amp
            if amp < .025:
                ev['rejection'] = 'insufficient_relative_motion'
                continue
            powers = _power(times, diff, frequencies)
            idx = int(np.argmax(powers))
            f, power = float(frequencies[idx]), float(powers[idx])
            subharmonic_power = float(_power(times, diff, [f/2])[0])
            # If relative positions never alternate, side sorting can turn one
            # stride into two apparent cycles. Do not assume those labels valid.
            alternating_fraction = float(min(np.mean(diff > 0), np.mean(diff < 0)))
            # Squared centered bilateral difference should repeat twice per
            # signed cycle. This is a harmonic consistency check on the SAME
            # observations, not independent step/contact evidence.
            # Center before squaring: a fixed projected left/right offset is
            # not motion and otherwise creates a spurious fundamental term.
            separation = _detrend(times, diff)**2
            step_power = float(_power(times, separation, [2*f])[0])
            sep_grid = np.arange(low, observed_fps/4, 1/(duration*30))
            sep_powers = _power(times, separation, sep_grid)
            sep_best = float(sep_grid[int(np.argmax(sep_powers))])
            a, b = _detrend(times, left), _detrend(times, right)
            den = float(np.linalg.norm(a)*np.linalg.norm(b))
            corr = float(a@b/den) if den > 1e-12 else 1.
            rivals = powers[np.abs(frequencies-f) > 1/duration]
            rival = float(np.max(rivals)) if len(rivals) else 0.
            halves = []
            for mask in (times <= duration/2, times > duration/2):
                tt, yy = times[mask], diff[mask]
                halves.append(float(_power(tt, yy, [f])[0]) if len(tt) >= 8 else 0.)
            ev.update(stride_hz=f, candidate_spm=120*f,
                      signed_power=power, unsigned_step_power=step_power,
                      unsigned_best_hz=sep_best, left_right_correlation=corr,
                      half_frequency_power=subharmonic_power,
                      alternating_position_fraction=alternating_fraction,
                      competing_power=rival, half_window_power=halves,
                      max_gap_s=_gap(times, 0, duration))
            failures = []
            if power < .55: failures.append('weak_signed_periodicity')
            if subharmonic_power > .12: failures.append('half_frequency_ambiguity')
            if alternating_fraction < .15: failures.append('side_sorting_or_non_alternating_positions')
            if corr > -.25: failures.append('no_bilateral_antiphase')
            if step_power < .25 or abs(sep_best-2*f) > 1/duration:
                failures.append('factor_two_or_side_identity_ambiguous')
            if rival > .75*power: failures.append('competing_frequency')
            if min(halves) < .4: failures.append('unstable_half_windows')
            if ev['max_gap_s'] > min(.3, .5/f): failures.append('long_occlusion')
            if failures:
                ev['rejection'] = failures
            else:
                accepted.append(ev)
    # Independent joint families must agree; axes are correlated, not replicates.
    pairs = [(a, b) for a in accepted for b in accepted
             if a['joint'] == 'ANKLE' and b['joint'] == 'KNEE'
             and abs(a['stride_hz']-b['stride_hz']) <= 1/duration]
    if not pairs:
        return refuse('no_agreeing_unambiguous_ankle_and_knee_cycles')
    best = max(pairs, key=lambda p: min(e['signed_power'] for e in p))
    f = float(np.mean([e['stride_hz'] for e in best]))
    if any(abs(e['stride_hz']-f) > 1/duration for e in accepted):
        return refuse('joint_or_axis_frequency_conflict')
    result.update(status='estimated', cadence_spm=120*f,
                  confidence='moderate_conditional',
                  selected_channels=[e['joint']+'_'+e['axis'] for e in best],
                  resolution_spm=120/duration,
                  resolution_note='Rayleigh stride-frequency resolution converted to steps/min; not a statistical CI.',
                  reasons=['Two joint families, antiphase and unsigned factor-two evidence agree.'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw_frames', type=Path)
    parser.add_argument('--fps', type=float, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = estimate_cadence(json.loads(args.raw_frames.read_text()), args.fps)
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(text+'\n')
    else:
        print(text)


if __name__ == '__main__':
    main()
