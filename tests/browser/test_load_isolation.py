"""P2 切片7 训练负荷真浏览器隔离测试（AGPL-3.0）。

Flask 线程 + tmp_path DB + playwright(channel=chrome)：登录→/training-load→
SVG 真实挂载（三条 path、点数>0）→summary 与 API 一致（独立 Fraction 手算参考）→
days 联动→reload→窄屏/宽屏→三主题→同名 A/B 隔离＋空态→owner 伪造→
开关关闭 API/页面 404＋导航/入口隐藏→恢复。外部请求=0（仅总览页既有 bootcdn
字体样式为历史基线，被路由拦截且白名单单列，负荷页自身零外链）。
"""
import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from fractions import Fraction as F
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from running.preview import create_app

PASSWORD = 'Synthetic-local-only'
BJT = timezone(timedelta(hours=8))
# 总览页 index.html 既有的 bootcdn 字体样式（历史基线，非本切片引入；被 ctx.route 拦截）。
LEGACY_EXTERNAL = 'https://cdn.bootcdn.net/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css'


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


def ref_ewma(daily, start, end):
    """独立手算参考：Fraction 精确递推，种子0，TSB_n=CTL_{n-1}-ATL_{n-1}。"""
    ctl = atl = F(0)
    last = None
    d = start
    while d <= end:
        L = F(str(daily.get(d, 0.0)))
        tsb = ctl - atl
        ctl = ctl + (L - ctl) / F(42)
        atl = atl + (L - atl) / F(7)
        last = (float(ctl), float(atl), float(tsb))
        d += timedelta(days=1)
    return last


