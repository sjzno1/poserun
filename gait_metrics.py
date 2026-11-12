"""
PoseRun Analyzer - 步态指标 + 视频质量检查（应用层封装）
- compute_gait(video_path)：跑一次姿态分析并计算步态指标（带内存缓存，避免重复跑视频）
- check_video_quality(video_path)：时长/帧数/有效帧比例/视角提示（借鉴同类项目）
"""

import os
import threading

from gait_analyzer import GaitAnalyzer

# 质量检查阈值
MIN_DURATION_SEC = 3.0
MAX_DURATION_SEC = 120.0
MIN_FRAMES = 20
MIN_VALID_FRAME_RATIO = 0.5

# 步态计算缓存：video_path -> {'fps':..., 'gait':...}（同一次会话内复用）
_gait_cache = {}
_cache_lock = threading.Lock()


def compute_gait(video_path, use_cache=True):
    """对视频计算步态指标。返回 {'gait': dict|None, 'fps': float, 'frames': int, 'duration': float}
    姿态识别失败或数据不足时 gait 为 None。"""
    if use_cache:
        with _cache_lock:
            hit = _gait_cache.get(video_path)
        if hit:
            return hit

    from pose_analyzer import PoseAnalyzer
    import cv2

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    duration = total_frames / fps if fps > 0 else 0.0

    gait = None
    frames_used = 0
    try:
        analyzer = PoseAnalyzer()
        try:
            results = analyzer.process_video(video_path)
        finally:
            analyzer.close()
        frames_used = len(results)
        if results:
            gait = GaitAnalyzer(fps=fps).analyze(results)
    except Exception as e:
        print(f'[gait] 步态计算失败: {e}')
        gait = None

    out = {'gait': gait, 'fps': round(fps, 1),
           'frames': frames_used, 'duration': round(duration, 1)}
    with _cache_lock:
        _gait_cache[video_path] = out
        # 限制缓存大小
        if len(_gait_cache) > 20:
            _gait_cache.pop(next(iter(_gait_cache)))
    return out


def check_video_quality(video_path):
    """Container-only preflight; never run the retired automatic person picker."""
    import cv2
    import math
    warnings = ['预检只检查容器时长和帧率，不验证人体数量、身份或姿态可测量性；分析仍每段仅分析一个连续主体，允许可区分背景人。']
    reasons, retake_tips = [], []
    result = {'ok': False, 'duration': None, 'fps': None, 'total_frames': None,
              'valid_frame_ratio': None, 'single_person_scene_verified': False,
              'preflight_scope': 'container_metadata_only', 'warnings': warnings,
              'reasons': reasons, 'retake_tips': retake_tips}
    cap = cv2.VideoCapture(video_path)
    try:
        if not cap.isOpened():
            reasons.append('无法打开视频，文件可能损坏或格式不支持')
            retake_tips.append('请重新录制或换一个单人视频文件')
            return result
        raw_fps, raw_count = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = float(raw_fps) if raw_fps is not None and not isinstance(raw_fps, bool) else float('nan')
        count = float(raw_count) if raw_count is not None and not isinstance(raw_count, bool) else float('nan')
        fps = fps if math.isfinite(fps) and fps > 0 else None
        count = int(count) if math.isfinite(count) and count > 0 and count.is_integer() else None
        duration = count / fps if count is not None and fps is not None else None
        if duration is not None and not math.isfinite(duration):
            duration = None
        result.update(fps=round(fps, 1) if fps is not None else None,
                      total_frames=count, duration=round(duration, 1) if duration is not None else None)
        if duration is None:
            reasons.append('容器帧率或帧数未知，无法预检时长')
            retake_tips.append('请检查文件或重新录制，不能用默认帧率补齐')
        elif duration < MIN_DURATION_SEC:
            reasons.append(f'视频时长过短（{duration:.1f}s < {MIN_DURATION_SEC}s）')
            retake_tips.append('请拍摄至少3秒的连续单人跑步视频')
        elif duration > MAX_DURATION_SEC:
            warnings.append(f'视频较长（{duration:.0f}s），逐帧处理可能耗时较长')
            retake_tips.append('建议上传10–30秒稳定、全身可见的单人视频')
        if count is not None and count < MIN_FRAMES:
            reasons.append(f'容器声明帧数过少（{count} < {MIN_FRAMES}）')
        if fps is not None and fps < 15:
            warnings.append(f'容器声明帧率偏低（{fps:.0f}fps），时间分辨率有限')
            retake_tips.append('建议使用30fps以上拍摄，但帧率不等于测量准确率')
        result['ok'] = not reasons
        return result
    finally:
        cap.release()
