"""Remote frame-offload path tests (Phase2/3). Model-free; HTTP via loopback mock.

Covers: config gating (default off), lossless compress roundtrips, batching,
count-mismatch fail-closed, HTTP error classification, landmarks reconstruction
dtype, analyze_frame override skipping YOLO predict, and the two-pass remote
pipeline. No assertions weakened; no real weights or production services touched.
"""
import os
import sys
import json
import time
import hashlib
import threading
import http.server
import email.parser
import email.policy
from pathlib import Path

import numpy as np
import cv2
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import remote_detect as rd
from remote_detect import (RemoteConfig, RemoteDetectError, RemoteBatchLandmarks,
                           FrameBatchClient, config_from_env, compress_frames,
                           decompress_frames)
from pose_analyzer import YOLO_POSE_LANDMARK_NAMES
from calibrated_analyzer import CalibratedPoseAnalyzer, Landmark
from compute_budget import Budget

H, W = 32, 24
FRAME_BYTES = H * W * 3
TOKEN = 'unit-test-token'


def make_frames(n, h=H, w=W):
    rng = np.random.RandomState(1234)
    return [rng.randint(0, 256, (h, w, 3), dtype=np.uint8) for _ in range(n)]


def valid_response(indices, h=H, w=W, nc=1):
    n = len(indices)
    lms, xys, bxs = [], [], []
    for _ in range(n):
        fl, fx, fb = [], [], []
        for _c in range(nc):
            fl.append([{'name': YOLO_POSE_LANDMARK_NAMES[j], 'x': 0.5, 'y': 0.5,
                        'visibility': 0.9} for j in range(17)])
            fx.append([[12.0, 12.0] for _ in range(17)])
            fb.append([1.0, 2.0, 20.0, 30.0])
        lms.append(fl); xys.append(fx); bxs.append(fb)
    return {'frame_indices': list(indices), 'pose_detected': [nc > 0] * n,
            'landmarks': lms, 'xy_pixel': xys, 'boxes': bxs,
            'conf_missing': [False] * n, 'boxes_missing': [False] * n,
            'meta': {'predict_s': 0.01, 'decompress_s': 0.0, 'device': 'cpu'}}


ZERO_TIMINGS = {'connect_s': 0.0, 'upload_s': 0.0, 'wait_s': 0.0,
                'download_s': 0.0, 'request_bytes': 0, 'response_bytes': 0}


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / 'token.txt'
    p.write_text(TOKEN + '\n')
    return str(p)


def make_cfg(token_file, port=9, batch_size=4, codec='zlib'):
    return RemoteConfig(url='http://127.0.0.1:%d' % port, token_file=token_file,
                        batch_size=batch_size, codec=codec)


class FakeAnalyzer:
    def __init__(self):
        self.calls = []
        self._detection_status = 'detected'

    def _select_primary_landmarks(self, frame, xy, conf, boxes):
        self.calls.append((xy, conf, boxes))
        return [Landmark(nm, 0.5, 0.5, 0.0, 0.9) for nm in YOLO_POSE_LANDMARK_NAMES], 0


def make_provider(token_file, batch_size=4, codec='zlib'):
    cfg = make_cfg(token_file, batch_size=batch_size, codec=codec)
    return RemoteBatchLandmarks(FakeAnalyzer(), cfg, Budget())


# ---------------- config gating ----------------

def test_config_default_off(monkeypatch):
    for k in list(os.environ):
        if k.startswith('POSERUN_REMOTE_DETECT_'):
            monkeypatch.delenv(k, raising=False)
    cfg = config_from_env()
    assert cfg.enabled is False
    assert cfg.url == ''


def test_config_enabled(monkeypatch, token_file):
    monkeypatch.setenv('POSERUN_REMOTE_DETECT_URL', 'http://192.168.1.100:8765')
    monkeypatch.setenv('POSERUN_REMOTE_DETECT_TOKEN_FILE', token_file)
    monkeypatch.setenv('POSERUN_REMOTE_DETECT_BATCH_SIZE', '40')
    cfg = config_from_env()
    assert cfg.enabled is True
    assert cfg.batch_size == 40
    assert cfg.token == TOKEN
    assert cfg.host == '192.168.1.100' and cfg.port == 8765


