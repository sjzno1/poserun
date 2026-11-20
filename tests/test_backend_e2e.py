"""端到端：合成跑步视频 → PoseAnalyzer(新后端) → 逐帧指标 → GaitAnalyzer
在容器内运行：python tests/test_backend_e2e.py

测量口径与测试分层（owner 决策 2026-11-17，经 NAS 容器内证伪后修正）：
- 先前「因果 EMA 滞后导致摆动丢失」的判断已被证伪：真实 process_video 的 EMA 路径
  小腿角 min 仍≈63°；把合成器自己画的关节像素直接喂角度函数则摆动≈30° 可达。
  真根因是 yolo11n-pose 对合成卡通图的踝关节定位误差(35–85px)，属**检测质量**问题，
  不是测量数学退化，也不是时间平滑问题。
- 因此本测试分两层：
  A) 测量数学 oracle（确定性、本地可跑、不依赖检测器）：用 fixture 的**已知真值关节**
     喂生产 _calculate_angle_with_ground，断言能复现生成器的支撑(60°)/摆动(30°)。
     这验证「角度计算」这一测量契约本身正确，与检测器质量解耦。
  B) 真实管线健康检查（需 NAS torch/YOLO）：process_video 真实推理，断言检测率、
     带符号小腿角/可见性/膝角范围契约、步态管线不崩溃。不再用依赖检测质量的
     「摆动角<=45°」门（该门在此合成 fixture 上不可达，是检测局限而非测量缺陷）。
"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import cv2

FPS = 30
N_FRAMES = 150  # 5 秒
W, H = 640, 480

# 跑步周期：170 步/分 → 单腿周期 0.706s
PERIOD = 60.0 / 170.0 * 2


def _leg(t, phase, support_shin=60.0, swing_shin=30.0):
    ph = (t / PERIOD + phase) % 1.0
    if ph < 0.6:
        return support_shin, support_shin
    return swing_shin, -swing_shin


def draw_runner(img, i):
    """画一个更写实的跑者（填充躯干/头/四肢），返回各关节像素坐标"""
    t = i / FPS
    cx = 320
    hip_y = 200 + int(10 * math.sin(2 * math.pi * t / PERIOD))  # 髋部上下振荡
    sh_y = 120  # 肩高
    sh_x = cx - 12  # 肩（略前倾）
    head = (sh_x - 4, sh_y - 34)

    def leg(hip_pt, phase):
        shin, signed = _leg(t, phase)
        L = 100.0
        rad = math.radians(abs(shin))
        dx = L * math.cos(rad) * (-1 if signed > 0 else 1)
        dy = L * math.sin(rad)
        knee = (hip_pt[0] + int(dx * 0.5), hip_pt[1] + int(dy * 0.5))
        ankle = (hip_pt[0] + int(dx), hip_pt[1] + int(dy))
        return knee, ankle

    lh, rh = (cx - 20, hip_y), (cx + 20, hip_y)
    lk, la = leg(lh, 0.0)
    rk, ra = leg(rh, 0.5)
    # 手臂（摆动）
    arm_ph = (t / PERIOD) % 1.0
    sw = math.sin(2 * math.pi * arm_ph)
    l_elb = (sh_x - 18, sh_y + 34)
    l_wri = (sh_x - 18 + int(-24 * sw), sh_y + 34 + int(20 * sw))
    r_elb = (sh_x + 30, sh_y + 34)
    r_wri = (sh_x + 30 + int(24 * sw), sh_y + 34 + int(-20 * sw))

    # 躯干（填充四边形）
    torso = np.array([[sh_x - 22, sh_y], [sh_x + 30, sh_y],
                      [rh[0] + 16, hip_y + 8], [lh[0] - 16, hip_y + 8]], np.int32)
    cv2.fillPoly(img, [torso], (70, 110, 160))
    # 头
    cv2.circle(img, head, 20, (90, 140, 190), -1)
    # 四肢（粗线，圆头）
    cv2.line(img, (sh_x, sh_y + 6), lh, (70, 110, 160), 16, cv2.LINE_AA)   # 躯干-髋
    cv2.line(img, lh, lk, (60, 95, 140), 18, cv2.LINE_AA)
    cv2.line(img, lk, la, (60, 95, 140), 14, cv2.LINE_AA)
    cv2.line(img, rh, rk, (60, 95, 140), 18, cv2.LINE_AA)
    cv2.line(img, rk, ra, (60, 95, 140), 14, cv2.LINE_AA)
    # 手臂
    cv2.line(img, (sh_x, sh_y), l_elb, (80, 120, 170), 14, cv2.LINE_AA)
    cv2.line(img, l_elb, l_wri, (80, 120, 170), 12, cv2.LINE_AA)
    cv2.line(img, (sh_x + 30, sh_y), r_elb, (80, 120, 170), 14, cv2.LINE_AA)
    cv2.line(img, r_elb, r_wri, (80, 120, 170), 12, cv2.LINE_AA)
    return {'head': head, 'sh': (sh_x, sh_y), 'lh': lh, 'rh': rh,
            'lk': lk, 'rk': rk, 'la': la, 'ra': ra,
            'l_elb': l_elb, 'l_wri': l_wri, 'r_elb': r_elb, 'r_wri': r_wri}


def make_video(path):
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    vw = cv2.VideoWriter(path, fourcc, FPS, (W, H))
    for i in range(N_FRAMES):
        img = np.zeros((H, W, 3), np.uint8)
        img[:] = (40, 40, 40)
        cv2.line(img, (0, 430), (W, 430), (120, 120, 120), 2)  # 地面
        draw_runner(img, i)
        vw.write(img)
    vw.release()


def measurement_oracle():
    """A) 测量数学 oracle：用 fixture 已知真值关节验证生产角度函数。

    不加载检测器、不需要 torch/YOLO，本地确定性可跑。断言 _calculate_angle_with_ground
    能从真值关节复现生成器的支撑(60°)/摆动(30°)，证明测量契约本身正确。
    """
    from pose_analyzer import PoseAnalyzer, Landmark

    # _calculate_angle_with_ground 是纯函数（不引用 self）。用未初始化实例做未绑定调用，
    # 直接测生产实现，避免 PoseAnalyzer() 触发 YOLO 模型加载（oracle 不需要检测器）。
    angle_fn = PoseAnalyzer._calculate_angle_with_ground
    probe = object.__new__(PoseAnalyzer)  # 不跑 __init__，不加载模型
    tol = 3.0  # fixture 关节为整数像素，含 ~1–2° 取整误差
    support_hits = swing_hits = 0
    max_err = 0.0
    img = np.zeros((H, W, 3), np.uint8)
    for i in range(N_FRAMES):
        t = i / FPS
        joints = draw_runner(img, i)
        for knee_key, ankle_key, phase in (('lk', 'la', 0.0), ('rk', 'ra', 0.5)):
            intended = abs(_leg(t, phase)[0])
            kx, ky = joints[knee_key]
            ax, ay = joints[ankle_key]
            knee = Landmark(name='KNEE', x=kx / W, y=ky / H, z=0.0)
            ankle = Landmark(name='ANKLE', x=ax / W, y=ay / H, z=0.0)
            got = angle_fn(probe, knee, ankle, W, H)
            err = abs(got - intended)
            max_err = max(max_err, err)
            assert err <= tol, (
                f"测量数学 oracle 失败 frame={i} leg={knee_key}: "
                f"真值关节应得 {intended:.1f}°，实际 {got:.2f}°（误差 {err:.2f}° > {tol}°）")
            if intended >= 55:
                support_hits += 1
            elif intended <= 35:
                swing_hits += 1
    assert support_hits > 0 and swing_hits > 0, (
        f"oracle 未覆盖支撑/摆动两区间: support={support_hits} swing={swing_hits}")
    print(f"[oracle] 测量数学正确：支撑帧 {support_hits}、摆动帧 {swing_hits}，"
          f"最大角度误差 {max_err:.2f}°（容差 {tol}°）")
    return support_hits, swing_hits, max_err


def main():
    from pose_analyzer import PoseAnalyzer
    from gait_analyzer import GaitAnalyzer

    # A) 测量数学 oracle（确定性，不依赖检测器）
    measurement_oracle()

    # B) 真实管线健康检查（需 torch/YOLO；在 NAS 容器内跑）
    vid = os.path.join('/tmp', 'e2e_runner.mp4')
    make_video(vid)
    print(f"video: {vid} ({os.path.getsize(vid)} bytes)")

    pa = PoseAnalyzer()
    print(f"backend: {pa.backend}")
    assert pa.backend == 'yolo_lstm', f"应使用 YOLO-LSTM 后端，实际 {pa.backend}"

    results = pa.process_video(vid)  # 生产真实路径（含因果 EMA 时间平滑）
    pa.close()
    n_valid = len(results)
    print(f"valid frames: {n_valid}/{N_FRAMES} ({n_valid/N_FRAMES:.0%})")
    assert n_valid >= N_FRAMES * 0.3, f"人体检测率过低: {n_valid}/{N_FRAMES}"

    # 逐帧指标契约（真实管线输出）
    assert all('left_shin_signed' in r and 'right_shin_signed' in r for r in results), "缺少带符号小腿角"
    assert all(0.0 <= lm.visibility <= 1.0 for r in results for lm in r['landmarks']), "visibility 超出 [0,1]"
    knees = [r['knee_flexion'] for r in results]
    shins = [r['shin_angle'] for r in results]
    assert 0 <= min(knees) and max(knees) <= 180, "膝盖角度超出合理范围"
    assert 0 <= min(shins) and max(shins) <= 180, "小腿角超出合理范围"
    print(f"pipeline knee range: {min(knees):.0f}-{max(knees):.0f}  shin range: {min(shins):.0f}-{max(shins):.0f}")
    # 注：合成卡通 fixture 上 yolo11n-pose 踝定位误差大，摆动角(≈30°)在此不可达，
    # 属检测质量局限（已由 oracle 证明测量数学本身正确），不作为管线红灯。

    # 步态：合成卡通图步态检测精度有限，只要管线不崩溃即可（真实视频上正常工作）。
    g = GaitAnalyzer(fps=FPS).analyze(results)
    assert g is not None, "步态计算失败（帧数不足？）"
    print(f"gait: cadence={g['cadence']} steps={g['total_steps']} "
          f"symmetry={g['step_symmetry']} valid_ratio={g['valid_frame_ratio']}")
    assert g['valid_frame_ratio'] > 0, f"步态分析无有效帧: {g}"
    print(f"strike_posture: {g['strike_posture']}")

    print("\nE2E 通过 ✅  测量数学 oracle 正确 + 真实管线健康（后端=yolo_lstm，检测率 {:.0%}，步频 {:.1f}，步数 {}）".format(
        n_valid / N_FRAMES, g['cadence'], g['total_steps']))


if __name__ == '__main__':
    main()
