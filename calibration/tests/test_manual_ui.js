'use strict';
// 手工补录页：自包含（无 CDN/第三方字体）、证据标签、键盘可达、配速不臆造、时区换算。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.resolve(__dirname, '../../running/manual-entry.html'), 'utf8');

test('自包含：无 http(s) 外链、无 @import、无外部样式/脚本', () => {
    assert.doesNotMatch(html, /https?:\/\//);
    assert.doesNotMatch(html, /@import/);
    assert.doesNotMatch(html, /<link[^>]+href="http/);
    assert.doesNotMatch(html, /<script[^>]+src="http/);
    assert.match(html, /<style>/);
});

test('证据标签与未核验口径', () => {
    assert.match(html, /手工记录 · 未核验/);
    assert.match(html, /跑者自填/);
    assert.match(html, /缺测不臆造配速|不臆造配速/);
});

test('键盘可达：交互元素均为原生控件，输入均有 label，焦点可见，无 div onclick', () => {
    assert.doesNotMatch(html, /<div[^>]*\sonclick=/);
    for (const m of html.matchAll(/<(input|select|textarea)[^>]*\bid="([\w-]+)"/g)) {
        assert.match(html, new RegExp(`<label[^>]*for="${m[2]}"`), 'label for ' + m[2]);
    }
    for (const m of html.matchAll(/<button[^>]*>/g)) assert.match(m[0], /\bid="/);
    assert.match(html, /:focus-visible/);
    assert.match(html, /<form[^>]*id="manualForm"/);
    assert.match(html, /type="datetime-local"/);
    assert.match(html, /Asia\/Shanghai/);
});

test('纯函数：配速仅在距离与时长齐备时给出；时区换算正确', () => {
    const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
    const script = scripts.at(-1);
    const mod = { exports: {} };
    const context = vm.createContext({ module: mod });
    vm.runInContext(script, context, { filename: 'manual-entry-inline.js' });
    const { formatPace, toZoneISO } = mod.exports;
    assert.equal(formatPace(5000, 1800), "6'00\" /km");
    assert.equal(formatPace(5200, 1860), "5'58\" /km");
    assert.equal(formatPace(null, 1800), '');
    assert.equal(formatPace(5000, null), '');
    assert.equal(formatPace(0, 1800), '');
    // 2026-11-16 07:30 Asia/Shanghai (UTC+8) == 2026-11-15 23:30 UTC
    assert.equal(toZoneISO('2026-11-16T07:30', 'Asia/Shanghai'), '2026-11-15T23:30:00.000Z');
    // 2026-01-16 07:30 America/New_York (EST, UTC-5) == 2026-01-16 12:30 UTC
    assert.equal(toZoneISO('2026-01-16T07:30', 'America/New_York'), '2026-01-16T12:30:00.000Z');
    // 2026-07-16 07:30 America/New_York (EDT, UTC-4) == 2026-07-16 11:30 UTC
    assert.equal(toZoneISO('2026-07-16T07:30', 'America/New_York'), '2026-07-16T11:30:00.000Z');
    assert.equal(toZoneISO('', 'Asia/Shanghai'), '');
});