def test_config_bad_batch_size(monkeypatch, token_file):
    monkeypatch.setenv('POSERUN_REMOTE_DETECT_URL', 'http://127.0.0.1:8765')
    monkeypatch.setenv('POSERUN_REMOTE_DETECT_TOKEN_FILE', token_file)
    monkeypatch.setenv('POSERUN_REMOTE_DETECT_BATCH_SIZE', '0')
    with pytest.raises(RemoteDetectError) as ei:
        config_from_env()
    assert ei.value.kind == 'bad_payload'


def test_config_requires_token_file(monkeypatch):
    monkeypatch.setenv('POSERUN_REMOTE_DETECT_URL', 'http://127.0.0.1:8765')
    monkeypatch.delenv('POSERUN_REMOTE_DETECT_TOKEN_FILE', raising=False)
    with pytest.raises(RemoteDetectError) as ei:
        config_from_env()
    assert ei.value.kind == 'auth'


def test_config_rejects_public_host(token_file):
    with pytest.raises(RemoteDetectError) as ei:
        RemoteConfig(url='http://8.8.8.8:8765', token_file=token_file)
    assert ei.value.kind == 'bad_payload'


def test_config_rejects_path_prefix(token_file):
    with pytest.raises(RemoteDetectError) as ei:
        RemoteConfig(url='http://127.0.0.1:8765/detect_frames', token_file=token_file)
    assert ei.value.kind == 'bad_payload'


def test_config_rejects_https(token_file):
    with pytest.raises(RemoteDetectError) as ei:
        RemoteConfig(url='https://127.0.0.1:8765', token_file=token_file)
    assert ei.value.kind == 'bad_payload'


# ---------------- lossless compression ----------------

@pytest.mark.parametrize('codec', ['zlib', 'zstd', 'png'])
def test_compress_roundtrip_lossless(codec):
    frames = make_frames(5)
    raw = b''.join(f.tobytes() for f in frames)
    blob, raw_len = compress_frames(frames, codec)
    assert raw_len == len(raw)
    out = decompress_frames(blob, codec, 5, H, W)
    assert out.shape == (5, H, W, 3)
    assert out.tobytes() == raw  # bit-exact lossless


def test_compress_empty_batch_rejected():
    with pytest.raises(RemoteDetectError) as ei:
        compress_frames([], 'zlib')
    assert ei.value.kind == 'bad_payload'


def test_decompress_rejects_truncated_zlib():
    frames = make_frames(2)
    blob, raw_len = compress_frames(frames, 'zlib')
    with pytest.raises(RemoteDetectError) as ei:
        decompress_frames(blob[:-3], 'zlib', 2, H, W)
    assert ei.value.kind == 'bad_payload'


def test_decompress_rejects_wrong_count():
    frames = make_frames(2)
    blob, _ = compress_frames(frames, 'zlib')
    with pytest.raises(RemoteDetectError):
        decompress_frames(blob, 'zlib', 3, H, W)


# ---------------- batching (mock client) ----------------

def test_batching_flush_boundaries(monkeypatch, token_file):
    p = make_provider(token_file, batch_size=4)
    posted = []

    def fake_detect(meta, blob, timeout_s):
        posted.append(list(meta['frame_indices']))
        return valid_response(meta['frame_indices']), dict(ZERO_TIMINGS)

    monkeypatch.setattr(p._client, 'detect', fake_detect)
    p.set_video(__file__, 30.0)
    for i, f in enumerate(make_frames(10)):
        p.upload_frame(f, i)
    assert posted == [[0, 1, 2, 3], [4, 5, 6, 7]]
    assert p.finish_upload() == 10
    assert posted == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]
    assert p.stats['batches'] == 3
    assert p.stats['frames_uploaded'] == 10
    assert set(p._frames) == set(range(10))


def test_upload_requires_set_video(token_file):
    p = make_provider(token_file)
    with pytest.raises(RemoteDetectError) as ei:
        p.upload_frame(make_frames(1)[0], 0)
    assert ei.value.kind == 'bad_payload'


