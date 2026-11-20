"""手工补录页双账号浏览器隔离：换账号/刷新/后退不泄露。AGPL-3.0"""
from threading import Thread
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from running.preview import create_app


def test_manual_entry_switch_reload_and_history(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = True
    passwords = 'Synthetic-local-only'
    for name in ('runner_a', 'runner_b'):
        user, err = app.extensions['poserun_auth'].register_user(name, passwords, '同名跑者')
        assert not err
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', ignore_default_args=['--disable-back-forward-cache'])
            ctx = browser.new_context(viewport={'width': 390, 'height': 844})
            ctx.route('**/*', lambda r: r.continue_() if urlsplit(r.request.url).netloc == urlsplit(base).netloc else r.abort())
            page = ctx.new_page()
            page.goto(base + '/manual-entry')
            def login(name):
                page.locator('#loginUsername').fill(name)
                page.locator('#loginPassword').fill(passwords)
                with page.expect_navigation(wait_until='load'):
                    page.locator('#loginBtn').click()
            login('runner_a')
            expect(page.locator('#manualEmpty')).to_be_visible()
            page.locator('#manualStart').fill('2026-11-16T07:30')
            page.locator('#manualDistance').fill('5.2')
            page.locator('#manualDuration').fill('31')
            page.locator('#manualNotes').fill('晨跑 A')
            page.locator('#manualSubmit').click()
            expect(page.locator('#manualList')).to_contain_text('5.20 km')
            expect(page.locator('#manualList')).to_contain_text('晨跑 A')
            expect(page.locator('#manualList')).to_contain_text('手工记录 · 未核验')
            # 换账号：退出后 runner_b 看不到 runner_a 的记录
            with page.expect_navigation(wait_until='load'):
                page.locator('#navLogoutBtn').evaluate('(e)=>e.click()')
            expect(page.locator('#loginBtn')).to_be_visible()
            login('runner_b')
            expect(page.locator('#manualEmpty')).to_be_visible()
            expect(page.locator('#manualList li')).to_have_count(0)
            # 后退场景：真实跨页导航后再返回（镜像周报测试）
            page.get_by_role('link', name='返回总览').click()
            expect(page.locator('#weeklyKm')).to_be_visible()
            page.go_back()
            for refresh in (False, True):
                if refresh:
                    page.reload()
                expect(page.locator('#manualEmpty')).to_be_visible()
                expect(page.locator('#manualList li')).to_have_count(0)
            browser.close()
    finally:
        server.shutdown(); thread.join(); server.server_close()
