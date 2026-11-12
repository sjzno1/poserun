'use strict';
// Execute the actual inline script; DOM/network doubles do not run a browser/server.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../static/upload.html'), 'utf8');
const home = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]);
assert.ok(scripts.some(script => script.includes('let currentFileName')));
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
function harness() {
    class Element {
        constructor(attrs = '') {
            this.style = {}; this.listeners = {}; this.value = '';
            this.disabled = /\bdisabled\b/.test(attrs); this.textContent = '';
            this.hidden = /\bhidden\b/.test(attrs);
            const style = attrs.match(/style="([^"]*)"/);
            if (style && /display:\s*none/.test(style[1])) this.style.display = 'none';
        }
        addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
        async emit(type, event = {}) {
            for (const callback of this.listeners[type] || []) {
                await callback({ target: this, preventDefault() {}, ...event });
            }
        }
        click() { return this.disabled ? Promise.resolve() : this.emit('click'); }
        pause() { this.paused = true; }
        removeAttribute(name) { delete this[name]; }
        load() { this.loads = (this.loads || 0) + 1; }
    }
    const elements = Object.fromEntries([...html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)]
        .map(match => [match[1], new Element(match[0])]));
    elements.reportFormatSelect.value = 'pdf';
    const calls = [], revoked = [], alerts = [], storage = [];
    let url = 0;
    const context = vm.createContext({
        document: { getElementById: id => elements[id] || null },
        window: new Element(), console: { error() {} }, AbortController,
        FormData: class { constructor() { this.entries = []; } append(...args) { this.entries.push(args); } },
        URL: { createObjectURL: () => `blob:${++url}`, revokeObjectURL: value => revoked.push(value) },
        alert: value => alerts.push(value), authUser: null,
        localStorage: { setItem: (...args) => storage.push(args) }, location: {},
        apiFetch: (url, options) => {
            const pending = deferred(); calls.push({ url, options, ...pending }); return pending.promise;
        }
    });
    for (const script of scripts) vm.runInContext(script, context, { filename: 'static/upload.html' });
    const read = code => JSON.parse(vm.runInContext(`JSON.stringify(${code})`, context));
    const upload = (name = 'run.mp4', type = 'video/mp4') => context.uploadFile({ name, type });
    const finish = (index = calls.length - 1, data = { success: true, filename: 'server.mp4' }) => {
        calls[index].resolve({ json: async () => data });
    };
    return { context, elements, calls, revoked, alerts, storage, read, upload, finish };
}
async function uploaded() {
    const h = harness(); const pending = h.upload(); h.finish(); await pending; return h;
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('两页允许背景/前后有人、每段一个自动主体、不明确停止且不换人；无框选入口', () => {
    for (const page of [html, home]) {
        assert.match(page, /每段视频只分析一个主体/);
        assert.match(page, /允许背景或前后有人/);
        assert.match(page, /自动确定[\s\S]*前景跑者/);
        assert.match(page, /主体不明确[\s\S]*停止分析[\s\S]*不自动换人/);
        assert.doesNotMatch(page, /只支持单人视频|背景也不要出现其他人/);
        assert.match(page, /不验证(?:跑者的)?真实身份/);
        assert.doesNotMatch(page, /框选|target_selection|targetCanvas|clearTarget|pointerdown|<canvas/i);
        assert.match(page, /font-awesome\/4\.7\.0/);
        assert.match(page, /src="\/icons\.js"/);
        assert.match(page, /src="\/common\.js"/);
    }
    assert.doesNotMatch(html.match(/<input[^>]+id="videoFile"[^>]*>/)[0], /\bmultiple\b/);
});
test('初始和上传期间按钮禁用，上传成功仅等待手动开始', async () => {
    const h = harness(); assert.equal(h.elements.startAnalysisButton.disabled, true);
    await h.elements.startAnalysisButton.click(); assert.equal(h.calls.length, 0);
    const pending = h.upload(); assert.equal(h.elements.startAnalysisButton.disabled, true);
    assert.match(h.elements.uploadStatus.textContent, /正在上传/);
    assert.equal(h.calls[0].options.body.entries[0][0], 'video');
    h.finish(); await pending;
    assert.equal(h.elements.startAnalysisButton.disabled, false);
    assert.match(h.elements.uploadStatus.textContent, /点击开始分析/);
    assert.deepEqual(h.calls.map(call => call.url), ['/api/upload']);
});
test('手动开始无任何目标字段，保留姓名格式参数，成功缓存并跳转', async () => {
    const h = await uploaded(); h.elements.userNameInput.value = ' 张三 ';
    h.elements.reportFormatSelect.value = 'docx';
    const pending = h.elements.startAnalysisButton.click();
    assert.equal(h.calls[1].url, '/api/analyze');
    assert.deepEqual(JSON.parse(h.calls[1].options.body), {
        video_file: 'server.mp4', user_name: '张三', coach_name: '系统自动评估', keyframes: 5, report_format: 'docx'
    });
    assert.equal(h.elements.startAnalysisButton.disabled, true);
    assert.equal(h.elements.loadingSpinner.style.display, 'block');
    await h.elements.startAnalysisButton.click(); assert.equal(h.calls.length, 2);
    const data = { status: 'success', report: 'report.docx' };
    h.finish(1, data); await pending;
    assert.deepEqual(h.storage, [['poserun_last_result', JSON.stringify(data)]]);
    assert.equal(h.context.location.href, '/results.html');
    assert.equal(h.elements.loadingSpinner.style.display, 'none');
    assert.equal(h.elements.startAnalysisButton.disabled, false);
});
test('姓名依次使用登录账号或文件名，报告默认PDF', async () => {
    for (const user of [null, { name: '登录姓名' }, { username: 'account' }]) {
        const h = await uploaded(); h.context.authUser = user; h.elements.reportFormatSelect.value = '';
        const pending = h.elements.startAnalysisButton.click();
        const payload = JSON.parse(h.calls[1].options.body);
        assert.equal(payload.user_name, user ? (user.name || user.username) : 'run');
        assert.equal(payload.report_format, 'pdf');
        assert.equal(Object.hasOwn(payload, 'target_selection'), false);
        assert.equal(Object.hasOwn(payload, 'target_selections'), false);
        h.finish(1, { status: 'success' }); await pending;
    }
});
test('主体不明确时展示服务端错误，不缓存不跳转，恢复按钮与进度', async () => {
    const h = await uploaded(); const pending = h.elements.startAnalysisButton.click();
    h.finish(1, { status: 'error', error: '主体不明确，无法自动确认前景跑者' }); await pending;
    assert.match(h.elements.uploadError.textContent, /主体不明确，无法自动确认前景跑者/);
    assert.equal(h.elements.uploadError.hidden, false);
    assert.equal(h.alerts.length, 0);
    assert.equal(h.storage.length, 0); assert.equal(h.context.location.href, undefined);
    assert.equal(h.elements.startAnalysisButton.disabled, false);
    assert.equal(h.elements.loadingSpinner.style.display, 'none');
});
test('分析网络或JSON失败恢复按钮，不伪造结果；unauthorized不重复弹窗', async () => {
    for (const message of ['network', 'unauthorized']) {
        const h = await uploaded(); const pending = h.elements.startAnalysisButton.click();
        h.calls[1].reject(new Error(message)); await pending;
        assert.equal(h.alerts.length, 0);
        assert.equal(h.elements.uploadError.hidden, false);
        assert.equal(h.storage.length, 0); assert.equal(h.elements.startAnalysisButton.disabled, false);
        assert.equal(h.elements.loadingSpinner.style.display, 'none');
    }
    const h = await uploaded(); const pending = h.elements.startAnalysisButton.click();
    h.calls[1].resolve({ json: async () => { throw new Error('bad json'); } }); await pending;
    assert.equal(h.elements.uploadError.hidden, false); assert.equal(h.storage.length, 0);
    assert.equal(h.elements.startAnalysisButton.disabled, false);
});
test('上传失败、缺文件名和网络失败均禁止分析且显示错误', async () => {
    for (const data of [{ success: false, error: 'failed' }, { success: true }, null]) {
        const h = harness(); const pending = h.upload();
        if (data) h.finish(0, data); else h.calls[0].reject(new Error('network'));
        await pending; await h.elements.startAnalysisButton.click();
        assert.equal(h.elements.startAnalysisButton.disabled, true);
        assert.match(h.elements.uploadStatus.textContent, /上传失败/);
        assert.equal(h.elements.uploadError.hidden, false); assert.equal(h.calls.length, 1);
    }
});
test('快速切文件立即清状态、停止预览、释放URL、中止旧上传', async () => {
    const h = await uploaded(); const pending = h.upload('new.mp4');
    assert.equal(h.calls[0].options.signal.aborted, true);
    assert.deepEqual(h.revoked, ['blob:1']);
    assert.equal(h.elements.previewVideo.paused, true);
    assert.equal(h.elements.previewVideo.src, 'blob:2');
    assert.equal(h.read('currentFileName'), null);
    assert.equal(h.read('selectedFile.name'), 'new.mp4');
    assert.equal(h.elements.startAnalysisButton.disabled, true);
    assert.equal(h.elements.loadingSpinner.style.display, 'none');
    h.finish(1); await pending;
});
test('旧上传慢响应不能覆盖新文件成功状态', async () => {
    const h = harness(); const old = h.upload('old.mp4'); const newer = h.upload('new.mp4');
    h.finish(1, { success: true, filename: 'new-server.mp4' }); await newer;
    const status = h.elements.uploadStatus.textContent;
    h.finish(0, { success: true, filename: 'old-server.mp4' }); await old;
    assert.equal(h.read('currentFileName'), 'new-server.mp4');
    assert.equal(h.elements.uploadStatus.textContent, status);
    assert.equal(h.elements.startAnalysisButton.disabled, false);
});
test('旧上传JSON慢响应不能覆盖新文件', async () => {
    const h = harness(); const old = h.upload(); const body = deferred();
    h.calls[0].resolve({ json: () => body.promise }); await tick();
    const newer = h.upload('new.mp4'); h.finish(1, { success: true, filename: 'new.mp4' }); await newer;
    body.resolve({ success: true, filename: 'old.mp4' }); await old;
    assert.equal(h.read('currentFileName'), 'new.mp4');
});
test('旧上传错误不弹窗、不提前启用新上传按钮', async () => {
    const h = harness(); const old = h.upload(); const newer = h.upload('new.mp4');
    h.calls[0].reject(new Error('network')); await old;
    assert.equal(h.alerts.length, 0); assert.equal(h.elements.startAnalysisButton.disabled, true);
    h.finish(1); await newer;
});
test('切换无效格式也彻底清空旧状态与预览', async () => {
    const h = await uploaded(); await h.upload('bad.txt', 'text/plain');
    assert.equal(h.read('currentFileName'), null); assert.equal(h.read('selectedFile'), null);
    assert.equal(h.elements.startAnalysisButton.disabled, true);
    assert.equal(h.elements.videoPreview.style.display, 'none');
    assert.equal(h.elements.previewVideo.src, undefined);
    assert.deepEqual(h.revoked, ['blob:1']); assert.equal(h.calls.length, 1);
});
test('切文件后旧分析成功/失败/网络错误均不覆盖新分析按钮、进度或跳转', async () => {
    for (const outcome of ['success', 'error', 'network']) {
        const h = await uploaded(); const old = h.elements.startAnalysisButton.click();
        const newer = h.upload('new.mp4'); h.finish(2); await newer;
        const nextAnalysis = h.elements.startAnalysisButton.click();
        if (outcome === 'network') h.calls[1].reject(new Error('network'));
        else h.finish(1, { status: outcome, error: 'old failure' });
        await old;
        assert.equal(h.storage.length, 0); assert.equal(h.alerts.length, 0);
        assert.equal(h.context.location.href, undefined);
        assert.equal(h.elements.startAnalysisButton.disabled, true);
        assert.equal(h.elements.loadingSpinner.style.display, 'block');
        h.finish(3, { status: 'success' }); await nextAnalysis;
        assert.equal(h.storage.length, 1);
    }
});
test('旧分析JSON慢响应不缓存不跳转，不启用仍在上传的新文件', async () => {
    const h = await uploaded(); const old = h.elements.startAnalysisButton.click(); const body = deferred();
    h.calls[1].resolve({ json: () => body.promise }); await tick();
    const newer = h.upload('new.mp4'); body.resolve({ status: 'success' }); await old;
    assert.equal(h.storage.length, 0); assert.equal(h.context.location.href, undefined);
    assert.equal(h.elements.startAnalysisButton.disabled, true);
    h.finish(2); await newer;
});
test('离开页面释放URL、清src并使未完成响应失效', async () => {
    const h = harness(); const pending = h.upload();
    await h.context.window.emit('beforeunload');
    assert.deepEqual(h.revoked, ['blob:1']); assert.equal(h.elements.previewVideo.src, undefined);
    assert.equal(h.calls[0].options.signal.aborted, true);
    h.finish(); await pending;
    assert.equal(h.read('currentFileName'), null); assert.equal(h.elements.startAnalysisButton.disabled, true);
    await h.context.window.emit('beforeunload'); assert.deepEqual(h.revoked, ['blob:1']);
});
test('文件输入和拖放执行实际上传路径，每次只取首个文件且不自动分析', async () => {
    for (const type of ['change', 'drop']) {
        const h = harness(); const files = [{ name: 'first.mp4', type: 'video/mp4' }, { name: 'second.mp4', type: 'video/mp4' }];
        if (type === 'change') await h.elements.videoFile.emit(type, { target: { files } });
        else await h.elements.dropZone.emit(type, { dataTransfer: { files } });
        assert.equal(h.calls.length, 1); assert.equal(h.calls[0].options.body.entries[0][1].name, 'first.mp4');
        h.finish(); await tick(); assert.equal(h.elements.startAnalysisButton.disabled, false);
        assert.equal(h.calls.length, 1);
    }
});
test('无 Bootstrap 的真实上传 HTML：初始隐藏、主体失败保留 reason/帧零、重试清错', async () => {
    const h = await uploaded();
    assert.equal(h.context.bootstrap, undefined);
    assert.equal(h.elements.loadingSpinner.hidden, true);
    assert.equal(h.elements.uploadError.hidden, true);
    const pending = h.elements.startAnalysisButton.click();
    assert.equal(h.elements.loadingSpinner.hidden, false);
    h.finish(1, { status: 'error', error: 'primary_subject_lost', reason: 'occluded', frame_index: 0 });
    await pending;
    assert.equal(h.elements.loadingSpinner.hidden, true);
    assert.equal(h.elements.startAnalysisButton.disabled, false);
    assert.equal(h.elements.uploadError.hidden, false);
    assert.match(h.elements.uploadError.textContent, /primary_subject_lost.*reason: occluded.*frame_index: 0/);
    const retry = h.elements.startAnalysisButton.click();
    assert.equal(h.elements.uploadError.hidden, true);
    h.calls[2].reject(new Error('network')); await retry;
    assert.equal(h.elements.loadingSpinner.hidden, true);
    assert.equal(h.elements.uploadError.hidden, false);
    assert.equal(h.storage.length, 0);
});
test('HTTP 失败即使成功形状也不得缓存跳转，错误用纯文本展示', async () => {
    const h = await uploaded(); const pending = h.elements.startAnalysisButton.click();
    h.calls[1].resolve({ ok: false, json: async () => ({ status: 'success', error: '<img onerror=bad>', reason: 'lost', frame_index: 23 }) });
    await pending;
    assert.match(h.elements.uploadError.textContent, /<img onerror=bad>.*frame_index: 23/);
    assert.equal(h.storage.length, 0); assert.equal(h.context.location.href, undefined);
    assert.equal(h.elements.loadingSpinner.hidden, true);
    assert.equal(h.elements.startAnalysisButton.disabled, false);
});
test('切文件中止分析；旧上传失败及旧分析失败不能污染当前内联错误', async () => {
    const h = await uploaded(); const oldAnalysis = h.elements.startAnalysisButton.click();
    const oldUpload = h.upload('middle.mp4');
    assert.equal(h.calls[1].options.signal.aborted, true);
    const latest = h.upload('latest.mp4');
    h.finish(3, { success: false, error: 'current failure', reason: 'current', frame_index: 9 }); await latest;
    const error = h.elements.uploadError.textContent;
    h.finish(2, { success: false, error: 'stale upload' }); await oldUpload;
    h.finish(1, { status: 'error', error: 'stale analysis' }); await oldAnalysis;
    assert.equal(h.elements.uploadError.textContent, error);
    assert.equal(h.elements.uploadError.hidden, false);
    assert.equal(h.elements.startAnalysisButton.disabled, true);
    assert.equal(h.elements.loadingSpinner.hidden, true);
});
test('认证姓名自动填充但不覆盖用户输入', () => {
    const h = harness(); h.context.onAuthReady({ name: '账号姓名' });
    assert.equal(h.elements.userNameInput.value, '账号姓名');
    h.elements.userNameInput.value = '自填姓名'; h.context.onAuthReady({ name: '其他姓名' });
    assert.equal(h.elements.userNameInput.value, '自填姓名');
});