def test_upload_rejects_resolution_change(monkeypatch, token_file):
    p = make_provider(token_file)
    monkeypatch.setattr(p._client, 'detect',
                        lambda m, b, t: (valid_response(m['frame_indices']), dict(ZERO_TIMINGS)))
    p.set_video(__file__, 30.0)
    p.upload_frame(make_frames(1)[0], 0)
    with pytest.raises(RemoteDetectError) as ei:
        p.upload_frame(np.zeros((H + 1, W, 3), np.uint8), 1)
    assert ei.value.kind == 'bad_payload'


# ---------------- count mismatch fail-closed ----------------

def test_count_mismatch_short_response(monkeypatch, token_file):
    p = make_provider(token_file, batch_size=4)
    monkeypatch.setattr(p._client, 'detect',
                        lambda m, b, t: (valid_response(m['frame_indices'][:-1]), dict(ZERO_TIMINGS)))
    p.set_video(__file__, 30.0)
    with pytest.raises(RemoteDetectError) as ei:
        for i, f in enumerate(make_frames(4)):
            p.upload_frame(f, i)
    assert ei.value.kind == 'count_mismatch'


def test_count_mismatch_wrong_indices(monkeypatch, token_file):
    p = make_provider(token_file, batch_size=4)

    def fake(m, b, t):
        resp = valid_response(m['frame_indices'])
        resp['frame_indices'] = [99, 100, 101, 102]
        return resp, dict(ZERO_TIMINGS)

    monkeypatch.setattr(p._client, 'detect', fake)
    p.set_video(__file__, 30.0)
    with pytest.raises(RemoteDetectError) as ei:
        for i, f in enumerate(make_frames(4)):
            p.upload_frame(f, i)
    assert ei.value.kind == 'count_mismatch'


def test_finish_upload_count_mismatch(monkeypatch, token_file):
    p = make_provider(token_file, batch_size=10)
    # server returns only 1 of 2 frames but with matching indices subset trick:
    # drop one frame's result by returning fewer landmarks lists -> count_mismatch
    def fake(m, b, t):
        resp = valid_response(m['frame_indices'])
        resp['landmarks'] = resp['landmarks'][:-1]
        return resp, dict(ZERO_TIMINGS)

    monkeypatch.setattr(p._client, 'detect', fake)
    p.set_video(__file__, 30.0)
    with pytest.raises(RemoteDetectError) as ei:
        for i, f in enumerate(make_frames(2)):
            p.upload_frame(f, i)
        p.finish_upload()
    assert ei.value.kind == 'count_mismatch'


# ---------------- bad payload schema ----------------

def test_bad_payload_xy_length(monkeypatch, token_file):
    p = make_provider(token_file, batch_size=4)

    def fake(m, b, t):
        resp = valid_response(m['frame_indices'])
        resp['xy_pixel'][0][0] = [0.0] * 33  # must be 34
        return resp, dict(ZERO_TIMINGS)

    monkeypatch.setattr(p._client, 'detect', fake)
    p.set_video(__file__, 30.0)
    with pytest.raises(RemoteDetectError) as ei:
        for i, f in enumerate(make_frames(4)):
            p.upload_frame(f, i)
    assert ei.value.kind == 'bad_payload'


def test_bad_payload_landmark_name(monkeypatch, token_file):
    p = make_provider(token_file, batch_size=4)

    def fake(m, b, t):
        resp = valid_response(m['frame_indices'])
        resp['landmarks'][0][0][0]['name'] = 'nose'  # wrong case/order
        return resp, dict(ZERO_TIMINGS)

    monkeypatch.setattr(p._client, 'detect', fake)
    p.set_video(__file__, 30.0)
    with pytest.raises(RemoteDetectError) as ei:
        for i, f in enumerate(make_frames(4)):
            p.upload_frame(f, i)
    assert ei.value.kind == 'bad_payload'


# ---------------- landmarks reconstruction ----------------

