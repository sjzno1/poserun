"""远程帧批姿态检测客户端（方案B Phase2/3，DESIGN-FRAMES.md 权威规格）。

接法：NAS 本地 cv2 解码 → 无损压缩 raw BGR → 分批 POST /detect_frames 到 LAN
检测服务（6.4，钉 opencv 4.10.0.84 + torch2.8/ultralytics8.3.199，CPU）→ 取回
逐帧候选关键点 → NAS 本地跑与生产完全相同的主subject选择/平滑/测量。

默认关：POSERUN_REMOTE_DETECT_URL 未设/为空 → 纯本地路径，逐字节不变。
失败即整单失败（fail-closed）：任何远程错误都抛 RemoteDetectError，绝不静默
回退本地推理。

线协议（multipart/form-data）：
  meta  part: {video_sha256, height, width, fps|null, frame_indices, batch_id, codec}
  frames part: 按 frame_indices 顺序拼接的 raw BGR(uint8) 无损压缩字节
    codec=zlib: zlib.compress(拼接raw, 6) 单流
    codec=png : 每帧 cv2.imencode('.png')，4字节大端长度前缀 + PNG 字节
    codec=zstd: zstandard 单流（两侧需安装 zstandard，否则配置期拒绝）
响应 JSON：
  {frame_indices, pose_detected:[bool], landmarks:[每帧每候选17点{name,x,y,z,visibility}],
   xy_pixel:[每帧每候选17×2像素float], boxes:[每帧每候选xyxy像素float],
   meta:{predict_s, decompress_s, device, model_sha256, versions, ...}}
  x/y 归一化(x/w,y/h)，visibility=conf，与生产 _detect_landmarks 字段一致；
  xy_pixel/boxes 为像素坐标 float64 JSON 精确往返，保证 NAS 侧选择输入与本地
  float32→float64 提升比特级一致。

本模块导入期仅用 stdlib（numpy/cv2/zstandard 均按需懒加载），默认关时不改变
现网导入图。
"""
import hashlib
import http.client
import ipaddress
import json
import math
import os
import socket
import time
import urllib.parse
import uuid
import zlib

ERROR_KINDS = ('timeout', 'conn', 'auth', 'overload', 'bad_payload', 'count_mismatch')
CODECS = ('raw', 'zlib', 'png', 'zstd')
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_BATCH_SIZE = 40
# Phase4 设计修正（业主+主控核实）：NAS↔6.4 为 10G 链路（实测 241MB 上传
# 0.216s≈8.9Gbps），两端 SSD。压缩是负优化：281 帧原始 440MB 直传≈0.4s，
# 而 zlib 耗 NAS CPU 17.8s(level1)~28.2s(level6) 只省≈0.2s 传输。
# 故默认 raw 直传（identity 无损）；zlib/png/zstd 保留为窄带回退。
DEFAULT_CODEC = 'raw'
# raw 批大小钳制预留：multipart meta/boundary 开销余量（1MiB）。
RAW_BODY_MARGIN = 1024 * 1024
MAX_BODY_BYTES = 64 * 1024 * 1024  # 与收帧服务请求体上限一致
# Phase4 B 择优：NAS 实测 281 帧 level1 17.83s / level3 19.92s / level6 28.22s，
# 压缩比 1.786 / 1.825 / 1.825；速度优先且无损 → level1（解压与 level 无关）。
ZLIB_LEVEL = 1
ENV_URL = 'POSERUN_REMOTE_DETECT_URL'
ENV_TOKEN_FILE = 'POSERUN_REMOTE_DETECT_TOKEN_FILE'
ENV_TIMEOUT_S = 'POSERUN_REMOTE_DETECT_TIMEOUT_S'
ENV_BATCH_SIZE = 'POSERUN_REMOTE_DETECT_BATCH_SIZE'
ENV_CODEC = 'POSERUN_REMOTE_DETECT_CODEC'
ENV_PREFIX = 'POSERUN_REMOTE_DETECT_'


class RemoteDetectError(RuntimeError):
    """Fail-closed 远程检测错误；kind 用于分类报告，绝不触发本地回退。"""

    def __init__(self, kind, message):
        if kind not in ERROR_KINDS:
            raise ValueError('unknown remote error kind: %s' % kind)
        self.kind = kind
        super().__init__('[remote_detect:%s] %s' % (kind, message))


