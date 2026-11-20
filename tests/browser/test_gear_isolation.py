"""跑鞋＋训练日志页双账号浏览器隔离：换账号/刷新/后退不泄露；私密备注不出现在页面。AGPL-3.0"""
from threading import Thread
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from running.preview import create_app

PASSWORD = 'Synthetic-local-only'
PRIVATE = '私密-浏览器隔离-勿泄露'


def serve(app):
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever); thread.start()
    return server, thread, f'http://127.0.0.1:{server.server_port}'


def make_app(tmp_path):
    app = create_app(tmp_path)
    app.config['GEAR_ENABLED'] = True
    app.config['MANUAL_ENTRY_ENABLED'] = True
    app.config['WEEKLY_REPORT_ENABLED'] = True
    for name in ('runner_a', 'runner_b'):
        user, err = app.extensions['poserun_auth'].register_user(name, PASSWORD, '同名跑者')
        assert not err
    return app


def login(page, name):
    page.locator('#loginUsername').fill(name)
    page.locator('#loginPassword').fill(PASSWORD)
    with page.expect_navigation(wait_until='load'):
        page.locator('#loginBtn').click()


def logout(page):
    with page.expect_navigation(wait_until='load'):
        page.locator('#navLogoutBtn').evaluate('(e)=>e.click()')
    expect(page.locator('#loginBtn')).to_be_visible()


def test_gear_switch_reload_and_history(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions['running_store']
    auth = app.extensions['poserun_auth']
    users = {u['username']: u for u in auth.list_users()}
    for name, sha in (('runner_a', 'gear-iso-a'), ('runner_b', 'gear-iso-b')):
        store.add(users[name]['id'], dict(start_time='2026-11-16T07:30:00+08:00', sport='running',
                                          source='fit', distance_m=5000, duration_s=1800, file_sha256=sha))
    server, thread, base = serve(app)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', ignore_default_args=['--disable-back-forward-cache'])
            ctx = browser.new_context(viewport={'width': 390, 'height': 844})
            ctx.route('**/*', lambda r: r.continue_() if urlsplit(r.request.url).netloc == urlsplit(base).netloc else r.abort())
            page = ctx.new_page()
            page.goto(base + '/gear')
            login(page, 'runner_a')
            expect(page.locator('#gearEmpty')).to_be_visible()
            page.locator('#gearName').fill('隔离测试鞋 A')
            page.locator('#gearSubmit').click()
            expect(page.locator('#gearList')).to_contain_text('隔离测试鞋 A')
            expect(page.locator('#gearList')).to_contain_text('0.00 km')
            expect(page.locator('#gearList')).to_contain_text('已导入活动内合计 · 未核验')
            # 关联本人活动后里程出现
            expect(page.locator('#linkActivity option')).to_have_count(1)
            page.locator('#linkSubmit').click()
            expect(page.locator('#gearList')).to_contain_text('5.00 km')
            # 换账号：runner_b 看不到 runner_a 的鞋与里程
            logout(page)
            login(page, 'runner_b')
            expect(page.locator('#gearEmpty')).to_be_visible()
            expect(page.locator('#gearList li')).to_have_count(0)
            expect(page.locator('#linkGear')).to_contain_text('暂无可选跑鞋')
            # 后退＋刷新不泄露
            page.get_by_role('link', name='返回总览').click()
            expect(page.locator('#weeklyKm')).to_be_visible()
            page.go_back()
            for refresh in (False, True):
                if refresh:
                    page.reload()
                expect(page.locator('#gearEmpty')).to_be_visible()
                expect(page.locator('#gearList li')).to_have_count(0)
            browser.close()
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_journal_switch_reload_history_and_private_never_rendered(tmp_path):
    app = make_app(tmp_path)
    server, thread, base = serve(app)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', ignore_default_args=['--disable-back-forward-cache'])
            ctx = browser.new_context(viewport={'width': 390, 'height': 844})
            ctx.route('**/*', lambda r: r.continue_() if urlsplit(r.request.url).netloc == urlsplit(base).netloc else r.abort())
            page = ctx.new_page()
            page.goto(base + '/journal')
            login(page, 'runner_a')
            expect(page.locator('#journalEmpty')).to_be_visible()
            page.locator('#journalDate').fill('2026-11-18')
            page.locator('#journalBody').fill('甲的训练体会')
            page.locator('#journalPrivate').fill(PRIVATE)
            page.locator('#journalSubmit').click()
            expect(page.locator('#journalList')).to_contain_text('甲的训练体会')
            expect(page.locator('#journalList')).to_contain_text('含私密备注（仅本人可见）')
            expect(page.locator('#journalList')).to_contain_text('跑者自填 · 未核验')
            # 私密备注绝不渲染在列表页
            assert PRIVATE not in page.content()
            # 换账号：runner_b 看不到 runner_a 的日志
            logout(page)
            login(page, 'runner_b')
            expect(page.locator('#journalEmpty')).to_be_visible()
            expect(page.locator('#journalList li')).to_have_count(0)
            # 后退＋刷新不泄露
            page.get_by_role('link', name='返回总览').click()
            expect(page.locator('#weeklyKm')).to_be_visible()
            page.go_back()
            for refresh in (False, True):
                if refresh:
                    page.reload()
                expect(page.locator('#journalEmpty')).to_be_visible()
                expect(page.locator('#journalList li')).to_have_count(0)
                assert PRIVATE not in page.content()
            browser.close()
    finally:
        server.shutdown(); thread.join(); server.server_close()