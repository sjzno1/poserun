"""
PoseRun Analyzer - 姿态分析核心模块
基于 YOLO11n-Pose 的关键点检测 + 确定性因果 EMA 时序平滑
支持关键帧标注、历史对比等高级功能

后端说明:
  - 关键点检测: YOLO11n-Pose (COCO 17 点)
  - 时序平滑: 确定性因果 EMA (类名 LSTMSmoother 保留以兼容 API; 仅在显式连续 frame_index/fps 下启用)
  - 指标计算: 保持原有几何算法 (膝盖角/小腿角/跨步/对称性)
"""

import cv2
import numpy as np
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from pathlib import Path
import os
import math
import json
from datetime import datetime

try:
    import cn_font
except ImportError:
    cn_font = None

# 最近一次 PoseAnalyzer 使用的后端（供 /api/health 展示）
LAST_POSE_BACKEND = 'not_loaded'

# YOLO11n-Pose 17 点 landmark 名称表 (COCO 标准布局)
YOLO_POSE_LANDMARK_NAMES = [
    'NOSE',                    # 0
    'LEFT_EYE',                # 1
    'RIGHT_EYE',               # 2
    'LEFT_EAR',                # 3
    'RIGHT_EAR',               # 4
    'LEFT_SHOULDER',           # 5
    'RIGHT_SHOULDER',          # 6
    'LEFT_ELBOW',              # 7
    'RIGHT_ELBOW',             # 8
    'LEFT_WRIST',              # 9
    'RIGHT_WRIST',             # 10
    'LEFT_HIP',                # 11
    'RIGHT_HIP',               # 12
    'LEFT_KNEE',               # 13
    'RIGHT_KNEE',              # 14
    'LEFT_ANKLE',              # 15
    'RIGHT_ANKLE',             # 16
]

# YOLO 17 点 → MediaPipe 33 点映射 (用于兼容原有指标计算代码)
# MediaPipe 33 点中我们只用到: SHOULDER(11,12), ELBOW(13,14), WRIST(15,16),
# HIP(23,24), KNEE(25,26), ANKLE(27,28)
YOLO_TO_MEDIAPIPE_MAP = {
    0: None,      # NOSE → 无对应 (MediaPipe NOSE=0)
    1: 2,         # LEFT_EYE → LEFT_EYE(2)
    2: 5,         # RIGHT_EYE → RIGHT_EYE(5)
    3: 7,         # LEFT_EAR → LEFT_EAR(7)
    4: 8,         # RIGHT_EAR → RIGHT_EAR(8)
    5: 11,        # LEFT_SHOULDER → LEFT_SHOULDER(11)
    6: 12,        # RIGHT_SHOULDER → RIGHT_SHOULDER(12)
    7: 13,        # LEFT_ELBOW → LEFT_ELBOW(13)
    8: 14,        # RIGHT_ELBOW → RIGHT_ELBOW(14)
    9: 15,        # LEFT_WRIST → LEFT_WRIST(15)
    10: 16,       # RIGHT_WRIST → RIGHT_WRIST(16)
    11: 23,       # LEFT_HIP → LEFT_HIP(23)
    12: 24,       # RIGHT_HIP → RIGHT_HIP(24)
    13: 25,       # LEFT_KNEE → LEFT_KNEE(25)
    14: 26,       # RIGHT_KNEE → RIGHT_KNEE(26)
    15: 27,       # LEFT_ANKLE → LEFT_ANKLE(27)
    16: 28,       # RIGHT_ANKLE → RIGHT_ANKLE(28)
}

# MediaPipe 33 点名称表 (保持兼容, 用于 Landmark 对象)
POSE_LANDMARK_NAMES = [
    'NOSE', 'LEFT_EYE_INNER', 'LEFT_EYE', 'LEFT_EYE_OUTER', 'RIGHT_EYE_INNER',
    'RIGHT_EYE', 'RIGHT_EYE_OUTER', 'LEFT_EAR', 'RIGHT_EAR', 'MOUTH_LEFT',
    'MOUTH_RIGHT', 'LEFT_SHOULDER', 'RIGHT_SHOULDER', 'LEFT_ELBOW', 'RIGHT_ELBOW',
    'LEFT_WRIST', 'RIGHT_WRIST', 'LEFT_PINKY', 'RIGHT_PINKY', 'LEFT_INDEX',
    'RIGHT_INDEX', 'LEFT_THUMB', 'RIGHT_THUMB', 'LEFT_HIP', 'RIGHT_HIP',
    'LEFT_KNEE', 'RIGHT_KNEE', 'LEFT_ANKLE', 'RIGHT_ANKLE', 'LEFT_HEEL',
    'RIGHT_HEEL', 'LEFT_FOOT_INDEX', 'RIGHT_FOOT_INDEX',
]