def _is_private_host(host):
    """只允许私网/环回字面量 IP，防止帧数据被发往公网。"""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


class RemoteConfig:
    def __init__(self, url='', token_file='', timeout_s=DEFAULT_TIMEOUT_S,
                 batch_size=DEFAULT_BATCH_SIZE, codec=DEFAULT_CODEC):
        self.url = (url or '').strip()
        self.token_file = (token_file or '').strip()
        self.timeout_s = float(timeout_s)
        self.batch_size = int(batch_size)
        self.codec = (codec or DEFAULT_CODEC).strip().lower()
        self.host = ''
        self.port = 8765
        self.token = ''
        if not self.url:
            return  # 默认关
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme != 'http':
            raise RemoteDetectError('bad_payload', '远程URL仅支持 http（LAN 明文+token）: %s' % parts.scheme)
        if parts.username or parts.password:
            raise RemoteDetectError('bad_payload', '远程URL不允许携带凭据')
        if parts.path not in ('', '/'):
            raise RemoteDetectError('bad_payload', '远程URL不允许路径前缀')
        if parts.query or parts.fragment:
            raise RemoteDetectError('bad_payload', '远程URL不允许 query/fragment')
        if not parts.hostname or not _is_private_host(parts.hostname):
            raise RemoteDetectError('bad_payload', '远程主机必须是私网/环回字面量IP')
        self.host = parts.hostname
        self.port = parts.port or 8765
        if not (0 < self.port < 65536):
            raise RemoteDetectError('bad_payload', '远程端口非法')
        if not math.isfinite(self.timeout_s) or not (1.0 <= self.timeout_s <= 600.0):
            raise RemoteDetectError('bad_payload', '超时需在 [1,600] 秒')
        if not (1 <= self.batch_size <= 512):
            raise RemoteDetectError('bad_payload', 'batch_size 需在 [1,512]')
        if self.codec not in CODECS:
            raise RemoteDetectError('bad_payload', '未知压缩 codec: %s' % self.codec)
        if self.codec == 'zstd':
            try:
                import zstandard  # noqa: F401
            except ImportError:
                raise RemoteDetectError('bad_payload', 'codec=zstd 需要 zstandard 包')
        if not self.token_file:
            raise RemoteDetectError('auth', '启用远程检测必须设置 %s' % ENV_TOKEN_FILE)
        try:
            with open(self.token_file, 'r', encoding='utf-8') as f:
                self.token = f.read().strip()
        except OSError as exc:
            raise RemoteDetectError('auth', '无法读取 token 文件: %s' % exc)
        if not self.token:
            raise RemoteDetectError('auth', 'token 文件为空')

    @property
    def enabled(self):
        return bool(self.url)


def config_from_env():
    def _num(env, default, cast):
        raw = os.environ.get(env, '').strip()
        if not raw:
            return default
        try:
            return cast(raw)
        except ValueError:
            raise RemoteDetectError('bad_payload', '%s 数值非法: %r' % (env, raw))
    return RemoteConfig(
        url=os.environ.get(ENV_URL, ''),
        token_file=os.environ.get(ENV_TOKEN_FILE, ''),
        timeout_s=_num(ENV_TIMEOUT_S, DEFAULT_TIMEOUT_S, float),
        batch_size=_num(ENV_BATCH_SIZE, DEFAULT_BATCH_SIZE, int),
        codec=os.environ.get(ENV_CODEC, DEFAULT_CODEC),
    )


# ---------------------------------------------------------------- 无损压缩
def compress_frames(frames, codec):
    """frames: 同形状 uint8 HxWx3 BGR 列表。返回 (压缩字节, 原始字节数)。"""
    if not frames:
        raise RemoteDetectError('bad_payload', '空帧批')
    if codec == 'raw':
        raw = bytearray()
        for f in frames:
            raw += f.tobytes()
        return bytes(raw), len(raw)
    if codec == 'zlib':
        raw = bytearray()
        for f in frames:
            raw += f.tobytes()
        return zlib.compress(bytes(raw), ZLIB_LEVEL), len(raw)
    if codec == 'png':
        import cv2
        out = bytearray()
        raw_len = 0
        for f in frames:
            ok, buf = cv2.imencode('.png', f)
            if not ok:
                raise RemoteDetectError('bad_payload', 'PNG 编码失败')
            b = buf.tobytes()
            out += len(b).to_bytes(4, 'big')
            out += b
            raw_len += f.nbytes
        return bytes(out), raw_len
    if codec == 'zstd':
        import zstandard
        raw = bytearray()
        for f in frames:
            raw += f.tobytes()
        return zstandard.ZstdCompressor(level=3).compress(bytes(raw)), len(raw)
    raise RemoteDetectError('bad_payload', '未知 codec: %s' % codec)


