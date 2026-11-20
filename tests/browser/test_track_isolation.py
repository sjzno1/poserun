"""P2 切片5 浏览器隔离门：真 Chrome、loopback、禁外网。

抓的是「页面 200 但 SVG 未挂载 DOM」这类纯渲染 bug（上一切片教训）：
导入带 GPS 的 GPX → 打开轨迹页 → 断言 path 真实挂载且点数>0、海拔剖面存在、
起终点/比例尺、A/B 同名隔离、无轨迹文案、开关 404、reload 后仍渲染、列表入口可达。
"""
import json
import math
import re
import urllib.error
import urllib.request
import uuid
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from running.preview import create_app


def gpx_bytes(n=40, lat0=31.23, lon0=121.47):
    pts = []
    for i in range(n):
        lat = lat0 + i * 0.0004
        lon = lon0 + i * 0.0005 + 0.0002 * math.sin(i / 3)
        ele = 10 + 5 * math.sin(i / 4)
        pts.append(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}"><ele>{ele:.1f}</ele>'
                   f'<time>2026-11-16T00:{i:02d}:00Z</time></trkpt>')
    return ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="t">'
            '<trk><name>map</name><trkseg>' + ''.join(pts) + '</trkseg></trk></gpx>').encode()


def http_json(base, path, token=None, method='GET', body=None):
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header('Content-Type', 'application/json')
    try:
        r = urllib.request.urlopen(req, data=data, timeout=15)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def http_import(base, token, filename, data):
    boundary = '----poserunmap' + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(base + '/api/running/activities/import', data=body, method='POST')
    req.add_header('Authorization', 'Bearer ' + token)
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def path_point_count(d):
    return len(re.findall(r'[ML]', d or ''))


