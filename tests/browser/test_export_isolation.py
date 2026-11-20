"""P2 切片6 数据导出真浏览器隔离测试（AGPL-3.0）。

Flask 线程 + tmp_path DB + playwright(channel=chrome)：登录→journal 页头导出面板→
选择年份→点击导出→断言真实下载内容（CSV BOM/表头/换算/缺测留空/软删排除/排序；
JSON count/字段/null）→键盘 Enter 触发→reload→同名 A/B 隔离→owner 伪造→
设备来源只读但可导出→开关关闭 API 404＋面板隐藏。
"""
import csv
import io
import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from running.preview import create_app

BOM = b'\xef\xbb\xbf'
CSV_HEADER = ['id', 'start_time', 'sport', 'distance_km', 'duration_s', 'avg_hr',
              'elevation_gain_m', 'source', 'notes']
PASSWORD = 'Synthetic-local-only'
GPX = ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="t">'
       '<trk><name>export</name><trkseg>'
       '<trkpt lat="31.2304" lon="121.4737"><ele>4</ele><time>2026-04-01T02:00:00Z</time></trkpt>'
       '<trkpt lat="31.2314" lon="121.4747"><ele>6</ele><time>2026-04-01T02:10:00Z</time></trkpt>'
       '</trkseg></trk></gpx>').encode()


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
    boundary = '----poserunexport' + uuid.uuid4().hex
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


def csv_rows(raw):
    assert raw.startswith(BOM), 'CSV 下载必须带 UTF-8 BOM'
    return list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))