def test_load_isolation_browser(tmp_path):
    app = create_app(tmp_path)
    for flag in ('WEEKLY_REPORT_ENABLED', 'MANUAL_ENTRY_ENABLED', 'GEAR_ENABLED',
                 'TRAINING_STATS_ENABLED', 'TRACK_MAP_ENABLED', 'EXPORT_ENABLED', 'LOAD_ENABLED'):
        app.config[flag] = True
    auth = app.extensions['poserun_auth']
    store = app.extensions['running_store']
    users = {}
    for name in ('load_a', 'load_b'):
        user, err = auth.register_user(name, PASSWORD, '负荷浏览器同名')
        assert not err
        users[name] = user['id']
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        tokens = {}
        for name in ('load_a', 'load_b'):
            s, j = http_json(base, '/api/auth/login', method='POST',
                             body={'username': name, 'password': PASSWORD})
            assert s == 200, j
            tokens[name] = j['token']

        # A：跨 10 天 5 次含心率活动（60min hr150 → 每次 TRIMP 男参考值）
        now_utc = datetime.now(timezone.utc)
        today = now_utc.astimezone(BJT).date()
        offsets = [9, 6, 3, 1, 0]
        dhr = (150 - 60) / (190 - 60)
        load_each = 60 * dhr * 0.64 * math.exp(1.92 * dhr)  # 独立直式 ≈100.44029272374173
        a_ids = []
        for k in offsets:
            start = (now_utc - timedelta(days=k, hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
            s, j = http_json(base, '/api/running/activities', tokens['load_a'], method='POST',
                             body={'start_time': start, 'distance_km': 5.0,
                                   'duration_min': 60, 'avg_hr': 150,
                                   'notes': 'A-UNIQUE-负荷隔离' if k == 9 else None})
            assert s == 201, j
            a_ids.append(j['activity']['id'])
        daily = {today - timedelta(days=k): load_each for k in offsets}
        first = today - timedelta(days=9)
        ref_ctl, ref_atl, ref_tsb = ref_ewma(daily, first, today)
        ref_load7d = sum(v for d, v in daily.items() if d >= today - timedelta(days=6))
        ref_ratio = ref_atl / ref_ctl

        # HTTP 边界：匿名 401、非法 days 400、方法守卫、owner 伪造无效
        s, _ = http_json(base, '/api/running/load')
        assert s == 401
        for bad in ('6', '366', 'abc', ''):
            s, _ = http_json(base, '/api/running/load?days=' + bad, tokens['load_a'])
            assert s == 400, bad
        s, _ = http_json(base, '/api/running/load', tokens['load_a'], method='POST', body={})
        assert s == 405
        s, forged = http_json(base, '/api/running/load?days=90&owner=%d&user_id=%d'
                              % (users['load_a'], users['load_a']), tokens['load_b'])
        assert s == 200 and all(p['daily_load'] == 0.0 and p['activities'] == 0 for p in forged['series']) \
            and len(forged['series']) == 90  # B 尚无活动→全 0 窗口，伪造参数拿不到 A 数据

        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', ignore_default_args=['--disable-back-forward-cache'])
            ctx = browser.new_context(viewport={'width': 390, 'height': 844})
            externals = []

            def route(r):
                if urlsplit(r.request.url).netloc == urlsplit(base).netloc:
                    r.continue_()
                else:
                    externals.append(r.request.url)
                    r.abort()

            ctx.route('**/*', route)
            page = ctx.new_page()

            def login(name):
                # common.js 登录成功后 location.reload()；等 reload 完成＋遮罩隐藏。
                # 不能只等页面表单：匿名时也可见，会在登录完成前误判通过。
                page.locator('#loginUsername').fill(name)
                page.locator('#loginPassword').fill(PASSWORD)
                with page.expect_navigation(wait_until='load'):
                    page.locator('#loginBtn').click()
                page.locator('#authOverlay').wait_for(state='hidden', timeout=15000)

            def path_points():
                return page.evaluate("""() => [...document.querySelectorAll('#loadChart path.ld-line')]
                    .map(p => (p.getAttribute('d').match(/[ML]/g) || []).length)""")

            def wait_chart(n):
                page.wait_for_function(
                    """(n) => { const ps = document.querySelectorAll('#loadChart path.ld-line');
                        return ps.length === 3 &&
                          [...ps].every(p => (p.getAttribute('d').match(/[ML]/g) || []).length === n); }""",
                    arg=n, timeout=15000)

            # ---- A：真实 SVG 挂载＋summary 与 API/手算参考一致 ----
            page.goto(base + '/training-load')
            login('load_a')
            wait_chart(90)  # 固定 90 天窗口（含零负荷日），today-89 至 today
            assert page.locator('#loadChart svg').get_attribute('role') == 'img'
            assert len(page.locator('#loadChart svg').get_attribute('aria-label') or '') > 10
            api = page.evaluate("""async () => {
                const r = await fetch('/api/running/load?days=90',
                    {cache: 'no-store', headers: {Authorization: 'Bearer ' + localStorage.getItem('poserun_token')}});
                return {status: r.status, body: await r.json()};
            }""")
            assert api['status'] == 200
            s = api['body']['summary']
            assert abs(s['current_ctl'] - ref_ctl) < 1e-6, (s['current_ctl'], ref_ctl)
            assert abs(s['current_atl'] - ref_atl) < 1e-6
            assert abs(s['current_tsb'] - ref_tsb) < 1e-6
            assert abs(s['load_7d'] - ref_load7d) < 1e-6
            assert abs(s['acute_chronic_ratio'] - ref_ratio) < 1e-6
            assert len(api['body']['series']) == 90
            # DOM 卡片数值与 API 一致（按页面 toFixed(2) 格式）
            for key in ('current_ctl', 'current_atl', 'current_tsb'):
                want = page.evaluate('(v) => v.toFixed(2)', s[key])
                expect(page.locator('#loadSummary')).to_contain_text(want, timeout=10000)
            expect(page.locator('#loadSummary')).to_contain_text(
                page.evaluate('(v) => v.toFixed(1)', s['load_7d']))
            expect(page.locator('#loadInterpretation')).to_have_text(s['interpretation'])
            text = page.locator('#loadAssumptions').inner_text()
            for want in ('190', '60', 'TRIMP', '42', 'TSB', '0.5'):
                assert want in text, want
            assert 'A-UNIQUE' not in page.content()  # 负荷 payload 不含 notes

            # days 联动：7 天窗口截断为 7 点；90 天恢复 90 点
            page.locator('#loadDays').select_option('7')
            wait_chart(7)
            page.locator('#loadDays').select_option('90')
            wait_chart(90)

            # reload 后仍真实渲染
            page.reload()
            page.locator('#authOverlay').wait_for(state='hidden', timeout=15000)
            wait_chart(90)

            # 窄屏无横向溢出；宽屏正常
            assert page.evaluate('document.documentElement.scrollWidth') <= 391
            page.set_viewport_size({'width': 1280, 'height': 900})
            wait_chart(90)
            assert page.evaluate('document.documentElement.scrollWidth') <= 1281
            page.set_viewport_size({'width': 390, 'height': 844})

            # 三主题：线色来自 token，随主题变化且非空
            strokes = []
            for theme in ('clarion', 'nightlab', 'fieldnote'):
                page.evaluate('(t) => document.documentElement.setAttribute("data-theme", t)', theme)
                page.wait_for_timeout(120)
                stroke = page.evaluate("""() => getComputedStyle(
                    document.querySelector('#loadChart path.ld-line')).stroke""")
                assert stroke and stroke != 'none', theme
                strokes.append(stroke)
            assert len(set(strokes)) == 3, strokes
            page.evaluate('() => document.documentElement.setAttribute("data-theme", "clarion")')

            # 负荷页阶段外部请求必须为 0
            assert externals == [], externals

            # ---- 登出 → 同名 B：空态，不见 A 数据 ----
            with page.expect_navigation(wait_until='load'):
                page.locator('#navLogoutBtn').evaluate('(e)=>e.click()')
            page.goto(base + '/training-load')
            login('load_b')
            expect(page.locator('#loadEmpty')).to_be_visible(timeout=15000)
            assert page.locator('#loadChart path.ld-line').count() == 0
            expect(page.locator('#loadInterpretation')).to_have_text('暂无活动记录，无法评估训练负荷。')
            assert page.evaluate('(v) => v.toFixed(2)', ref_ctl) not in page.locator('#loadSummary').inner_text()

            # B 建一条本人活动（30min 无 hr → fallback 15）→ reload 只见本人形态
            s, j = http_json(base, '/api/running/activities', tokens['load_b'], method='POST',
                             body={'start_time': (now_utc - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                   'distance_km': 3.0, 'duration_min': 30, 'notes': 'B-UNIQUE-负荷隔离'})
            assert s == 201, j
            page.reload()
            page.locator('#authOverlay').wait_for(state='hidden', timeout=15000)
            wait_chart(90)
            expect(page.locator('#loadSummary')).to_contain_text(
                page.evaluate('(v) => v.toFixed(2)', 15.0 / 42))
            assert page.evaluate('(v) => v.toFixed(2)', ref_ctl) not in page.locator('#loadSummary').inner_text()
            assert 'A-UNIQUE' not in page.content() and 'B-UNIQUE' not in page.content()

            # ---- 开关关闭：页面/API 404＋导航与总览入口隐藏 ----
            with page.expect_navigation(wait_until='load'):
                page.locator('#navLogoutBtn').evaluate('(e)=>e.click()')
            app.config['LOAD_ENABLED'] = False
            resp = page.goto(base + '/training-load')
            assert resp.status == 404
            # 登出是全设备吊销；重新登录取新 token 验 API 404
            s, j = http_json(base, '/api/auth/login', method='POST',
                             body={'username': 'load_a', 'password': PASSWORD})
            assert s == 200, j
            fresh = j['token']
            s, _ = http_json(base, '/api/running/load?days=90', fresh)
            assert s == 404
            page.goto(base + '/')
            login('load_a')
            expect(page.locator('#loadEntry')).to_be_hidden()
            assert page.evaluate("""() => getComputedStyle(document.getElementById('navLoadItem')).display""") == 'none'

            # ---- 恢复开关：入口回来，页面可用 ----
            app.config['LOAD_ENABLED'] = True
            page.reload()
            page.locator('#authOverlay').wait_for(state='hidden', timeout=15000)
            expect(page.locator('#loadEntry')).to_be_visible(timeout=15000)
            assert page.evaluate("""() => getComputedStyle(document.getElementById('navLoadItem')).display""") != 'none'
            page.goto(base + '/training-load')
            page.locator('#authOverlay').wait_for(state='hidden', timeout=15000)
            wait_chart(90)
            browser.close()

            # 外部请求白名单：仅总览页既有 bootcdn 字体样式（历史基线，已被拦截）
            assert set(externals) <= {LEGACY_EXTERNAL}, externals
    finally:
        server.shutdown()
        thread.join(timeout=10)