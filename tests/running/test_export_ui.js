'use strict';
// P2 切片6 数据导出入口 UI 契约（node --test，无外部依赖）。AGPL-3.0
// journal 页头导出面板：三主题令牌/窄屏/键盘/a11y/blob 下载纯函数。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const root = path.join(__dirname, '..', '..');
const journalHtml = fs.readFileSync(path.join(root, 'running', 'journal.html'), 'utf8');
const themeCss = fs.readFileSync(path.join(root, 'static', 'themes', 'theme-system.css'), 'utf8');

function inlineScript(src) {
    const scripts = [...src.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
    return scripts.at(-1);
}
function loadPure() {
    const mod = { exports: {} };
    const context = vm.createContext({ module: mod });
    vm.runInContext(inlineScript(journalHtml), context, { filename: 'journal.html' });
    return mod.exports;
}

test('export panel markup: hidden panel, year select with label, real buttons, live status', () => {
    assert.match(journalHtml, /id="exportPanel"[^>]*hidden/);
    assert.match(journalHtml, /<label[^>]*for="exportYear"/);
    assert.match(journalHtml, /<select[^>]*id="exportYear"/);
    assert.match(journalHtml, /<button[^>]*type="button"[^>]*id="exportCsvBtn"/);
    assert.match(journalHtml, /<button[^>]*type="button"[^>]*id="exportJsonBtn"/);
    const statusTag = journalHtml.match(/<p[^>]*id="exportStatus"[^>]*>/)[0];
    assert.match(statusTag, /role="status"/);
    assert.match(statusTag, /aria-live="polite"/);
    // 按钮有可读文案（中文）
    assert.match(journalHtml, /id="exportCsvBtn"[^>]*>[^<]*CSV/);
    assert.match(journalHtml, /id="exportJsonBtn"[^>]*>[^<]*JSON/);
    // 面板位于页头区（h1 之后、journalForm 之前）
    assert.ok(journalHtml.indexOf('id="exportPanel"') > journalHtml.indexOf('<h1>'));
    assert.ok(journalHtml.indexOf('id="exportPanel"') < journalHtml.indexOf('id="journalForm"'));
});

test('journal page stays offline: no external resources', () => {
    assert.doesNotMatch(journalHtml, /https?:\/\//);
    assert.doesNotMatch(journalHtml, /@import/);
    assert.match(journalHtml, /href="\/common\.css"/);
    assert.match(journalHtml, /src="\/common\.js"/);
});

test('export styles use only tokens defined in all three themes', () => {
    const used = new Set([...journalHtml.matchAll(/var\((--[\w-]+)/g)].map(m => m[1]));
    assert.ok(used.size > 0);
    for (const theme of ['clarion', 'nightlab', 'fieldnote']) {
        const rule = themeCss.split('\n').find(line => line.includes(`data-theme=${theme}]`) && line.includes('{'));
        assert.ok(rule, 'theme rule ' + theme);
        for (const token of used) {
            assert.ok(rule.includes(token + ':'), `${theme} 缺少 ${token}`);
        }
    }
});

test('export a11y/keyboard: no div buttons, no positive tabindex, focus-visible, narrow layout', () => {
    assert.doesNotMatch(journalHtml, /<div[^>]*\sonclick=/i);
    assert.doesNotMatch(journalHtml, /tabindex\s*=\s*["']?[1-9]/);
    assert.match(journalHtml, /:focus-visible/);
    assert.match(journalHtml, /@media\(max-width:700px\)/);
    assert.doesNotMatch(journalHtml, /(?<!max-)width:\s*(?:[5-9]\d\d|\d{4,})px/);
});

test('export wiring: apiFetch + blob download + revoke + flag-gated visibility', () => {
    const script = inlineScript(journalHtml);
    assert.match(script, /apiFetch\('\/api\/running\/export/);
    assert.match(script, /createObjectURL\(/);
    assert.match(script, /revokeObjectURL\(/);
    assert.match(script, /\.download\s*=/);
    assert.match(script, /export_enabled/); // dashboard 旗标驱动面板显隐
    assert.match(script, /exportPanel/);
});

test('exFilename: year/all × csv/json', () => {
    const f = loadPure();
    assert.equal(f.exFilename('csv', ''), 'poserun-activities-all.csv');
    assert.equal(f.exFilename('csv', null), 'poserun-activities-all.csv');
    assert.equal(f.exFilename('json', '2026'), 'poserun-activities-2026.json');
    assert.equal(f.exFilename('csv', 2025), 'poserun-activities-2025.csv');
    assert.equal(f.exFilename('json', ''), 'poserun-activities-all.json');
});

test('exYearOptions: 全部 first, then currentYear..2000 descending, unique', () => {
    const f = loadPure();
    const opts = f.exYearOptions(2026);
    assert.equal(opts[0].value, '');
    assert.equal(opts[0].label, '全部');
    assert.equal(opts[1].value, '2026');
    assert.equal(opts[opts.length - 1].value, '2000');
    assert.equal(opts.length, 1 + (2026 - 2000 + 1));
    const values = opts.map(o => o.value);
    assert.equal(new Set(values).size, values.length);
    for (let i = 2; i < opts.length; i++) {
        assert.equal(Number(opts[i].value), Number(opts[i - 1].value) - 1);
    }
});

test('exParseFilename: quoted/unquoted Content-Disposition, fallback on missing', () => {
    const f = loadPure();
    assert.equal(f.exParseFilename('attachment; filename=poserun-activities-2026.csv', 'fb.csv'),
        'poserun-activities-2026.csv');
    assert.equal(f.exParseFilename('attachment; filename="poserun-activities-all.json"', 'fb.json'),
        'poserun-activities-all.json');
    assert.equal(f.exParseFilename('attachment', 'fb.csv'), 'fb.csv');
    assert.equal(f.exParseFilename(null, 'fb.csv'), 'fb.csv');
    assert.equal(f.exParseFilename('', 'fb.json'), 'fb.json');
});

test('exDownloadBlob: anchor download attr, blob href, click, revoke (DOM double)', () => {
    const f = loadPure();
    const calls = { clicks: 0, revoked: [], appended: 0, removed: 0 };
    const anchor = {
        style: {},
        click() { calls.clicks++; },
    };
    const documentDouble = {
        createElement(tag) { assert.equal(tag, 'a'); return anchor; },
        body: {
            appendChild() { calls.appended++; },
            removeChild() { calls.removed++; },
        },
    };
    const urlDouble = {
        createObjectURL(blob) { assert.equal(blob, 'BLOB'); return 'blob:fake'; },
        revokeObjectURL(u) { calls.revoked.push(u); },
    };
    f.exDownloadBlob('BLOB', 'poserun-activities-2026.csv', documentDouble, urlDouble);
    assert.equal(anchor.download, 'poserun-activities-2026.csv');
    assert.equal(anchor.href, 'blob:fake');
    assert.equal(calls.clicks, 1);
    assert.deepEqual(calls.revoked, ['blob:fake']);
    assert.equal(calls.appended, 1);
    assert.equal(calls.removed, 1);
});

test('journal pure functions unchanged (regression guard)', () => {
    const f = loadPure();
    assert.equal(f.fmtEntryDate('2026-05-01T02:00:00+00:00'), '2026-05-01');
    assert.equal(f.fmtEntryDate(''), '未填');
    assert.equal(f.revisionLabel(0), '第 1 版');
    assert.equal(f.privacyLabel(true), '含私密备注（仅本人可见）');
    assert.equal(f.privacyLabel(false), '无私密备注');
});