def test_landmarks_for_float64_and_conf_from_visibility(monkeypatch, token_file):
    p = make_provider(token_file, batch_size=2)
    monkeypatch.setattr(p._client, 'detect',
                        lambda m, b, t: (valid_response(m['frame_indices']), dict(ZERO_TIMINGS)))
    p.set_video(__file__, 30.0)
    frames = make_frames(2)
    for i, f in enumerate(frames):
        p.upload_frame(f, i)
    p.finish_upload()
    lm = p.landmarks_for(frames[0], 0, 30.0)
    assert len(lm) == 17
    xy, conf, boxes = p.analyzer.calls[0]
    assert xy.dtype == np.float64 and xy.shape == (1, 17, 2)
    assert conf.dtype == np.float64 and conf.shape == (1, 17)
    assert boxes.dtype == np.float64 and boxes.shape == (1, 4)
    assert np.all(conf == 0.9)  # reconstructed from visibility


def test_landmarks_for_no_candidate(monkeypatch, token_file):
    p = make_provider(token_file, batch_size=1)
    monkeypatch.setattr(p._client, 'detect',
                        lambda m, b, t: (valid_response(m['frame_indices'], nc=0), dict(ZERO_TIMINGS)))
    p.set_video(__file__, 30.0)
    f = make_frames(1)[0]
    p.upload_frame(f, 0)
    p.finish_upload()
    p.landmarks_for(f, 0, 30.0)
    xy, conf, boxes = p.analyzer.calls[0]
    assert xy.shape == (0, 17, 2)
    assert conf.shape == (0, 17)
    assert boxes.shape == (0, 4)


def test_verify_frame_detects_pixel_drift(monkeypatch, token_file):
    p = make_provider(token_file, batch_size=1)
    monkeypatch.setattr(p._client, 'detect',
                        lambda m, b, t: (valid_response(m['frame_indices']), dict(ZERO_TIMINGS)))
    p.set_video(__file__, 30.0)
    f = make_frames(1)[0]
    p.upload_frame(f, 0)
    p.finish_upload()
    p.verify_frame(0, f)  # identical ok
    drifted = f.copy(); drifted[0, 0, 0] ^= 1
    with pytest.raises(RemoteDetectError) as ei:
        p.verify_frame(0, drifted)
    assert ei.value.kind == 'count_mismatch'


# ---------------- real loopback HTTP mock service ----------------

def _parse_multipart(content_type, body):
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
        b'Content-Type: ' + content_type.encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if name:
            parts[name] = part.get_payload(decode=True)
    return json.loads(parts['meta'].decode()), parts['frames']


@pytest.fixture
def mock_service():
    control = {'status': 200, 'drop': False, 'sleep': 0.0, 'auth': None}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length)
            control['auth'] = self.headers.get('Authorization')
            if control['sleep']:
                time.sleep(control['sleep'])
            if control['status'] != 200:
                self._send(control['status'], {'error': 'mock'})
                return
            meta, _blob = _parse_multipart(self.headers.get('Content-Type', ''), body)
            indices = meta['frame_indices']
            if control['drop']:
                indices = indices[:-1]
            self._send(200, valid_response(indices))

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield control, port
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize('status,kind', [(401, 'auth'), (403, 'auth'),
                                         (503, 'overload'), (429, 'overload'),
                                         (504, 'timeout'), (413, 'bad_payload'),
                                         (400, 'bad_payload'), (500, 'bad_payload')])
def test_http_error_classification(mock_service, token_file, status, kind):
    control, port = mock_service
    control['status'] = status
    cfg = make_cfg(token_file, port=port)
    client = FrameBatchClient(cfg)
    frames = make_frames(2)
    blob, _ = compress_frames(frames, 'zlib')
    meta = {'video_sha256': 'x', 'height': H, 'width': W, 'fps': 30.0,
            'frame_indices': [0, 1], 'batch_id': 'b', 'codec': 'zlib'}
    with pytest.raises(RemoteDetectError) as ei:
        client.detect(meta, blob, 5.0)
    assert ei.value.kind == kind