def test_export_isolation_browser(tmp_path):
    app = create_app(tmp_path)
    app.config['EXPORT_ENABLED'] = True
    app.config['MANUAL_ENTRY_ENABLED'] = True
    app.config['GEAR_ENABLED'] = True
    auth = app.extensions['poserun_auth']
    users = {}
    for name in ('export_a', 'export_b'):
        user, err = auth.register_user(name, PASSWORD, '导出浏览器同名')
        assert not err
        users[name] = user['id']
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        tokens = {}
        for name in ('export_a', 'export_b'):
            s, j = http_json(base, '/api/auth/login', method='POST',
                             body={'username': name, 'password': PASSWORD})
            assert s == 200, j
            tokens[name] = j['token']

        def create(name, start, km, minutes=None, hr=None, notes=None):
            body = {'start_time': start, 'distance_km': km}
            if minutes is not None:
                body['duration_min'] = minutes
            if hr is not None:
                body['avg_hr'] = hr
            if notes is not None:
                body['notes'] = notes
            s, j = http_json(base, '/api/running/activities', tokens[name], method='POST', body=body)
            assert s == 201, j
            return j['activity']

        notes_a = '间歇, 4组\n第二行"引号"与中文'
        act2025 = create('export_a', '2025-06-01T02:00:00Z', 5.0, 30, 145, notes_a)
        boundary = create('export_a', '2025-12-31T16:00:00Z', 1.0, 5)  # 北京 2026 元旦
        act2026 = create('export_a', '2026-03-01T02:00:00Z', 5.1234, 31.675)  # 缺 hr/notes
        deleted = create('export_a', '2026-02-01T02:00:00Z', 8.0, 40, notes='将被软删')
        s, _ = http_json(base, '/api/running/activities/%d' % deleted['id'],
                         tokens['export_a'], method='DELETE')
        assert s == 200
        # 设备来源（GPX）记录：可导出、PATCH 403 只读
        s, jg = http_import(base, tokens['export_a'], 'export-device.gpx', GPX)
        assert s == 201, jg
        gpx_id = jg['activity']['id']
        s, _ = http_json(base, '/api/running/activities/%d' % gpx_id, tokens['export_a'],
                         method='PATCH', body={'distance_km': 9})
        assert s == 403
        act_b = create('export_b', '2026-05-01T02:00:00Z', 20.0, 100, notes='B-UNIQUE-导出隔离')
        a_ids = sorted([act2025['id'], boundary['id'], act2026['id'], gpx_id])

        # HTTP 边界：匿名 401、方法守卫、非法参数 400、owner 伪造无效
        s, _ = http_json(base, '/api/running/export?format=csv')
        assert s == 401
        s, _ = http_json(base, '/api/running/export', tokens['export_a'], method='POST', body={})
        assert s == 405
        s, _ = http_json(base, '/api/running/export?format=xml', tokens['export_a'])
        assert s == 400
        s, _ = http_json(base, '/api/running/export?format=json&year=abc', tokens['export_a'])
        assert s == 400
        s, forged = http_json(base, '/api/running/export?format=json&owner=%d&user_id=%d'
                              % (users['export_a'], users['export_a']), tokens['export_b'])
        assert s == 200 and forged['count'] == 1 and forged['activities'][0]['id'] == act_b['id']

        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', ignore_default_args=['--disable-back-forward-cache'])
            ctx = browser.new_context(viewport={'width': 390, 'height': 844}, accept_downloads=True)
            ctx.route('**/*', lambda r: r.continue_() if urlsplit(r.request.url).netloc == urlsplit(base).netloc else r.abort())
            page = ctx.new_page()

            def login(name):
                # common.js 登录成功后 location.reload()；等 reload 完成＋遮罩隐藏
                # （applyAuthedUI 在 /api/auth/me 校验通过后才隐藏遮罩并触发 onAuthReady）。
                # 不能只等 journalForm：它匿名时也可见，会在登录完成前误判通过。
                page.locator('#loginUsername').fill(name)
                page.locator('#loginPassword').fill(PASSWORD)
                with page.expect_navigation(wait_until='load'):
                    page.locator('#loginBtn').click()
                page.locator('#authOverlay').wait_for(state='hidden', timeout=15000)

            def export_download(fmt, year, dest):
                page.locator('#exportYear').select_option(year)
                btn = '#exportCsvBtn' if fmt == 'csv' else '#exportJsonBtn'
                with page.expect_download(timeout=15000) as dl:
                    page.locator(btn).click()
                dl.value.save_as(str(dest))
                return Path(dest).read_bytes()

            # ---- A：页头导出面板可见（旗标开），年份选项含 全部/2026/2025 ----
            page.goto(base + '/journal')
            login('export_a')
            expect(page.locator('#exportPanel')).to_be_visible(timeout=15000)
            assert page.locator('#exportCsvBtn').is_visible()
            assert page.locator('#exportJsonBtn').is_visible()
            options = page.locator('#exportYear option').all_text_contents()
            assert '全部' in options and '2026' in options and '2025' in options

            # 全部年份 CSV：BOM＋表头＋4 行（软删排除）＋排序＋换算＋缺测留空＋notes 完整
            raw = export_download('csv', '', tmp_path / 'a-all.csv')
            rows = csv_rows(raw)
            assert rows[0] == CSV_HEADER
            assert len(rows) == 5
            ids = [r[0] for r in rows[1:]]
            assert ids == [str(act2025['id']), str(boundary['id']), str(act2026['id']), str(gpx_id)]
            assert str(deleted['id']) not in ids
            assert rows[1][3] == '5.0' and rows[3][3] == '5.1234'
            assert rows[1][8] == notes_a  # 逗号/换行/引号/中文完整还原
            assert rows[3][5] == '' and rows[3][6] == '' and rows[3][8] == ''  # 缺测留空
            assert 'B-UNIQUE' not in raw.decode('utf-8-sig')
            assert rows[4][7] == 'gpx'  # 设备来源记录在导出中

            # 全部年份 JSON：count/字段/null/无 B 数据
            raw = export_download('json', '', tmp_path / 'a-all.json')
            d = json.loads(raw.decode('utf-8'))
            assert set(d) == {'exported_at', 'year', 'count', 'activities'}
            assert d['year'] is None and d['count'] == 4
            datetime.fromisoformat(d['exported_at'])
            assert [a['id'] for a in d['activities']] == a_ids
            third = d['activities'][2]
            assert abs(third['distance_m'] - 5123.4) < 1e-6 and third['distance_km'] == 5.1234
            assert third['avg_hr'] is None and third['elevation_gain_m'] is None and third['notes'] is None
            assert 'B-UNIQUE' not in raw.decode('utf-8')

            # 年份过滤：2025 只有 1 条（北京跨年边界归 2026）；2026 有 3 条
            raw = export_download('csv', '2025', tmp_path / 'a-2025.csv')
            rows = csv_rows(raw)
            assert len(rows) == 2 and rows[1][0] == str(act2025['id'])
            raw = export_download('json', '2026', tmp_path / 'a-2026.json')
            d = json.loads(raw.decode('utf-8'))
            assert d['year'] == 2026 and d['count'] == 3
            assert [a['id'] for a in d['activities']] == sorted([boundary['id'], act2026['id'], gpx_id])

            # 键盘触发：真实 button 聚焦后 Enter 也能下载（先重置年份为全部）
            page.locator('#exportYear').select_option('')
            page.locator('#exportCsvBtn').focus()
            with page.expect_download(timeout=15000) as dl:
                page.keyboard.press('Enter')
            dl.value.save_as(str(tmp_path / 'a-keyboard.csv'))
            assert len(csv_rows(Path(tmp_path / 'a-keyboard.csv').read_bytes())) == 5

            # reload 后面板仍在、导出仍正确
            page.reload()
            expect(page.locator('#exportPanel')).to_be_visible(timeout=15000)
            raw = export_download('json', '', tmp_path / 'a-reload.json')
            assert json.loads(raw.decode('utf-8'))['count'] == 4

            # ---- 登出 → 同名 B：只见本人数据 ----
            with page.expect_navigation(wait_until='load'):
                page.locator('#navLogoutBtn').evaluate('(e)=>e.click()')
            page.goto(base + '/journal')
            login('export_b')
            expect(page.locator('#exportPanel')).to_be_visible(timeout=15000)
            raw = export_download('json', '', tmp_path / 'b-all.json')
            d = json.loads(raw.decode('utf-8'))
            assert d['count'] == 1 and d['activities'][0]['id'] == act_b['id']
            assert d['activities'][0]['notes'] == 'B-UNIQUE-导出隔离'
            text = raw.decode('utf-8')
            for aid in a_ids:
                assert '"id": %d' % aid not in text and '"id":%d' % aid not in text
            assert notes_a not in text
            raw = export_download('csv', '', tmp_path / 'b-all.csv')
            rows = csv_rows(raw)
            assert len(rows) == 2 and rows[1][0] == str(act_b['id'])

            # ---- 开关关闭：API 404＋面板隐藏（页面本身仍可用）----
            with page.expect_navigation(wait_until='load'):
                page.locator('#navLogoutBtn').evaluate('(e)=>e.click()')
            app.config['EXPORT_ENABLED'] = False
            # 登出是全设备吊销（token 版本递增），旧 token 会 401；重新登录取新 token 验 404
            s, j = http_json(base, '/api/auth/login', method='POST',
                             body={'username': 'export_a', 'password': PASSWORD})
            assert s == 200, j
            fresh = j['token']
            s, _ = http_json(base, '/api/running/export?format=csv', fresh)
            assert s == 404
            s, _ = http_json(base, '/api/running/export?format=json', fresh)
            assert s == 404
            page.goto(base + '/journal')
            login('export_a')
            expect(page.locator('#exportPanel')).to_be_hidden()
            assert page.locator('#exportCsvBtn').is_hidden()
            # 恢复开关后面板回来
            app.config['EXPORT_ENABLED'] = True
            page.reload()
            expect(page.locator('#exportPanel')).to_be_visible(timeout=15000)
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=10)