@dataclass
class Landmark:
    """姿态关键点 (兼容 MediaPipe 33 点格式)"""
    name: str
    x: float  # 归一化坐标 [0, 1]
    y: float  # 归一化坐标 [0, 1]
    z: float  # 深度信息 (YOLO 不提供, 设为 0)
    visibility: float = 1.0  # 可见度置信度 [0, 1]

    def to_pixel(self, width: int, height: int) -> Tuple[int, int]:
        """转换为像素坐标"""
        return (int(self.x * width), int(self.y * height))


class LSTMSmoother:
    """确定性因果 EMA 平滑器（类名保留以兼容既有调用方 API）。

    旧实现是随机初始化、无训练权重的 LSTM：恒定点会被随机权重拖动，
    跨实例输出不可复现，不构成平滑器。现改为确定性因果指数滑动平均：
      - float64 计算，x/y 独立，无随机数、无训练权重；
      - 恒定输入 → 恒定输出（EMA 不动点，显式短路保证逐位相等）；
      - 同输入同输出，跨实例一致；
      - 时间平滑仅在显式且连续的 frame_index/fps 下启用：
        缺口/重复/回退/fps 变化/缺少时间上下文 → 重置状态并直通原始点。

    hidden_size/num_steps 为旧 LSTM 遗留参数，仅保持构造签名兼容，不再使用。
    """

    ALPHA = 0.5  # 固定确定性权重（新观测占比），不依赖训练或随机初始化

    def __init__(self, hidden_size: int = 32, num_steps: int = 5):
        self.hidden_size = hidden_size
        self.num_steps = num_steps
        self._state: Optional[np.ndarray] = None
        self._last_frame_index: Optional[int] = None
        self._last_fps: Optional[float] = None

    @staticmethod
    def _valid_time(frame_index, fps) -> bool:
        if frame_index is None or fps is None:
            return False
        if isinstance(frame_index, bool) or not isinstance(frame_index, (int, np.integer)):
            return False
        if isinstance(fps, bool) or not isinstance(fps, (int, float, np.number)):
            return False
        return frame_index >= 0 and math.isfinite(float(fps)) and fps > 0

    def add(self, point: np.ndarray, frame_index=None, fps=None) -> np.ndarray:
        """加入一个观测点。显式连续 frame_index/fps 时做因果 EMA，否则直通。"""
        p = np.asarray(point, dtype=np.float64).reshape(2).copy()
        valid_time = self._valid_time(frame_index, fps)
        contiguous = (
            valid_time
            and self._state is not None
            and self._last_fps == float(fps)
            and frame_index == self._last_frame_index + 1
        )
        if contiguous:
            if not np.array_equal(p, self._state):
                self._state = self.ALPHA * p + (1.0 - self.ALPHA) * self._state
        else:
            # 段首/时间缺口/无时间上下文：不混合历史，从原始点重新开始。
            self._state = p.copy()
        if valid_time:
            self._last_frame_index = int(frame_index)
            self._last_fps = float(fps)
        else:
            self._last_frame_index = None
            self._last_fps = None
        return self._state.copy()

    def reset(self):
        """重置时间状态。"""
        self._state = None
        self._last_frame_index = None
        self._last_fps = None