def test_http_timeout_classification(mock_service, token_file):
    control, port = mock_service
    control['sleep'] = 1.5
    cfg = make_cfg(token_file, port=port)
    client = FrameBatchClient(cfg)
    blob, _ = compress_frames(make_frames(1), 'zlib')
    meta = {'video_sha256': 'x', 'height': H, 'width': W, 'fps': 30.0,
            'frame_indices': [0], 'batch_id': 'b', 'codec': 'zlib'}
    with pytest.raises(RemoteDetectError) as ei:
        client.detect(meta, blob, 0.3)
    assert ei.value.kind == 'timeout'


def test_http_conn_classification(token_file):
    # port 1 is reserved/unlistened -> connection refused
    cfg = make_cfg(token_file, port=1)
    client = FrameBatchClient(cfg)
    blob, _ = compress_frames(make_frames(1), 'zlib')
    meta = {'video_sha256': 'x', 'height': H, 'width': W, 'fps': 30.0,
            'frame_indices': [0], 'batch_id': 'b', 'codec': 'zlib'}
    with pytest.raises(RemoteDetectError) as ei:
        client.detect(meta, blob, 2.0)
    assert ei.value.kind == 'conn'


def test_http_roundtrip_and_auth_header(mock_service, token_file):
    control, port = mock_service
    p = make_provider(token_file, batch_size=4)
    p.cfg = make_cfg(token_file, port=port, batch_size=4)
    p._client = FrameBatchClient(p.cfg)
    p.set_video(__file__, 30.0)
    frames = make_frames(4)
    for i, f in enumerate(frames):
        p.upload_frame(f, i)
    assert p.finish_upload() == 4
    assert control['auth'] == 'Bearer ' + TOKEN
    assert p.stats['batches'] == 1
    assert p.stats['frames_uploaded'] == 4
    assert set(p._frames) == {0, 1, 2, 3}
    lm = p.landmarks_for(frames[0], 0, 30.0)
    assert len(lm) == 17


def test_http_count_mismatch_fail_closed(mock_service, token_file):
    control, port = mock_service
    control['drop'] = True
    p = make_provider(token_file, batch_size=4)
    p.cfg = make_cfg(token_file, port=port, batch_size=4)
    p._client = FrameBatchClient(p.cfg)
    p.set_video(__file__, 30.0)
    with pytest.raises(RemoteDetectError) as ei:
        for i, f in enumerate(make_frames(4)):
            p.upload_frame(f, i)
    assert ei.value.kind == 'count_mismatch'


# ---------------- analyze_frame override ----------------

def _bare_analyzer():
    a = CalibratedPoseAnalyzer.__new__(CalibratedPoseAnalyzer)
    a._smoothers = {}
    a._last_context = None
    a._subject_requested_index = None
    a._subject_requested_fps = None
    a._detection_status = 'detected'
    a._primary_subject = None
    a._subject_frame_counter = 0
    a._video_frame_index = 0
    a._yolo_model = None
    return a


def test_analyze_frame_override_skips_predict(monkeypatch):
    a = _bare_analyzer()

    def boom(frame):
        raise AssertionError('YOLO predict must NOT run when override provided')

    monkeypatch.setattr(a, '_detect_landmarks', boom)
    frame = np.zeros((100, 100, 3), np.uint8)
    override = [Landmark(nm, 0.5, 0.5, 0.0, 0.9) for nm in YOLO_POSE_LANDMARK_NAMES]
    res = a.analyze_frame(frame, frame_index=0, fps=30.0, landmarks_override=override)
    assert res is not None
    assert 'landmarks' in res and 'left_knee_angle' in res


def test_analyze_frame_empty_override_returns_none(monkeypatch):
    a = _bare_analyzer()
    monkeypatch.setattr(a, '_detect_landmarks',
                        lambda f: (_ for _ in ()).throw(AssertionError('no predict')))
    assert a.analyze_frame(np.zeros((100, 100, 3), np.uint8), landmarks_override=[]) is None


