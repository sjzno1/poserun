"""Release gate alongside the light suite. Uses installed Chrome, fresh profile, loopback only."""
import re
from datetime import datetime, timezone
from threading import Thread
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from running.preview import create_app


def test_stats_monthly_pb_trend_and_isolation(tmp_path):
    app = create_app(tmp_path)
    app.config['TRAINING_STATS_ENABLED'] = True
    passwords = 'Synthetic-local-only'
    now = datetime.now(timezone.utc)
    october = datetime(now.year, 10, 15, 6, 0, tzinfo=timezone.utc)
    users = {}
    for name in ('runner_a', 'runner_b'):
        user, err = app.extensions['poserun_auth'].register_user(name, passwords, '同名跑者')
        assert not err
        users[name] = user['id']
    store = app.extensions['running_store']
    # A：本月 12.34km、上月 5km、一条缺距离（不计入合计但计入缺测说明）
    store.add(users['runner_a'], dict(start_time=now.isoformat(),
        source='fit', sport='running', distance_m=12340, duration_s=3600))
    store.add(users['runner_a'], dict(start_time=october.isoformat(),
        source='manual', sport='running', distance_m=5000, duration_s=1500))
    store.add(users['runner_a'], dict(start_time=now.isoformat(),
        source='manual', sport='running', distance_m=None, duration_s=600))
    # B：56.78km，与 A 完全隔离
    store.add(users['runner_b'], dict(start_time=now.isoformat(),
        source='fit', sport='running', distance_m=56780, duration_s=3600))
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', ignore_default_args=['--disable-back-forward-cache'])
            ctx = browser.new_context(viewport={'width': 390, 'height': 844})
            ctx.route('**/*', lambda r: r.continue_() if urlsplit(r.request.url).netloc == urlsplit(base).netloc else r.abort())
            page = ctx.new_page()
            page.goto(base + '/training-stats')

            def login(name):
                page.locator('#loginUsername').fill(name)
                page.locator('#loginPassword').fill(passwords)
                page.locator('#loginBtn').click()

            login('runner_a')
            months = page.locator('#statsMonths')
            # 月度表必须真实渲染（12 行），且含本月/上月合计
            expect(months.locator('tbody tr')).to_have_count(12)
            expect(months).to_contain_text('12.34 km')
            expect(months).to_contain_text('5.00 km')
            expect(months).to_contain_text('距离缺 1')
            # PB：最长距离 12.34km，证据安全措辞，非终身纪录
            pb = page.locator('#statsPb')
            expect(pb).to_contain_text('12.34 km')
            expect(pb).to_contain_text('已导入活动内最佳')
            expect(pb).to_contain_text('非终身纪录')
            expect(pb).to_contain_text('整条活动均配')
            # 周趋势：SVG 存在、空周文字替代、不连线
            trend = page.locator('#statsTrend')
            expect(trend.locator('svg')).to_have_count(1)
            expect(trend).to_contain_text('不连线插值')
            expect(trend.locator('svg')).to_have_attribute('aria-label', re.compile('暂无记录'))
            # 年选择器：切到上一年 → 空年全空
            page.locator('#statsYear').select_option(str(now.year - 1))
            expect(pb).to_contain_text('暂无记录')
            expect(months).to_contain_text('—')
            page.locator('#statsYear').select_option(str(now.year))
            expect(months).to_contain_text('12.34 km')
            # 登出 → B 登录：只见 B 数据
            with page.expect_navigation(wait_until='load'):
                page.locator('#navLogoutBtn').evaluate('(e)=>e.click()')
            expect(page.locator('#loginBtn')).to_be_visible()
            login('runner_b')
            expect(months).to_contain_text('56.78 km')
            expect(months).not_to_contain_text('12.34 km')
            expect(pb).to_contain_text('56.78 km')
            expect(pb).not_to_contain_text('12.34 km')
            page.reload()
            expect(months).to_contain_text('56.78 km')
            expect(months).not_to_contain_text('12.34 km')
            browser.close()
    finally:
        server.shutdown(); thread.join(); server.server_close()