class KeyFrameAnalyzer:
    """关键帧提取与标注分析器"""

    STANCE_PHASES = ['heel_strike', 'mid_stance', 'toe_off']
    SWING_PHASES = ['initial_swinger', 'mid_swing', 'terminal_swing']

    def __init__(self, frame_width: int = 1920, frame_height: int = 1080):
        self.frame_width = frame_width
        self.frame_height = frame_height

    def extract_key_frames(self, video_path: str, num_frames: int = 5) -> List[Dict]:
        """提取关键帧（均匀分布 + 动作变化最大帧）"""
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if total_frames == 0:
            return []

        uniform_indices = [
            int(i * total_frames / num_frames)
            for i in range(num_frames)
        ]

        change_indices = self._find_action_change_frames(cap, total_frames, num_frames)

        selected_indices = sorted(set(uniform_indices) | set(change_indices))
        selected_indices = selected_indices[:num_frames]

        key_frames = []
        for idx in selected_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                key_frames.append({
                    'frame_index': idx,
                    'frame_number': len(key_frames),
                    'frame_data': frame.copy(),
                    'timestamp': self._frame_to_timestamp(cap, idx)
                })

        cap.release()
        return key_frames

    def _find_action_change_frames(self, cap, total_frames: int, num_frames: int) -> List[int]:
        """基于姿态变化查找关键帧"""
        change_indices = []
        prev_landmarks = None
        last_check = 0
        check_interval = max(1, total_frames // 50)

        for frame_idx in range(total_frames):
            ret, frame = cap.read()
            if not ret:
                continue

            if frame_idx - last_check < check_interval:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if prev_landmarks is not None:
                prev_gray = prev_landmarks['gray']
                diff = cv2.absdiff(gray, prev_gray)
                if np.mean(diff) > 30:
                    change_indices.append(frame_idx)

            prev_landmarks = {'gray': gray}
            last_check = frame_idx

            if len(change_indices) >= num_frames:
                break

        return change_indices

    def _frame_to_timestamp(self, cap, frame_idx: int) -> str:
        """帧索引转时间戳"""
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps > 0:
            seconds = frame_idx / fps
            return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}.{int((seconds % 1) * 100):02d}"
        return "00:00.00"

    PHASE_LABELS = {
        'heel_strike': '脚跟触地',
        'mid_stance': '承重中期',
        'toe_off': '蹬离前',
        'swing': '摆动期',
    }

    def draw_pose_annotations(self, frame: np.ndarray, landmarks: Optional[List[Landmark]] = None,
                              metrics: Optional[Dict] = None) -> np.ndarray:
        """在帧上绘制姿态标注（骨骼 + 关键点 + 指标面板）"""
        annotated = frame.copy()

        if landmarks is None:
            return annotated

        points = {lm.name: lm for lm in landmarks}
        height, width = annotated.shape[:2]

        # 骨骼连接 (使用 MediaPipe 33 点名称)
        connections = [
            ('LEFT_SHOULDER', 'RIGHT_SHOULDER'),
            ('LEFT_SHOULDER', 'LEFT_ELBOW'),
            ('LEFT_ELBOW', 'LEFT_WRIST'),
            ('RIGHT_SHOULDER', 'RIGHT_ELBOW'),
            ('RIGHT_ELBOW', 'RIGHT_WRIST'),
            ('LEFT_HIP', 'RIGHT_HIP'),
            ('LEFT_SHOULDER', 'LEFT_HIP'),
            ('RIGHT_SHOULDER', 'RIGHT_HIP'),
            ('LEFT_HIP', 'LEFT_KNEE'),
            ('LEFT_KNEE', 'LEFT_ANKLE'),
            ('RIGHT_HIP', 'RIGHT_KNEE'),
            ('RIGHT_KNEE', 'RIGHT_ANKLE'),
        ]

        for start, end in connections:
            if start in points and end in points:
                p1 = points[start].to_pixel(width, height)
                p2 = points[end].to_pixel(width, height)
                cv2.line(annotated, p1, p2, (0, 255, 0), 2)

        for lm in landmarks:
            px, py = lm.to_pixel(width, height)
            cv2.circle(annotated, (px, py), 4, (255, 0, 0), -1)

        if metrics:
            phase = metrics.get('running_phase', '')
            phase_cn = self.PHASE_LABELS.get(phase, phase)
            stride_cn = metrics.get('stride_analysis', {}).get('stride_level', '') or 'N/A'
            lines = [
                f"阶段: {phase_cn}",
                f"膝盖: {metrics.get('knee_flexion', 0):.0f}°",
                f"小腿: {metrics.get('shin_angle', 0):.0f}°",
                f"跨步: {stride_cn}",
            ]
            use_cn = cn_font is not None and cn_font.has_cn_font()
            if use_cn:
                font_size = 22
                panel_w = max(cn_font.text_size_cn(t, font_size)[0] for t in lines) + 24
                line_h = font_size + 8
            else:
                lines = [
                    f"Phase: {phase}",
                    f"Knee: {metrics.get('knee_flexion', 0):.0f} deg",
                    f"Shin: {metrics.get('shin_angle', 0):.0f} deg",
                    f"Stride: {stride_cn}",
                ]
                font_size, panel_w, line_h = 20, 220, 26
            panel_h = line_h * (len(lines) + 1) + 10
            x0, y0 = width - panel_w - 12, 12
            cv2.rectangle(annotated, (x0, y0), (x0 + panel_w, y0 + panel_h),
                          (0, 0, 0), -1)
            cv2.rectangle(annotated, (x0, y0), (x0 + panel_w, y0 + panel_h),
                          (0, 255, 255), 2)
            for i, line in enumerate(lines):
                ty = y0 + 22 + i * line_h
                if use_cn:
                    cn_font.put_text_cn(annotated, line, (x0 + 10, ty - font_size + 4),
                                         (0, 255, 255), font_size)
                else:
                    cv2.putText(annotated, line, (x0 + 8, ty),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

        if cn_font is not None and cn_font.has_cn_font():
            cn_font.put_text_cn(annotated, f"关键点: {len(landmarks)}", (10, 12),
                                (0, 255, 255), 22)
        else:
            cv2.putText(annotated, f"Points: {len(landmarks)}",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

        return annotated

    def save_annotations(self, output_dir: str, key_frames: List[Dict],
                        analysis_results: List[Dict]):
        """保存标注后的关键帧图片（含指标面板）"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        for i, kf in enumerate(key_frames):
            frame_data = kf['frame_data']
            related_result = self._find_related_result(analysis_results, kf['frame_index'])

            landmarks = related_result.get('landmarks', []) if related_result else []
            metrics = related_result.get('metrics', related_result) if related_result else None
            annotated = self.draw_pose_annotations(frame_data, landmarks, metrics)

            output_file = output_path / f"keyframe_{i+1:02d}.jpg"
            cv2.imwrite(str(output_file), annotated)

            info_file = output_path / f"keyframe_{i+1:02d}_info.json"
            info = {
                'frame_index': kf['frame_index'],
                'timestamp': kf['timestamp'],
                'frame_number': kf['frame_number'],
                'metrics': related_result if related_result else {}
            }
            with open(info_file, 'w', encoding='utf-8') as f:
                json.dump(info, f, ensure_ascii=False, indent=2, default=str)

        return str(output_path)

    def _find_related_result(self, analysis_results: List[Dict], frame_index: int) -> Optional[Dict]:
        """查找与关键帧最接近的分析结果"""
        if not analysis_results:
            return None

        if frame_index <= 0:
            return analysis_results[0]

        for result in reversed(analysis_results):
            if result.get('frame_index', result.get('frame_number', 0)) <= frame_index:
                return result

        return analysis_results[-1]


class PoseAnalyzer:
    """姿态分析器 - YOLO11n-Pose + LSTM 平滑

    后端：
      - 关键点检测: YOLO11n-Pose (COCO 17 点)
      - 时序平滑: LSTM (抑制逐帧抖动)
      - 指标计算: 保持原有几何算法
    """

    # YOLO 模型路径
    MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'data', 'models', 'yolo11n-pose.pt')

    def __init__(self):
        self.frame_analyzer = KeyFrameAnalyzer()
        self._yolo_model = None
        self._smoothers: Dict[str, LSTMSmoother] = {}  # 每个关键点一个平滑器
        self.backend = self._init_backend()

    def _init_backend(self):
        """初始化 YOLO11n-Pose 后端"""
        global LAST_POSE_BACKEND
        try:
            if not os.path.exists(self.MODEL_PATH):
                raise FileNotFoundError(self.MODEL_PATH)

            from ultralytics import YOLO
            self._yolo_model = YOLO(self.MODEL_PATH)
            LAST_POSE_BACKEND = 'yolo_lstm'
            return 'yolo_lstm'
        except Exception as e:
            print(f'[pose] YOLO11n-Pose 不可用（{e}）')
            self._yolo_model = None
            LAST_POSE_BACKEND = 'unavailable'
            return 'unavailable'

    def _detect_landmarks(self, frame: np.ndarray):
        """YOLO 关键点检测

        Args:
            frame: BGR 图像 (H, W, 3)

        Returns:
            (landmarks: List[Landmark], frame_index: int)
            未检测到人体返回 (None, 0)
        """
        if self._yolo_model is None:
            return None, 0

        # YOLO 预测 (返回 list[Results])
        # 使用较低的置信度阈值以检测卡通/合成图像中的人体
        # 注意: 对于真实跑步视频, 建议使用 conf=0.5 以获得更准确的结果
        results = self._yolo_model.predict(frame, conf=0.05, verbose=False)

        if not results or len(results) == 0:
            return None, 0

        first_result = results[0]  # type: ignore[index]
        if first_result.keypoints is None or len(first_result.keypoints) == 0:  # type: ignore[union-attr]
            return None, 0

        # 取第一个检测到的人
        kpts = first_result.keypoints[0]  # type: ignore[index]
        xy = kpts.xy[0]  # [17, 2]
        conf = kpts.conf[0] if kpts.conf is not None else np.ones(17)

        h, w = frame.shape[:2]
        landmarks = []

        for i in range(17):
            # YOLO 坐标是像素坐标, 需要归一化
            x_norm = float(xy[i][0]) / w
            y_norm = float(xy[i][1]) / h

            # 使用 YOLO 原始名称
            name = YOLO_POSE_LANDMARK_NAMES[i]

            landmarks.append(Landmark(
                name=name,
                x=x_norm,
                y=y_norm,
                z=0.0,  # YOLO 不提供深度
                visibility=float(conf[i]) if conf[i] is not None else 1.0,
            ))

        return landmarks, 0

    def _smooth_landmarks(self, landmarks: List[Landmark], *,
                          frame_index=None, fps=None) -> List[Landmark]:
        """确定性因果 EMA 平滑关键点序列。

        对每个关键点维护一个 LSTMSmoother；仅在显式连续 frame_index/fps
        下启用时间平滑，无时间上下文或时间缺口时直通原始点，不混合历史。
        """
        if not landmarks:
            return landmarks

        smoothed = []
        for lm in landmarks:
            # 每个关键点名称对应一个平滑器
            if lm.name not in self._smoothers:
                self._smoothers[lm.name] = LSTMSmoother(hidden_size=32, num_steps=5)

            smoother = self._smoothers[lm.name]
            point = np.array([lm.x, lm.y], dtype=np.float32)
            smoothed_point = smoother.add(point, frame_index=frame_index, fps=fps)

            smoothed.append(Landmark(
                name=lm.name,
                x=float(smoothed_point[0]),
                y=float(smoothed_point[1]),
                z=lm.z,
                visibility=lm.visibility,
            ))

        return smoothed

    def _reset_temporal(self):
        """视频切换/检测失败时清空时间状态，避免跨段混合。"""
        self._smoothers = {}

    def analyze_frame(self, frame: np.ndarray, *, frame_index=None, fps=None) -> Optional[Dict]:
        """分析单帧图像：关键点 + 确定性因果 EMA 平滑（需显式连续时间上下文）+ 指标"""
        landmarks, _ = self._detect_landmarks(frame)
        if landmarks is None:
            self._reset_temporal()
            return None

        # 确定性因果 EMA 平滑（仅显式连续 frame_index/fps）
        landmarks = self._smooth_landmarks(landmarks, frame_index=frame_index, fps=fps)

        analysis = self._calculate_metrics(landmarks, frame.shape)
        analysis['landmarks'] = landmarks
        analysis['frame_index'] = frame_index
        analysis['temporal_smoothing'] = (
            'contiguous_index_ema'
            if frame_index is not None and fps is not None
            else 'disabled_no_time_metadata'
        )
        return analysis

    def _calculate_metrics(self, landmarks: List[Landmark], frame_shape: Tuple) -> Dict:
        """计算关键跑步指标 (兼容 YOLO 17 点 + MediaPipe 33 点名称)"""
        width, height = frame_shape[1], frame_shape[0]
        self._metric_shape = frame_shape

        points = {lm.name: lm for lm in landmarks}

        metrics = {}

        # 1. 膝盖弯曲角度
        if 'LEFT_HIP' in points and 'LEFT_KNEE' in points and 'LEFT_ANKLE' in points:
            left_knee_angle = self._calculate_angle(
                points['LEFT_HIP'], points['LEFT_KNEE'], points['LEFT_ANKLE']
            )
        else:
            left_knee_angle = 0.0

        if 'RIGHT_HIP' in points and 'RIGHT_KNEE' in points and 'RIGHT_ANKLE' in points:
            right_knee_angle = self._calculate_angle(
                points['RIGHT_HIP'], points['RIGHT_KNEE'], points['RIGHT_ANKLE']
            )
        else:
            right_knee_angle = 0.0

        metrics['left_knee_angle'] = left_knee_angle
        metrics['right_knee_angle'] = right_knee_angle
        metrics['knee_flexion'] = (left_knee_angle + right_knee_angle) / 2

        # 2. 小腿角度
        if 'LEFT_KNEE' in points and 'LEFT_ANKLE' in points:
            left_shin_angle = self._calculate_angle_with_ground(
                points['LEFT_KNEE'], points['LEFT_ANKLE'], width, height
            )
        else:
            left_shin_angle = 90.0

        if 'RIGHT_KNEE' in points and 'RIGHT_ANKLE' in points:
            right_shin_angle = self._calculate_angle_with_ground(
                points['RIGHT_KNEE'], points['RIGHT_ANKLE'], width, height
            )
        else:
            right_shin_angle = 90.0

        metrics['left_shin_angle'] = left_shin_angle
        metrics['right_shin_angle'] = right_shin_angle
        metrics['shin_angle'] = (left_shin_angle + right_shin_angle) / 2

        # 带符号小腿角
        if 'LEFT_KNEE' in points and 'LEFT_ANKLE' in points:
            metrics['left_shin_signed'] = self._calculate_shin_angle_signed(
                points['LEFT_KNEE'], points['LEFT_ANKLE'], width, height
            )
        else:
            metrics['left_shin_signed'] = 0.0

        if 'RIGHT_KNEE' in points and 'RIGHT_ANKLE' in points:
            metrics['right_shin_signed'] = self._calculate_shin_angle_signed(
                points['RIGHT_KNEE'], points['RIGHT_ANKLE'], width, height
            )
        else:
            metrics['right_shin_signed'] = 0.0

        # 3. 跨步分析
        metrics['stride_analysis'] = self._analyze_stride(points, width)

        # 画布尺寸（供 Gait 等下游做像素几何；与 CalibratedPoseAnalyzer 一致）
        metrics['frame_width'] = width
        metrics['frame_height'] = height

        # 4. 身体前倾角度
        metrics['body_leang'] = self._calculate_body_leang(points)

        # 5. 身体框架对称性
        metrics['symmetry'] = self._calculate_symmetry(points)

        # 6. 跑步阶段识别
        metrics['running_phase'] = self._detect_running_phase(points, metrics)

        return metrics

    def _detect_running_phase(self, points: Dict, metrics: Dict) -> str:
        """基于小腿角度与膝盖弯曲粗略识别跑步阶段"""
        shin = metrics.get('shin_angle', 90)
        knee = metrics.get('knee_flexion', 180)

        if shin >= 70 and knee <= 165:
            if shin < 78:
                return 'heel_strike'
            if knee < 150:
                return 'mid_stance'
            return 'toe_off'
        return 'swing'

    def _calculate_angle(self, a: Landmark, b: Landmark, c: Landmark) -> float:
        """计算三点角度 (a-b-c)，浮点像素几何 (x*W, y*H)。

        归一化 x/W、y/H 直接做欧氏运算会随画幅纵横比失真；
        恢复浮点像素（不做整数截断）后角度与画幅无关。
        """
        shape = getattr(self, '_metric_shape', None)
        if shape is None or len(shape) < 2:
            return 0.0
        height, width = shape[0], shape[1]
        va = np.array([(a.x - b.x) * width, (a.y - b.y) * height], dtype=np.float64)
        vc = np.array([(c.x - b.x) * width, (c.y - b.y) * height], dtype=np.float64)

        dot_product = np.dot(va, vc)
        norm_a = np.linalg.norm(va)
        norm_c = np.linalg.norm(vc)

        if norm_a == 0 or norm_c == 0:
            return 0.0

        cos_angle = dot_product / (norm_a * norm_c)
        cos_angle = np.clip(cos_angle, -1.0, 1.0)

        return math.degrees(math.acos(cos_angle))

    def _calculate_angle_with_ground(self, knee: Landmark, ankle: Landmark, width: int, height: int) -> float:
        """计算小腿与地面夹角"""
        # 浮点像素（避免 to_pixel 的整数截断）
        dx = (ankle.x - knee.x) * width
        dy = (ankle.y - knee.y) * height

        if dx == 0 and dy == 0:
            return 0.0

        return math.degrees(math.atan2(abs(dy), abs(dx)))

    def _calculate_shin_angle_signed(self, knee: Landmark, ankle: Landmark, width: int, height: int) -> float:
        """带符号的小腿与地面夹角"""
        # 浮点像素（避免 to_pixel 的整数截断）
        dx = (ankle.x - knee.x) * width
        dy = (ankle.y - knee.y) * height
        if dx == 0 and dy == 0:
            return 0.0
        mag = math.degrees(math.atan2(abs(dy), abs(dx)))
        sign = 1.0 if dx < 0 else -1.0
        return sign * mag

    def _analyze_stride(self, points: Dict, width: int) -> Dict:
        """分析跨步情况"""
        if 'LEFT_HIP' not in points or 'RIGHT_HIP' not in points:
            return {'stride_distance': 0, 'stride_level': '未知', 'center_of_mass_x': 0, 'landing_foot_x': 0}

        center_x = (points['LEFT_HIP'].x + points['RIGHT_HIP'].x) / 2

        if 'LEFT_ANKLE' not in points or 'RIGHT_ANKLE' not in points:
            return {'stride_distance': 0, 'stride_level': '未知', 'center_of_mass_x': center_x, 'landing_foot_x': center_x}

        left_ankle_x = points['LEFT_ANKLE'].x
        right_ankle_x = points['RIGHT_ANKLE'].x

        forward_ankle_x = min(left_ankle_x, right_ankle_x)

        stride_distance = abs(forward_ankle_x - center_x)
        stride_level = '轻度跨步' if stride_distance < 0.05 else '中度跨步' if stride_distance < 0.1 else '严重跨步'

        return {
            'stride_distance': stride_distance,
            'stride_level': stride_level,
            'center_of_mass_x': center_x,
            'landing_foot_x': forward_ankle_x
        }

    def _calculate_body_leang(self, points: Dict) -> float:
        """身体前倾角：肩-髋连线与竖直方向的夹角（竖直=0°），浮点像素几何。

        旧实现 atan2(归一化水平偏移, 0.5) 是魔法常数，随画幅纵横比失真。
        """
        if 'LEFT_HIP' not in points or 'RIGHT_HIP' not in points or \
           'LEFT_SHOULDER' not in points or 'RIGHT_SHOULDER' not in points:
            return 0.0
        shape = getattr(self, '_metric_shape', None)
        if shape is None or len(shape) < 2:
            return 0.0
        height, width = shape[0], shape[1]

        hip_x = (points['LEFT_HIP'].x + points['RIGHT_HIP'].x) / 2 * width
        hip_y = (points['LEFT_HIP'].y + points['RIGHT_HIP'].y) / 2 * height
        shoulder_x = (points['LEFT_SHOULDER'].x + points['RIGHT_SHOULDER'].x) / 2 * width
        shoulder_y = (points['LEFT_SHOULDER'].y + points['RIGHT_SHOULDER'].y) / 2 * height

        dx = abs(shoulder_x - hip_x)
        dy = abs(hip_y - shoulder_y)
        if dx == 0 and dy == 0:
            return 0.0
        return math.degrees(math.atan2(dx, dy))

    def _calculate_symmetry(self, points: Dict) -> Dict:
        """计算身体框架对称性"""
        if 'LEFT_KNEE' not in points or 'RIGHT_KNEE' not in points or \
           'LEFT_SHOULDER' not in points or 'RIGHT_SHOULDER' not in points:
            return {'knee_distance': 0, 'shoulder_distance': 0, 'symmetry_score': 100, 'symmetry_level': '良好'}

        knee_distance = abs(points['LEFT_KNEE'].x - points['RIGHT_KNEE'].x)
        shoulder_distance = abs(points['LEFT_SHOULDER'].x - points['RIGHT_SHOULDER'].x)

        symmetry_score = 100 - (knee_distance * 100)

        return {
            'knee_distance': knee_distance,
            'shoulder_distance': shoulder_distance,
            'symmetry_score': max(0, min(100, symmetry_score)),
            'symmetry_level': '良好' if symmetry_score > 80 else '一般' if symmetry_score > 60 else '较差'
        }

    def process_video(self, video_path: str) -> List[Dict]:
        """处理整个视频，返回每一帧的分析结果"""
        cap = cv2.VideoCapture(video_path)

        if not cap.isOpened():
            raise ValueError(f"无法打开视频文件: {video_path}")

        # 重置时序平滑状态
        self._reset_temporal()

        results = []
        frame_count = 0
        fps = cap.get(cv2.CAP_PROP_FPS)
        fps = float(fps) if fps and math.isfinite(fps) and fps > 0 else None

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            analysis = self.analyze_frame(frame, frame_index=frame_count, fps=fps)
            if analysis:
                analysis['frame_number'] = frame_count
                analysis['frame_index'] = frame_count
                results.append(analysis)

            frame_count += 1

            if frame_count % 10 != 0:
                continue

        cap.release()
        return results

    def process_video_with_keyframes(self, video_path: str,
                                     num_keyframes: int = 5) -> Dict:
        """处理视频并提取关键帧（增强版）"""
        results = self.process_video(video_path)

        key_frames = self.frame_analyzer.extract_key_frames(video_path, num_keyframes)

        annotations_dir = os.path.join(os.path.dirname(video_path), 'annotations')
        self.frame_analyzer.save_annotations(annotations_dir, key_frames, results)

        return {
            'analysis_results': results,
            'key_frames': key_frames,
            'annotations_dir': annotations_dir
        }

    def compare_historical_data(self, video_path: str,
                               history_db: str = 'history.json') -> Dict:
        """对比历史分析数据"""
        current_results = self.process_video(video_path)

        history_file = Path(history_db)
        history = {}
        if history_file.exists():
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)

        record_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        current_metrics = self._calculate_comprehensive_metrics(current_results)

        history[record_id] = {
            'timestamp': datetime.now().isoformat(),
            'metrics': current_metrics
        }

        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2, default=str)

        return {
            'current': current_metrics,
            'history': history,
            'record_id': record_id
        }

    def _calculate_comprehensive_metrics(self, analysis_results: list) -> dict:
        """计算综合指标"""
        if not analysis_results:
            return {}

        avg_knee = np.mean([r.get('knee_flexion', 0) for r in analysis_results])
        avg_shin = np.mean([r.get('shin_angle', 0) for r in analysis_results])

        latest_stride = analysis_results[-1].get('stride_analysis', {})
        latest_symmetry = analysis_results[-1].get('symmetry', {})

        return {
            'avg_knee_flexion': avg_knee,
            'avg_shin_angle': avg_shin,
            'stride_analysis': latest_stride,
            'symmetry': latest_symmetry
        }

    def close(self):
        """释放资源"""
        if self._yolo_model is not None:
            del self._yolo_model
            self._yolo_model = None
        self._smoothers = {}


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("用法: python pose_analyzer.py <视频文件路径>")
        print("可选: --keyframes <关键帧数> --history <历史记录文件>")
        sys.exit(1)

    video_path = sys.argv[1]
    keyframes = 5
    history_db = 'history.json'

    for arg in sys.argv[2:]:
        if arg == '--keyframes' and len(sys.argv) > sys.argv.index(arg) + 1:
            keyframes = int(sys.argv[sys.argv.index(arg) + 1])
        elif arg == '--history' and len(sys.argv) > sys.argv.index(arg) + 1:
            history_db = sys.argv[sys.argv.index(arg) + 1]

    analyzer = PoseAnalyzer()

    try:
        results = analyzer.process_video(video_path)
        print(f"处理完成! 共分析 {len(results)} 帧")

        if results:
            avg_knee = np.mean([r['knee_flexion'] for r in results])
            avg_shin = np.mean([r['shin_angle'] for r in results])
            print(f"平均膝盖弯曲角度: {avg_knee:.1f}°")
            print(f"平均小腿角度: {avg_shin:.1f}°")
            print(f"跨步情况: {results[-1]['stride_analysis']['stride_level']}")
            print(f"身体对称性: {results[-1]['symmetry']['symmetry_level']}")

        analyzer.compare_historical_data(video_path, history_db)
        print(f"历史数据已保存到: {history_db}")

    finally:
        analyzer.close()