def test_analyze_frame_no_override_uses_local(monkeypatch):
    a = _bare_analyzer()
    called = []
    monkeypatch.setattr(a, '_detect_landmarks', lambda f: (called.append(1), ([], 0))[1])
    assert a.analyze_frame(np.zeros((100, 100, 3), np.uint8)) is None
    assert called == [1]


# ---------------- two-pass remote pipeline integration ----------------

class FakeProvider:
    def __init__(self, override):
        self.override = override
        self.frame_count = 0
        self.shas = {}
        self.stats = {}
        self.budget = None

    def set_video(self, path, fps):
        self.fps = fps

    def upload_frame(self, frame, index):
        assert index == self.frame_count
        self.shas[index] = hashlib.sha256(np.ascontiguousarray(frame).tobytes()).hexdigest()
        self.frame_count += 1

    def finish_upload(self):
        return self.frame_count

    def verify_frame(self, index, frame):
        got = hashlib.sha256(np.ascontiguousarray(frame).tobytes()).hexdigest()
        assert got == self.shas[index], 'pass-2 frame differs from pass-1'

    def landmarks_for(self, frame, index, fps):
        return list(self.override)


def _write_video(path, n, h=64, w=64, fps=30.0):
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
    assert vw.isOpened(), 'VideoWriter failed to open'
    rng = np.random.RandomState(7)
    for _ in range(n):
        vw.write(rng.randint(0, 256, (h, w, 3), dtype=np.uint8))
    vw.release()


def _clear_remote_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith('POSERUN_REMOTE_DETECT_'):
            monkeypatch.delenv(k, raising=False)


def test_video_pipeline_remote_two_pass(tmp_path, monkeypatch):
    import video_pipeline as vp
    _clear_remote_env(monkeypatch)
    vid = tmp_path / 'synth.mp4'
    _write_video(vid, 6)
    a = _bare_analyzer()
    override = [Landmark(nm, 0.5, 0.5, 0.0, 0.9) for nm in YOLO_POSE_LANDMARK_NAMES]
    provider = FakeProvider(override)
    rows = vp.process_video(a, str(vid), remote_provider=provider)
    assert len(rows) == 6
    assert provider.frame_count == 6
    assert all(r['pose_detected'] for r in rows)
    meta = a.last_video_metadata
    assert meta['decoded_frames'] == 6
    assert meta['schema_version'] == 'decoded-timeline-v1'
    assert provider.stats['decoded_frame_count'] == 6
    assert [r['frame_index'] for r in rows] == list(range(6))


def test_video_pipeline_remote_count_mismatch(tmp_path, monkeypatch):
    import video_pipeline as vp
    _clear_remote_env(monkeypatch)
    vid = tmp_path / 'synth.mp4'
    _write_video(vid, 5)
    a = _bare_analyzer()
    override = [Landmark(nm, 0.5, 0.5, 0.0, 0.9) for nm in YOLO_POSE_LANDMARK_NAMES]
    provider = FakeProvider(override)
    provider.finish_upload = lambda: 99
    with pytest.raises(RemoteDetectError) as ei:
        vp.process_video(a, str(vid), remote_provider=provider)
    assert ei.value.kind == 'count_mismatch'


def test_default_off_no_remote_dispatch(tmp_path, monkeypatch):
    """Remote env unset => process_video must take the local path (no provider)."""
    import video_pipeline as vp
    _clear_remote_env(monkeypatch)
    vid = tmp_path / 'synth.mp4'
    _write_video(vid, 3)
    a = _bare_analyzer()
    # local path calls _detect_landmarks; force empty detection -> no_pose rows
    monkeypatch.setattr(a, '_detect_landmarks', lambda f: ([], 0))
    rows = vp.process_video(a, str(vid))
    assert len(rows) == 3
    assert all(not r['pose_detected'] for r in rows)
    assert a.last_video_metadata['decoded_frames'] == 3

# ---------------- Phase4 raw 直传（10G 默认） ----------------

def test_default_codec_is_raw(monkeypatch, token_file):
    for k in list(os.environ):
        if k.startswith('POSERUN_REMOTE_DETECT_'):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('POSERUN_REMOTE_DETECT_URL', 'http://192.168.1.100:8765')
    monkeypatch.setenv('POSERUN_REMOTE_DETECT_TOKEN_FILE', token_file)
    cfg = config_from_env()
    assert cfg.codec == 'raw'
    assert rd.DEFAULT_CODEC == 'raw'
    assert 'raw' in rd.CODECS