def test_track_render_isolation_and_flag_gate(tmp_path):
    app = create_app(tmp_path)
    app.config['TRACK_MAP_ENABLED'] = True
    app.config['MANUAL_ENTRY_ENABLED'] = True
    passwords = 'Synthetic-local-only'
    users = {}
    for name in ('track_a', 'track_b'):
        user, err = app.extensions['poserun_auth'].register_user(name, passwords, '轨迹同名跑者')
        assert not err
        users[name] = user['id']
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        # 真导入路径：HTTP multipart 上传带 GPS 的 GPX（A 40 点、B 15 点，同名文件）
        tokens = {}
        for name in ('track_a', 'track_b'):
            s, j = http_json(base, '/api/auth/login', method='POST',
                             body={'username': name, 'password': passwords})
            assert s == 200, j
            tokens[name] = j['token']
        s, ja = http_import(base, tokens['track_a'], 'same-name.gpx', gpx_bytes(n=40))
        assert s == 201, ja
        aid_a = ja['activity']['id']
        s, jm = http_json(base, '/api/running/activities', tokens['track_a'], method='POST',
                          body={'distance_km': 5, 'duration_min': 30, 'start_time': '2026-11-15T08:00:00+08:00'})
        assert s == 201, jm
        aid_manual = jm['activity']['id']
        s, jb = http_import(base, tokens['track_b'], 'same-name.gpx', gpx_bytes(n=15, lat0=39.9, lon0=116.4))
        assert s == 201, jb
        aid_b = jb['activity']['id']
        assert aid_b != aid_a

        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', ignore_default_args=['--disable-back-forward-cache'])
            ctx = browser.new_context(viewport={'width': 390, 'height': 844})
            ctx.route('**/*', lambda r: r.continue_() if urlsplit(r.request.url).netloc == urlsplit(base).netloc else r.abort())
            page = ctx.new_page()

            def login(name):
                page.locator('#loginUsername').fill(name)
                page.locator('#loginPassword').fill(passwords)
                page.locator('#loginBtn').click()

            # ---- A：轨迹真实渲染（SVG path 挂载 DOM、点数>0）----
            page.goto(base + f'/activity-track?id={aid_a}')
            login('track_a')
            svg = page.locator('#trackMap')
            expect(svg).to_have_count(1)
            expect(svg).to_have_attribute('role', 'img')
            expect(svg).to_have_attribute('aria-label', re.compile(r'\d+'))
            track_path = page.locator('#trackPath')
            expect(track_path).to_have_count(1)
            d = track_path.get_attribute('d')
            assert path_point_count(d) > 0, f'SVG path 未挂载真实点: {d!r}'
            assert path_point_count(d) >= 2
            expect(page.locator('#trackStart')).to_have_count(1)
            expect(page.locator('#trackEnd')).to_have_count(1)
            expect(page.locator('#trackScaleLabel')).to_contain_text(re.compile(r'(m|km)'))
            # 海拔剖面真实渲染
            elev = page.locator('#elevChart')
            expect(elev).to_have_count(1)
            expect(page.locator('#elevLine')).to_have_count(1)
            elev_d = page.locator('#elevLine').get_attribute('d')
            assert path_point_count(elev_d) > 0
            expect(page.locator('#elevArea')).to_have_count(1)
            # 摘要
            expect(page.locator('#trackSummary')).to_contain_text('km')
            expect(page.locator('#trackEmpty')).to_be_hidden()

            # ---- reload 后仍渲染 ----
            page.reload()
            expect(page.locator('#trackPath')).to_have_count(1)
            assert path_point_count(page.locator('#trackPath').get_attribute('d')) > 0
            expect(page.locator('#elevLine')).to_have_count(1)

            # ---- 无轨迹活动：优雅文案 ----
            page.goto(base + f'/activity-track?id={aid_manual}')
            expect(page.locator('#trackEmpty')).to_be_visible()
            expect(page.locator('#trackEmpty')).to_contain_text('无轨迹数据')
            expect(page.locator('#trackMap')).to_have_count(0)

            # ---- 活动列表入口：轨迹链接真实存在且可点击到达 ----
            page.goto(base + '/activities.html')
            link = page.locator(f'a.rm-track-link[href="/activity-track?id={aid_a}"]')
            expect(link).to_have_count(1)
            expect(link).to_contain_text('轨迹')
            link.click()
            expect(page.locator('#trackPath')).to_have_count(1)
            assert path_point_count(page.locator('#trackPath').get_attribute('d')) > 0

            # ---- 登出 → B：A 的活动 404 文案；B 自己的轨迹渲染自己的点 ----
            with page.expect_navigation(wait_until='load'):
                page.locator('#navLogoutBtn').evaluate('(e)=>e.click()')
            page.goto(base + f'/activity-track?id={aid_a}')
            login('track_b')
            expect(page.locator('#trackStatus')).to_contain_text('不存在')
            expect(page.locator('#trackMap')).to_have_count(0)
            page.goto(base + f'/activity-track?id={aid_b}')
            expect(page.locator('#trackPath')).to_have_count(1)
            d_b = page.locator('#trackPath').get_attribute('d')
            assert path_point_count(d_b) > 0
            expect(page.locator('#trackSummary')).to_contain_text('km')
            # B 看不到 A 的列表入口
            page.goto(base + '/activities.html')
            expect(page.locator(f'a.rm-track-link[href="/activity-track?id={aid_a}"]')).to_have_count(0)
            expect(page.locator(f'a.rm-track-link[href="/activity-track?id={aid_b}"]')).to_have_count(1)

            # ---- 开关关闭：页面与 API 全 404 ----
            app.config['TRACK_MAP_ENABLED'] = False
            resp = page.goto(base + f'/activity-track?id={aid_b}')
            assert resp.status == 404
            s, _ = http_json(base, f'/api/running/activities/{aid_b}/track', tokens['track_b'])
            assert s == 404
            page.goto(base + '/activities.html')
            expect(page.locator('a.rm-track-link')).to_have_count(0)
            app.config['TRACK_MAP_ENABLED'] = True

            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=10)