def decompress_frames(blob, codec, count, height, width):
    """还原 (count,height,width,3) uint8 数组；长度/形状不符即 bad_payload。"""
    import numpy as np
    expected = count * height * width * 3
    if codec == 'raw':
        raw = blob
    elif codec == 'zlib':
        try:
            d = zlib.decompressobj()
            raw = d.decompress(blob, expected + 1)
            if not d.eof or len(raw) != expected:
                raise RemoteDetectError('bad_payload',
                                        'zlib 解压字节数 %d != 期望 %d' % (len(raw), expected))
        except zlib.error as exc:
            raise RemoteDetectError('bad_payload', 'zlib 解压失败: %s' % exc)
    elif codec == 'png':
        import cv2
        frames = []
        i = 0
        for _ in range(count):
            if i + 4 > len(blob):
                raise RemoteDetectError('bad_payload', 'PNG 流截断')
            n = int.from_bytes(blob[i:i + 4], 'big')
            i += 4
            if i + n > len(blob):
                raise RemoteDetectError('bad_payload', 'PNG 帧截断')
            arr = np.frombuffer(blob[i:i + n], dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None or img.shape != (height, width, 3):
                raise RemoteDetectError('bad_payload', 'PNG 解码形状不符')
            frames.append(img)
            i += n
        if i != len(blob):
            raise RemoteDetectError('bad_payload', 'PNG 流有多余字节')
        return np.stack(frames)
    elif codec == 'zstd':
        import zstandard
        try:
            raw = zstandard.ZstdDecompressor().decompress(blob, max_output_size=expected)
        except Exception as exc:
            raise RemoteDetectError('bad_payload', 'zstd 解压失败: %s' % exc)
    else:
        raise RemoteDetectError('bad_payload', '未知 codec: %s' % codec)
    if len(raw) != expected:
        raise RemoteDetectError('bad_payload',
                                '解压字节数 %d != 期望 %d' % (len(raw), expected))
    return np.frombuffer(raw, dtype=np.uint8).reshape(count, height, width, 3).copy()


# ---------------------------------------------------------------- HTTP 客户端
def _build_multipart(meta, blob):
    boundary = '----poserunframes%s' % uuid.uuid4().hex
    meta_bytes = json.dumps(meta, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    body = bytearray()
    body += ('--%s\r\nContent-Disposition: form-data; name="meta"\r\n'
             'Content-Type: application/json\r\n\r\n' % boundary).encode()
    body += meta_bytes
    body += ('\r\n--%s\r\nContent-Disposition: form-data; name="frames"; '
             'filename="frames.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
             % boundary).encode()
    body += blob
    body += ('\r\n--%s--\r\n' % boundary).encode()
    return bytes(body), boundary


class FrameBatchClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def detect(self, meta, blob, timeout_s):
        """发送一批；返回 (响应dict, 分项计时dict)。错误按 kind 分类抛出。"""
        body, boundary = _build_multipart(meta, blob)
        if len(body) > MAX_BODY_BYTES:
            raise RemoteDetectError('bad_payload',
                                    '请求体 %d 字节超过 64MiB 上限' % len(body))
        cfg = self.cfg
        headers = {
            'Host': '%s:%d' % (cfg.host, cfg.port),
            'Content-Type': 'multipart/form-data; boundary=%s' % boundary,
            'Content-Length': str(len(body)),
            'Authorization': 'Bearer %s' % cfg.token,
        }
        t0 = time.perf_counter()
        conn = http.client.HTTPConnection(cfg.host, cfg.port, timeout=timeout_s)
        try:
            try:
                conn.connect()
                t_conn = time.perf_counter()
                conn.putrequest('POST', '/detect_frames')
                for k, v in headers.items():
                    conn.putheader(k, v)
                conn.endheaders()
                for i in range(0, len(body), 65536):
                    conn.send(body[i:i + 65536])
                t_send = time.perf_counter()
                resp = conn.getresponse()
                t_hdr = time.perf_counter()
                data = resp.read()
                t_end = time.perf_counter()
                status = resp.status
            except (socket.timeout, TimeoutError) as exc:
                raise RemoteDetectError('timeout', '请求超时(%ss): %s' % (timeout_s, exc))
            except (ConnectionError, http.client.HTTPException, OSError) as exc:
                raise RemoteDetectError('conn', '连接失败: %s' % exc)
        finally:
            conn.close()
        timings = {'connect_s': round(t_conn - t0, 6),
                   'upload_s': round(t_send - t_conn, 6),
                   'wait_s': round(t_hdr - t_send, 6),
                   'download_s': round(t_end - t_hdr, 6),
                   'request_bytes': len(body), 'response_bytes': len(data)}
        if status in (401, 403):
            raise RemoteDetectError('auth', '服务拒绝鉴权 HTTP %d' % status)
        if status in (429, 503):
            raise RemoteDetectError('overload', '服务过载 HTTP %d' % status)
        if status == 504:
            raise RemoteDetectError('timeout', '服务推理超时 HTTP 504')
        if status == 413:
            raise RemoteDetectError('bad_payload', '服务拒绝请求体大小 HTTP 413')
        if status != 200:
            raise RemoteDetectError('bad_payload', '服务返回 HTTP %d: %s'
                                    % (status, data[:200].decode(errors='replace')))
        try:
            payload = json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RemoteDetectError('bad_payload', '响应非合法JSON: %s' % exc)
        if not isinstance(payload, dict):
            raise RemoteDetectError('bad_payload', '响应不是JSON对象')
        return payload, timings


# ---------------------------------------------------------------- 提供者
class RemoteBatchLandmarks:
    """process_video 远程路径的帧批提供者。

    上传阶段（pass 1）：upload_frame 缓冲→压缩→发送→校验→仅存候选数组与帧哈希；
    喂帧阶段（pass 2）：verify_frame 校验二次解码比特级一致，landmarks_for 用与
    生产相同的 _select_primary_landmarks 做主subject选择，返回 override 列表。
    """

    def __init__(self, analyzer, cfg, budget=None):
        self.analyzer = analyzer
        self.cfg = cfg
        self.budget = budget
        self._client = FrameBatchClient(cfg)
        self._buf = []
        self._buf_indices = []
        self._frames = {}
        self._shapes = []
        self._hashes = []
        self._height = None
        self._width = None
        self._video_sha = None
        self._fps = None
        self._batch_seq = 0
        self.stats = {'batches': 0, 'frames_uploaded': 0, 'compress_s': 0.0,
                      'connect_s': 0.0, 'upload_s': 0.0, 'wait_s': 0.0,
                      'download_s': 0.0, 'request_bytes': 0, 'response_bytes': 0,
                      'raw_bytes': 0, 'compressed_bytes': 0, 'predict_s': 0.0,
                      'service_decompress_s': 0.0}

    def set_video(self, path, fps=None):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        self._video_sha = h.hexdigest()
        self._fps = fps

    @property
    def frame_count(self):
        return len(self._hashes)

    def _timeout_for_request(self):
        t = self.cfg.timeout_s
        if self.budget is not None:
            t = min(t, max(1.0, self.budget.deadline - self.budget.clock()))
        return t

    def upload_frame(self, frame, index):
        if self._video_sha is None:
            raise RemoteDetectError('bad_payload', '未先调用 set_video')
        if self._height is None:
            self._height, self._width = frame.shape[:2]
        elif frame.shape[:2] != (self._height, self._width):
            raise RemoteDetectError('bad_payload', '视频中途分辨率变化，远程路径拒绝')
        self._shapes.append(tuple(frame.shape))
        self._hashes.append(hashlib.sha256(frame.tobytes()).hexdigest())
        self._buf.append(frame)
        self._buf_indices.append(index)
        limit = self.cfg.batch_size
        if self.cfg.codec == 'raw':
            # raw 批体可精确预知：钳制到 64MiB 上限内（留 multipart 余量），
            # 大分辨率自动降批；压缩 codec 尺寸不可预知，沿用发送前硬校验。
            frame_bytes = self._height * self._width * 3
            limit = min(limit, max(1, (MAX_BODY_BYTES - RAW_BODY_MARGIN) // frame_bytes))
        if len(self._buf) >= limit:
            self._flush()

    def _flush(self):
        if not self._buf:
            return
        if self._video_sha is None:
            raise RemoteDetectError('bad_payload', '未先调用 set_video')
        if self.budget is not None:
            self.budget.check()
        import numpy as np
        indices = list(self._buf_indices)
        t0 = time.perf_counter()
        blob, raw_len = compress_frames(self._buf, self.cfg.codec)
        compress_s = time.perf_counter() - t0
        self._batch_seq += 1
        meta = {'video_sha256': self._video_sha, 'height': self._height,
                'width': self._width, 'fps': self._fps, 'frame_indices': indices,
                'batch_id': '%s-%d' % (self._video_sha[:12], self._batch_seq),
                'codec': self.cfg.codec}
        payload, timings = self._client.detect(meta, blob, self._timeout_for_request())
        self._validate_and_store(payload, indices)
        s = self.stats
        s['batches'] += 1
        s['frames_uploaded'] += len(indices)
        s['compress_s'] += compress_s
        for k in ('connect_s', 'upload_s', 'wait_s', 'download_s',
                  'request_bytes', 'response_bytes'):
            s[k] += timings[k]
        s['raw_bytes'] += raw_len
        s['compressed_bytes'] += len(blob)
        smeta = payload.get('meta') or {}
        s['predict_s'] += float(smeta.get('predict_s') or 0.0)
        s['service_decompress_s'] += float(smeta.get('decompress_s') or 0.0)
        self._buf = []
        self._buf_indices = []

    def finish_upload(self):
        self._flush()
        if len(self._frames) != len(self._hashes):
            raise RemoteDetectError('count_mismatch',
                                    '远端结果数 %d != 解码帧数 %d'
                                    % (len(self._frames), len(self._hashes)))
        return len(self._hashes)

    def verify_frame(self, index, frame):
        if index >= len(self._hashes):
            raise RemoteDetectError('count_mismatch',
                                    '二次解码出现多余帧 %d（上传 %d 帧）'
                                    % (index, len(self._hashes)))
        if tuple(frame.shape) != self._shapes[index]:
            raise RemoteDetectError('count_mismatch', '帧 %d 形状与上传时不一致' % index)
        if hashlib.sha256(frame.tobytes()).hexdigest() != self._hashes[index]:
            raise RemoteDetectError('count_mismatch',
                                    '帧 %d 像素与上传时不一致（解码非确定性）' % index)

    def landmarks_for(self, frame, index, fps):
        """对远端候选跑生产选择逻辑；返回 override（无主体时为 []）。"""
        rec = self._frames.get(index)
        if rec is None:
            raise RemoteDetectError('count_mismatch', '缺少帧 %d 的远端结果' % index)
        analyzer = self.analyzer
        analyzer._subject_requested_index = index
        analyzer._subject_requested_fps = fps
        try:
            landmarks, _ = analyzer._select_primary_landmarks(
                frame, rec['xy'], rec['conf'], rec['boxes'])
        finally:
            analyzer._subject_requested_index = None
            analyzer._subject_requested_fps = None
        return landmarks if landmarks is not None else []

    def _validate_and_store(self, payload, indices):
        import numpy as np
        from pose_analyzer import YOLO_POSE_LANDMARK_NAMES
        if payload.get('frame_indices') != indices:
            raise RemoteDetectError('count_mismatch',
                                    '响应 frame_indices 与请求不一致')
        n = len(indices)
        lists = {}
        for key in ('landmarks', 'xy_pixel', 'boxes', 'pose_detected'):
            val = payload.get(key)
            if not isinstance(val, list) or len(val) != n:
                raise RemoteDetectError('count_mismatch',
                                        '响应 %s 长度 != %d' % (key, n))
            lists[key] = val
        flags = {}
        for key in ('conf_missing', 'boxes_missing'):
            val = payload.get(key)
            if val is None:
                flags[key] = [False] * n
            elif isinstance(val, list) and len(val) == n \
                    and all(isinstance(b, bool) for b in val):
                flags[key] = val
            else:
                raise RemoteDetectError('bad_payload', '响应 %s 非法' % key)
        for i, idx in enumerate(indices):
            cands = lists['landmarks'][i]
            xys = lists['xy_pixel'][i]
            bxs = lists['boxes'][i]
            detected = lists['pose_detected'][i]
            conf_missing = flags['conf_missing'][i]
            boxes_missing = flags['boxes_missing'][i]
            if not isinstance(detected, bool):
                raise RemoteDetectError('bad_payload',
                                        '帧 %d pose_detected 非布尔' % idx)
            if not isinstance(cands, list) or not isinstance(xys, list) \
                    or not isinstance(bxs, list):
                raise RemoteDetectError('bad_payload', '帧 %d 候选结构非法' % idx)
            nc = len(cands)
            if detected != (nc > 0) or len(xys) != nc:
                raise RemoteDetectError('bad_payload',
                                        '帧 %d pose_detected/候选数不一致' % idx)
            if not boxes_missing and len(bxs) != nc:
                raise RemoteDetectError('bad_payload', '帧 %d boxes 数不一致' % idx)
            conf = []
            for c, cand in enumerate(cands):
                if not isinstance(cand, list) or len(cand) != 17:
                    raise RemoteDetectError('bad_payload',
                                            '帧 %d 候选 %d 关键点数 != 17' % (idx, c))
                row = []
                for j, pt in enumerate(cand):
                    if not isinstance(pt, dict) or pt.get('name') != YOLO_POSE_LANDMARK_NAMES[j]:
                        raise RemoteDetectError('bad_payload',
                                                '帧 %d 关键点 %d 名称/顺序不符' % (idx, j))
                    for field in ('x', 'y', 'visibility'):
                        v = pt.get(field)
                        if not isinstance(v, (int, float)) or isinstance(v, bool) \
                                or not math.isfinite(v):
                            raise RemoteDetectError('bad_payload',
                                                    '帧 %d 关键点 %d 字段 %s 非法' % (idx, j, field))
                    if 'z' in pt:
                        v = pt['z']
                        if not isinstance(v, (int, float)) or isinstance(v, bool) \
                                or not math.isfinite(v):
                            raise RemoteDetectError('bad_payload',
                                                    '帧 %d 关键点 %d 字段 z 非法' % (idx, j))
                    row.append(float(pt['visibility']))
                conf.append(row)
            xy_arr = np.asarray(xys, dtype=np.float64)
            if nc > 0 and xy_arr.shape != (nc, 17, 2):
                raise RemoteDetectError('bad_payload',
                                        '帧 %d xy_pixel 形状非法' % idx)
            if nc > 0 and not np.isfinite(xy_arr).all():
                raise RemoteDetectError('bad_payload', '帧 %d 像素坐标含非有限值' % idx)
            if boxes_missing:
                stored_boxes = None
            else:
                box_arr = np.asarray(bxs, dtype=np.float64)
                if nc > 0 and box_arr.shape != (nc, 4):
                    raise RemoteDetectError('bad_payload',
                                            '帧 %d boxes 形状非法' % idx)
                if nc > 0 and not np.isfinite(box_arr).all():
                    raise RemoteDetectError('bad_payload', '帧 %d boxes 含非有限值' % idx)
                stored_boxes = box_arr.reshape(nc, 4)
            self._frames[idx] = {
                'xy': xy_arr.reshape(nc, 17, 2),
                'conf': None if conf_missing else np.asarray(conf, dtype=np.float64).reshape(nc, 17),
                'boxes': stored_boxes,
                'pose_detected': detected,
            }

    def stats_summary(self):
        s = dict(self.stats)
        for k in ('compress_s', 'connect_s', 'upload_s', 'wait_s', 'download_s',
                  'predict_s', 'service_decompress_s'):
            s[k] = round(s[k], 6)
        s['codec'] = self.cfg.codec
        s['batch_size'] = self.cfg.batch_size
        s['host'] = '%s:%d' % (self.cfg.host, self.cfg.port)
        s['video_sha256'] = self._video_sha
        s['compression_ratio'] = (round(s['raw_bytes'] / s['compressed_bytes'], 4)
                                  if s['compressed_bytes'] else None)
        return s