def test_raw_identity_roundtrip():
    frames = make_frames(5)
    blob, raw_len = compress_frames(frames, 'raw')
    assert blob == b''.join(f.tobytes() for f in frames)  # identity，无压缩
    assert raw_len == 5 * FRAME_BYTES
    out = decompress_frames(blob, 'raw', 5, H, W)
    assert out.dtype == np.uint8 and out.shape == (5, H, W, 3)
    for i, f in enumerate(frames):
        assert np.array_equal(out[i], f)


def test_raw_decompress_rejects_length_mismatch():
    frames = make_frames(2)
    blob, _ = compress_frames(frames, 'raw')
    with pytest.raises(RemoteDetectError) as ei:
        decompress_frames(blob[:-1], 'raw', 2, H, W)
    assert ei.value.kind == 'bad_payload'
    with pytest.raises(RemoteDetectError) as ei:
        decompress_frames(blob, 'raw', 3, H, W)
    assert ei.value.kind == 'bad_payload'


def test_raw_batch_clamped_to_body_limit(monkeypatch, token_file):
    # 1080p 帧 6.22MB：40 帧=248MB>64MiB → 自动降批到 10（留 1MiB 余量）。
    h, w = 1080, 1920
    fb = h * w * 3
    expect = max(1, (rd.MAX_BODY_BYTES - rd.RAW_BODY_MARGIN) // fb)
    assert expect == 10
    p = make_provider(token_file, batch_size=40, codec='raw')
    posted, sizes = [], []

    def fake_detect(meta, blob, timeout_s):
        posted.append(list(meta['frame_indices']))
        sizes.append(len(blob))
        assert len(blob) <= rd.MAX_BODY_BYTES - rd.RAW_BODY_MARGIN
        return valid_response(meta['frame_indices'], h=h, w=w), dict(ZERO_TIMINGS)

    monkeypatch.setattr(p._client, 'detect', fake_detect)
    p.set_video(__file__, 30.0)
    for i, f in enumerate(make_frames(25, h=h, w=w)):
        p.upload_frame(f, i)
    p.finish_upload()
    assert posted == [list(range(0, 10)), list(range(10, 20)), list(range(20, 25))]
    assert sizes == [10 * fb, 10 * fb, 5 * fb]
    assert p.stats['batches'] == 3 and p.stats['frames_uploaded'] == 25


def test_raw_small_frames_keep_batch_40(monkeypatch, token_file):
    # 960x544 帧 1.57MB：40 帧≈59.8MiB<64MiB-1MiB → 不降批。
    h, w = 960, 544
    p = make_provider(token_file, batch_size=40, codec='raw')
    posted = []

    def fake_detect(meta, blob, timeout_s):
        posted.append(list(meta['frame_indices']))
        return valid_response(meta['frame_indices'], h=h, w=w), dict(ZERO_TIMINGS)

    monkeypatch.setattr(p._client, 'detect', fake_detect)
    p.set_video(__file__, 30.0)
    for i, f in enumerate(make_frames(45, h=h, w=w)):
        p.upload_frame(f, i)
    p.finish_upload()
    assert posted == [list(range(0, 40)), list(range(40, 45))]


def test_raw_single_oversize_frame_fail_closed(monkeypatch, token_file):
    # 单帧即超 64MiB（如 8K）：钳制最小批 1 仍超限 → 发送前 bad_payload。
    h, w = 4320, 7680  # 99.5MB/帧
    p = make_provider(token_file, batch_size=40, codec='raw')
    # 不 mock detect：真实 FrameBatchClient.detect 在发送前硬校验体大小。
    p.set_video(__file__, 30.0)
    with pytest.raises(RemoteDetectError) as ei:
        p.upload_frame(make_frames(1, h=h, w=w)[0], 0)
    assert ei.value.kind == 'bad_payload'
