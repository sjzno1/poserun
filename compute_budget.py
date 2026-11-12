"""Explicit limits plus fresh-exec hard watchdog for all analysis/report stages.

Native decoder/Torch calls require an external process watchdog for a HARD wall
clock. This module never describes a cooperative deadline as hard cancellation.
"""
import contextvars
import fcntl
import functools
import math
import os
import time
from dataclasses import dataclass


class BudgetExceeded(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_batch: int = 4
    max_frames: int = 1800
    max_total_frames: int = 3600
    max_seconds: float = 60.0
    max_total_seconds: float = 120.0
    max_width: int = 1920
    max_height: int = 1920
    max_pixels: int = 1920 * 1080
    wall_seconds: float = 180.0


LIMITS = Limits()
_context = contextvars.ContextVar('poserun_compute_budget', default=None)


def unique_batch(names, limits=LIMITS):
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names):
        raise BudgetExceeded('请提供非空视频文件列表')
    # Raw list is bounded before dedupe, so repeated names cannot bloat requests.
    if len(names) > limits.max_batch:
        raise BudgetExceeded(f'批量最多 {limits.max_batch} 项（含重复项）')
    return list(dict.fromkeys(names))


class Budget:
    def __init__(self, limits=LIMITS, clock=time.monotonic):
        self.limits = limits
        self.clock = clock
        self.deadline = clock() + limits.wall_seconds
        self.frames = 0
        self.seconds = 0.0

    def check(self):
        if self.clock() >= self.deadline:
            raise BudgetExceeded('计算墙钟预算超时，拒绝发布该视频的部分结果')

    def frame(self, index, shape, fps):
        self.check()
        h, w = shape[:2]
        if (h > self.limits.max_height or w > self.limits.max_width
                or w*h > self.limits.max_pixels or min(w, h) <= 0):
            raise BudgetExceeded('视频分辨率超过计算预算')
        if index >= self.limits.max_frames or self.frames >= self.limits.max_total_frames:
            raise BudgetExceeded('视频或批量总帧数超过计算预算')
        valid_fps = isinstance(fps, (int, float)) and math.isfinite(fps) and fps > 0
        if valid_fps:
            if (index+1)/fps > self.limits.max_seconds or self.seconds + 1/fps > self.limits.max_total_seconds:
                raise BudgetExceeded('视频或批量总容器时长超过计算预算')
            self.seconds += 1/fps
        # Unknown fps stays unknown. Frames and wall time still bound the work.
        self.frames += 1


def current_budget():
    return _context.get()


def preflight(paths, budget):
    import cv2
    frame_total = 0
    seconds_total = 0.0
    for path in paths:
        budget.check()
        cap = cv2.VideoCapture(str(path))
        try:
            if not cap.isOpened():
                raise BudgetExceeded('无法打开视频')
            w, h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
            if all(math.isfinite(x) and x > 0 for x in (w, h)):
                if w > budget.limits.max_width or h > budget.limits.max_height or w*h > budget.limits.max_pixels:
                    raise BudgetExceeded('视频分辨率超过计算预算')
            count, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS)
            if math.isfinite(count) and count > 0:
                if count > budget.limits.max_frames:
                    raise BudgetExceeded('视频帧数超过计算预算')
                frame_total += count
                if math.isfinite(fps) and fps > 0:
                    seconds = count/fps
                    if seconds > budget.limits.max_seconds:
                        raise BudgetExceeded('视频时长超过计算预算')
                    seconds_total += seconds
        finally:
            cap.release()
    if frame_total > budget.limits.max_total_frames or seconds_total > budget.limits.max_total_seconds:
        raise BudgetExceeded('批量总帧数或总时长超过计算预算')
    budget.check()


def bounded_analysis(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        from flask import jsonify
        path = os.environ.get('POSERUN_ANALYSIS_LOCK', 'data/.analysis.lock')
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        f = open(path, 'a+b')
        try:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return jsonify(error='已有分析任务运行，请稍后重试', error_code='compute_busy'), 429
            token = _context.set(Budget())
            try:
                from worker_supervisor import execute_request
                return execute_request(fn)
            except BudgetExceeded as exc:
                return jsonify(error=str(exc), error_code='compute_budget_exceeded'), 422
            finally:
                _context.reset(token)
        finally:
            f.close()
    return wrapped
