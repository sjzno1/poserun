'use strict';
// 单元测试替身：Node DOM / mediaDevices / apiFetch 桩，不调用硬件、网络或数据库。
// 直接执行真实 HTML 的内联脚本；不构成真实浏览器、摄像头或科学测量验收。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
function eventTarget(extra = {}) {
    const handlers = {};
    return Object.assign(extra, {
        addEventListener(name, fn) { (handlers[name] ||= []).push(fn); },
        dispatch(name) { return Promise.all((handlers[name] || []).map(fn => fn())); }
    });
}
function page(name, overrides = {}) {
    const html = fs.readFileSync(path.join(root, 'static', name + '.html'), 'utf8');
    const elements = {};
    for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
        const attrs = {};
        elements[match[1]] = eventTarget({
            innerHTML: '', textContent: '', disabled: /\sdisabled(?:\s|>|=)/.test(match[0]),
            srcObject: null, style: { display: /display:\s*none/.test(match[0]) ? 'none' : '' },
            setAttribute(key, value) { attrs[key] = value; },
            getAttribute(key) { return attrs[key]; }
        });
    }
    const calls = [];
    const window = eventTarget({ isSecureContext: true });
    const context = vm.createContext({
        window, document: { getElementById(id) { assert.ok(elements[id], '真实 HTML 必须包含 ' + id); return elements[id]; } },
        navigator: { mediaDevices: { getUserMedia() { throw new Error('未经点击不应访问摄像头'); } } },
        apiFetch: async (...args) => { calls.push(args); throw new Error('未配置 API 替身'); },
        escapeHtml: value => String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
        console, ...overrides
    });
    for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
        if (!/\bsrc\s*=/.test(match[1])) vm.runInContext(match[2], context, { filename: name + '.html' });
    }
    return { html, elements, window, context, calls, run: code => vm.runInContext(code, context) };
}
function stream() {
    const tracks = [{ stops: 0, stop() { this.stops++; } }, { stops: 0, stop() { this.stops++; } }];
    return { tracks, getTracks() { return tracks; } };
}
function camera(getUserMedia) {
    return page('camera', { navigator: { mediaDevices: { getUserMedia } } });
}
function response(data, ok = true) { return { ok, json: async () => data }; }
function training(queue) {
    const calls = [];
    const p = page('training', { apiFetch: async (...args) => {
        calls.push(args);
        assert.ok(queue.length, 'API 替身响应队列已用尽');
        const next = queue.shift();
        if (next instanceof Error) throw next;
        return next;
    } });
    p.calls = calls;
    return p;
}
const history = () => response({ records: [{ id: 17 }] });
const insufficient = () => response({ session: { status: 'insufficient_evidence', weaknesses: [], drills: [], strength: [], weekly_plan: [], notes: ['证据不足，请勿据此开具处方'] } });
function recovered(p) {
    assert.equal(p.elements.generateSessionBtn.disabled, false);
    assert.equal(p.elements.refreshSessionBtn.disabled, false);
    assert.doesNotMatch(p.elements.generateSessionBtn.innerHTML, /生成中|检查中|已生成/);
    assert.equal(p.elements.sessionBody.getAttribute('aria-busy'), 'false');
}

