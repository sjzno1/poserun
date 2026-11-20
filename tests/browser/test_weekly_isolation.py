"""Release gate alongside the light suite. Uses installed Chrome, fresh profile, loopback only."""
from datetime import datetime, timezone
from threading import Thread
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from running.preview import create_app


def test_weekly_switch_reload_and_history(tmp_path):
    app = create_app(tmp_path)
    app.config['WEEKLY_REPORT_ENABLED'] = True
    passwords = 'Synthetic-local-only'
    for name, distance in [('runner_a', 12340), ('runner_b', 56780)]:
        user, err = app.extensions['poserun_auth'].register_user(name, passwords, '同名跑者')
        assert not err
        app.extensions['running_store'].add(user['id'], dict(start_time=datetime.now(timezone.utc).isoformat(),
            source='fit', sport='running', distance_m=distance, duration_s=3600))
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', ignore_default_args=['--disable-back-forward-cache'])
            ctx = browser.new_context(viewport={'width': 390, 'height': 844})
            ctx.route('**/*', lambda r: r.continue_() if urlsplit(r.request.url).netloc == urlsplit(base).netloc else r.abort())
            page = ctx.new_page()
            page.goto(base+'/weekly-report')
            def login(name):
                page.locator('#loginUsername').fill(name)
                page.locator('#loginPassword').fill(passwords)
                page.locator('#loginBtn').click()
            login('runner_a')
            expect(page.locator('#reportWeeks')).to_contain_text('12.34 km')
            expect(page.locator('#reportWeeks')).to_contain_text('暂无记录')
            page.locator('#weekZone').select_option('UTC')
            expect(page.locator('#reportWeeks')).to_contain_text('UTC')
            page.get_by_role('link', name='返回总览').click()
            expect(page.locator('#weeklyKm')).to_have_text('12.34 km')
            # Use the actual logout handler even if the narrow-screen menu is collapsed.
            with page.expect_navigation(wait_until='load'):
                page.locator('#navLogoutBtn').evaluate('(e)=>e.click()')
            expect(page.locator('#loginBtn')).to_be_visible()
            login('runner_b')
            expect(page.locator('#weeklyKm')).to_have_text('56.78 km')
            page.go_back()
            for refresh in (False, True):
                if refresh:
                    page.reload()
                report = page.locator('#reportWeeks')
                expect(report).to_contain_text('56.78 km')
                expect(report).not_to_contain_text('12.34 km')
                expect(report).to_contain_text('Asia/Shanghai')
                expect(report.locator('a')).to_have_count(1)
                expect(report.locator('a')).to_have_attribute('href', '/activities.html#2')
            browser.close()
    finally:
        server.shutdown(); thread.join(); server.server_close()