test('camera：诚实说明、上传入口和用户图标资源保持', () => {
    let calls = 0;
    const p = camera(() => { calls++; });
    assert.equal(calls, 0);
    assert.match(p.html, /摄像头预览/);
    assert.match(p.html, /href="\/upload.html"/);
    assert.match(p.html, /font-awesome\/4\.7\.0/);
    assert.match(p.html, /\/icons.js/);
    assert.doesNotMatch(p.html, /8443|实时摄像头分析|按 'q'|alert\(/);
    assert.equal(p.window.PAGE, 'camera');
});

test('camera：点击成功；重复启动不重复申请；停止释放所有轨道并清空 srcObject', async () => {
    const s = stream(); let calls = 0;
    const p = camera(async options => { calls++; assert.equal(options.audio, false); return s; });
    await p.elements.startCameraBtn.dispatch('click');
    assert.equal(p.elements.cameraVideo.srcObject, s);
    assert.equal(p.elements.cameraContainer.style.display, 'block');
    await p.run('startCamera()');
    assert.equal(calls, 1);
    await p.elements.stopCameraBtn.dispatch('click');
    assert.deepEqual(s.tracks.map(t => t.stops), [1, 1]);
    assert.equal(p.elements.cameraVideo.srcObject, null);
    assert.equal(p.elements.cameraContainer.style.display, 'none');
    assert.equal(p.elements.startCameraBtn.disabled, false);
});

test('camera：启动未决时重复点击只发一个请求', async () => {
    const d = deferred(); let calls = 0;
    const p = camera(() => { calls++; return d.promise; });
    const first = p.run('startCamera()');
    await p.run('startCamera()');
    assert.equal(calls, 1);
    assert.equal(p.elements.startCameraBtn.disabled, true);
    d.resolve(stream()); await first;
    await p.run('stopCamera()');
});

for (const name of ['NotAllowedError', 'NotFoundError', 'NotReadableError']) {
    test('camera：' + name + ' 内联错误并恢复可重试', async () => {
        const p = camera(async () => { throw { name }; });
        await p.run('startCamera()');
        assert.equal(p.elements.cameraError.style.display, 'block');
        assert.ok(p.elements.cameraError.textContent.length);
        assert.equal(p.elements.startCameraBtn.disabled, false);
        assert.equal(p.elements.cameraVideo.srcObject, null);
    });
}

test('camera：拒绝后重试成功清除旧错误', async () => {
    let calls = 0; const s = stream();
    const p = camera(async () => { if (++calls === 1) throw { name: 'NotAllowedError' }; return s; });
    await p.run('startCamera()'); await p.run('startCamera()');
    assert.equal(p.elements.cameraError.style.display, 'none');
    assert.equal(p.elements.cameraVideo.srcObject, s);
    await p.run('stopCamera()');
});

test('camera：非安全上下文不申请设备、不猜 HTTPS 端口', async () => {
    let calls = 0; const p = camera(() => { calls++; });
    p.window.isSecureContext = false;
    await p.run('startCamera()');
    assert.equal(calls, 0);
    assert.match(p.elements.cameraError.textContent, /安全上下文/);
    assert.doesNotMatch(p.elements.cameraError.textContent, /8443|继续前往/);
});

test('camera：不支持 mediaDevices 时内联解释', async () => {
    const p = page('camera', { navigator: {} });
    await p.run('startCamera()');
    assert.match(p.elements.cameraError.textContent, /不支持/);
    assert.equal(p.elements.startCameraBtn.disabled, false);
});

for (const event of ['pagehide', 'beforeunload']) {
    test('camera：启动期间快速离开 ' + event + ' 会停止迟到流', async () => {
        const d = deferred(); const s = stream(); const p = camera(() => d.promise);
        const start = p.run('startCamera()');
        await p.window.dispatch(event);
        d.resolve(s); await start;
        assert.deepEqual(s.tracks.map(t => t.stops), [1, 1]);
        assert.equal(p.elements.cameraVideo.srcObject, null);
        assert.equal(p.elements.cameraContainer.style.display, 'none');
    });
}

test('camera：取消未决请求不重开；迟到响应关闭后允许手动重试', async () => {
    const d = deferred(); const s = stream(); let calls = 0;
    const p = camera(() => { calls++; return calls === 1 ? d.promise : Promise.resolve(stream()); });
    const start = p.run('startCamera()');
    await p.run('stopCamera()'); await p.run('startCamera()');
    assert.equal(calls, 1);
    d.resolve(s); await start;
    assert.deepEqual(s.tracks.map(t => t.stops), [1, 1]);
    assert.match(p.elements.cameraStatus.textContent, /已停止/);
    assert.equal(p.elements.startCameraBtn.disabled, false);
    await p.run('startCamera()'); assert.equal(calls, 2);
    await p.run('stopCamera()');
});

test('camera：离开后迟到拒绝不污染页面；pageshow 不自动申请', async () => {
    const d = deferred(); let calls = 0;
    const p = camera(() => { calls++; return d.promise; });
    const start = p.run('startCamera()'); await p.window.dispatch('pagehide');
    d.reject({ name: 'NotAllowedError' }); await start;
    assert.equal(p.elements.cameraError.style.display, 'none');
    await p.window.dispatch('pageshow');
    assert.equal(calls, 1);
    assert.equal(p.elements.startCameraBtn.disabled, false);
});

test('camera：已启动后离开释放流；双重卸载不重复释放', async () => {
    const s = stream(); const p = camera(async () => s);
    await p.run('startCamera()');
    await p.window.dispatch('pagehide'); await p.window.dispatch('beforeunload');
    assert.deepEqual(s.tracks.map(t => t.stops), [1, 1]);
    assert.equal(p.elements.cameraVideo.srcObject, null);
});

test('training：认证前不加载；无记录明确空态和按钮恢复', async () => {
    const p = training([response({ records: [] })]);
    assert.equal(p.calls.length, 0);
    await p.run('onAuthReady({id: 1})');
    assert.equal(p.calls.length, 1);
    assert.match(p.elements.sessionAlerts.innerHTML, /暂无分析记录/);
    assert.match(p.elements.sessionBody.innerHTML, /上传/);
    recovered(p);
});

test('training：insufficient_evidence 成功响应是中性缺证据，不是生成成功', async () => {
    const p = training([history(), insufficient()]);
    await p.run('loadSession()');
    assert.equal(p.calls[1][0], '/api/training/session');
    assert.equal(p.calls[1][1].method, 'POST');
    assert.equal(JSON.parse(p.calls[1][1].body).record_id, 17);
    assert.match(p.elements.sessionAlerts.innerHTML, /alert-secondary/);
    assert.match(p.elements.sessionAlerts.innerHTML, /证据不足/);
    assert.match(p.elements.sessionBody.innerHTML, /证据不足，请勿据此开具处方/);
    assert.equal((p.elements.sessionBody.innerHTML.match(/暂无可支持的建议/g) || []).length, 4);
    assert.doesNotMatch(p.elements.sessionAlerts.innerHTML, /alert-success|已生成训练/);
    recovered(p);
});

test('training：旧未验证会话即使含有处方也不展示或补齐', async () => {
    const p = training([history(), response({ session: { weaknesses: ['UNSAFE_WEAKNESS'], drills: [{ name: 'UNSAFE_DRILL' }], strength: ['UNSAFE_STRENGTH'], weekly_plan: [['UNSAFE_PLAN']], notes: ['UNSAFE_NOTE'] } })]);
    await p.run('generateSession()');
    assert.doesNotMatch(p.elements.sessionBody.innerHTML, /UNSAFE_/);
    assert.match(p.elements.sessionAlerts.innerHTML, /证据不足/);
    recovered(p);
});

test('training：会话缺少可选字段不崩溃、不出现空白', async () => {
    const p = training([history(), response({ session: {} })]);
    await p.run('loadSession()');
    assert.match(p.elements.sessionBody.innerHTML, /暂无可支持/);
    recovered(p);
});

for (const [name, failure] of [
    ['网络失败', new Error('offline')],
    ['HTTP 失败', response({ error: '<img src=x onerror=bad>' }, false)],
    ['JSON 失败', { ok: true, json: async () => { throw new Error('Invalid JSON'); } }],
    ['格式失败', response({ records: null })]
]) {
    test('training：' + name + ' 后刷新能恢复且清除旧错误', async () => {
        const queue = [failure, history(), insufficient()]; const p = training(queue);
        await p.elements.refreshSessionBtn.dispatch('click');
        assert.match(p.elements.sessionAlerts.innerHTML, /加载失败/);
        assert.doesNotMatch(p.elements.sessionAlerts.innerHTML, /<img/);
        recovered(p);
        await p.elements.refreshSessionBtn.dispatch('click');
        assert.match(p.elements.sessionAlerts.innerHTML, /证据不足/);
        assert.doesNotMatch(p.elements.sessionAlerts.innerHTML, /加载失败/);
        recovered(p);
    });
}

for (const [name, failure] of [
    ['服务失败', response({ error: '训练服务不可用' }, false)],
    ['会话缺失', response({})],
    ['会话非对象', response({ session: [] })]
]) {
    test('training：' + name + ' 恢复按钮并不显示旧内容', async () => {
        const p = training([history(), insufficient(), history(), failure]);
        await p.run('loadSession()');
        await p.run('loadSession()');
        assert.match(p.elements.sessionAlerts.innerHTML, /加载失败/);
        assert.doesNotMatch(p.elements.sessionBody.innerHTML, /暂无可支持的建议/);
        assert.equal(p.run('currentSession'), null);
        recovered(p);
    });
}

test('training：重复触发期间只有一组请求、两按钮禁用，结束恢复', async () => {
    const d = deferred(); const p = training([d.promise, insufficient()]);
    const first = p.run('loadSession()');
    await p.run('generateSession()'); await p.run('loadSession()');
    assert.equal(p.calls.length, 1);
    assert.equal(p.elements.generateSessionBtn.disabled, true);
    assert.equal(p.elements.refreshSessionBtn.disabled, true);
    d.resolve(history()); await first;
    assert.equal(p.calls.length, 2);
    recovered(p);
});

test('training：刷新发现无记录时清空上次会话', async () => {
    const p = training([history(), insufficient(), response({ records: [] })]);
    await p.run('loadSession()'); await p.run('loadSession()');
    assert.equal(p.run('currentSession'), null);
    assert.match(p.elements.sessionAlerts.innerHTML, /暂无分析记录/);
    assert.doesNotMatch(p.elements.sessionBody.innerHTML, /暂无可支持的建议/);
    recovered(p);
});

test('training：安全说明 HTML 转义，资源与上传入口保留', async () => {
    const p = training([history(), response({ session: { status: 'insufficient_evidence', notes: ['<script>bad()</script>'] } })]);
    await p.run('loadSession()');
    assert.match(p.elements.sessionBody.innerHTML, /&lt;script&gt;/);
    assert.doesNotMatch(p.elements.sessionBody.innerHTML, /<script>/);
    assert.match(p.html, /font-awesome\/4\.7\.0/);
    assert.match(p.html, /\/icons.js/);
    assert.match(p.html, /href="\/upload.html"/);
    assert.equal(p.window.PAGE, 'training');
});
