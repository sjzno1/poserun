'use strict';
// P2 切片5 轨迹地图页：自包含（无 CDN/外链）、纯函数渲染口径、三主题令牌、键盘可达。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.resolve(__dirname, '../../running/activity-track.html'), 'utf8');
const themeCss = fs.readFileSync(path.resolve(__dirname, '../../static/themes/theme-system.css'), 'utf8');

function inlineScript(src) {
    const scripts = [...src.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
    return scripts.at(-1);
}
function loadPure() {
    const mod = { exports: {} };
    const context = vm.createContext({ module: mod });
    vm.runInContext(inlineScript(html), context, { filename: 'activity-track.html' });
    return mod.exports;
}

test('轨迹页自包含：无 http(s) 外链、无 @import、无外部样式/脚本', () => {
    assert.doesNotMatch(html, /https?:\/\//);
    assert.doesNotMatch(html, /@import/);
    assert.doesNotMatch(html, /<link[^>]+href="http/);
    assert.doesNotMatch(html, /<script[^>]+src="http/);
    assert.match(html, /<style>/);
    assert.match(html, /href="\/common\.css"/);
    assert.match(html, /src="\/common\.js"/);
});

test('轨迹页风格与证据口径', () => {
    assert.match(html, /rm-main/);
    assert.match(html, /rm-panel/);
    assert.match(html, /未核验/);
    assert.match(html, /window\.PAGE='activity-track'/);
    assert.match(html, /tabular-nums/);
    assert.match(html, /@media\(max-width:700px\)/);
    assert.match(html, /无轨迹数据/);
});

test('键盘可达：无 div onclick、焦点可见、状态区 aria-live、SVG 有 role/aria-label', () => {
    assert.doesNotMatch(html, /<div[^>]*\sonclick=/);
    assert.match(html, /:focus-visible/);
    assert.match(html, /aria-live="polite"/);
    const script = inlineScript(html);
    assert.match(script, /setAttribute\('role',\s*'img'\)/);
    assert.match(script, /setAttribute\('aria-label'/);
});

test('三主题令牌齐备：页面所用 CSS 变量在 clarion/nightlab/fieldnote 均有定义', () => {
    const used = new Set([...html.matchAll(/var\((--[\w-]+)/g)].map(m => m[1]));
    assert.ok(used.size > 0);
    for (const theme of ['clarion', 'nightlab', 'fieldnote']) {
        const rule = themeCss.split('\n').find(line => line.includes(`data-theme=${theme}]`) && line.includes('{'));
        assert.ok(rule, 'theme rule ' + theme);
        for (const token of used) {
            assert.ok(rule.includes(token + ':'), `${theme} 缺少 ${token}`);
        }
    }
});

test('纯函数可导出：无轨迹文案常量', () => {
    const m = loadPure();
    assert.equal(m.TM_NO_TRACK, '无轨迹数据');
    assert.match(m.TM_NO_TRACK_HINT, /手工/);
});

test('tmProject：纬度修正纵横比、y 翻转、首尾在画布内', () => {
    const m = loadPure();
    const lat0 = 31.23;
    const pts = [];
    for (let i = 0; i <= 10; i++) pts.push({ lat: lat0 + i * 0.001, lon: 121.47 + i * 0.001, ele: null, time: null });
    const proj = m.tmProject(pts, 800, 500, 30);
    assert.equal(proj.pts.length, 11);
    // 纬度跨度 0.01°≈1113m；经度跨度 0.01°≈1113*cos(31.23°)≈951m → 像素宽/高≈cos(lat0)
    const xs = proj.pts.map(p => p.x), ys = proj.pts.map(p => p.y);
    const w = Math.max(...xs) - Math.min(...xs), h = Math.max(...ys) - Math.min(...ys);
    assert.ok(Math.abs(w / h - Math.cos(lat0 * Math.PI / 180)) < 0.02, `aspect ${w / h}`);
    assert.ok(proj.pts[0].y > proj.pts[10].y, '北=上（y 翻转）');
    for (const p of proj.pts) {
        assert.ok(p.x >= 30 && p.x <= 770 && p.y >= 30 && p.y <= 470);
    }
});

test('tmPathD：命令数==点数，M 开头', () => {
    const m = loadPure();
    const pts = [{ x: 1, y: 2 }, { x: 3, y: 4 }, { x: 5, y: 6 }];
    const d = m.tmPathD(pts);
    assert.ok(d.startsWith('M'));
    assert.equal((d.match(/[ML]/g) || []).length, 3);
    assert.equal(m.tmPathD([]), '');
});

test('tmScaleBar：整刻度、不超过跨度三分之一', () => {
    const m = loadPure();
    const s10k = m.tmScaleBar(10000);
    assert.equal(s10k.meters, 2000);
    assert.equal(s10k.label, '2 km');
    const s800 = m.tmScaleBar(800);
    assert.equal(s800.meters, 200);
    assert.equal(s800.label, '200 m');
    for (const span of [50, 300, 1500, 42000, 100000]) {
        const s = m.tmScaleBar(span);
        assert.ok(s.meters <= Math.max(span / 3, 100));
        assert.ok(s.meters > 0);
    }
});

test('tmElevation：累计距离单调、首 0、全缺海拔 hasEle=false', () => {
    const m = loadPure();
    const pts = [
        { lat: 31.0, lon: 121.0, ele: 10, time: null },
        { lat: 31.001, lon: 121.0, ele: 20, time: null },
        { lat: 31.002, lon: 121.0, ele: null, time: null },
        { lat: 31.003, lon: 121.0, ele: 15, time: null },
    ];
    const e = m.tmElevation(pts);
    assert.equal(e.xs[0], 0);
    for (let i = 1; i < e.xs.length; i++) assert.ok(e.xs[i] > e.xs[i - 1]);
    assert.ok(e.hasEle);
    assert.equal(e.min, 10);
    assert.equal(e.max, 20);
    const none = m.tmElevation(pts.map(p => ({ ...p, ele: null })));
    assert.equal(none.hasEle, false);
    assert.equal(m.tmElevation([]).hasEle, false);
});

test('tmElevPathD：有海拔生成折线＋面积；无海拔为空', () => {
    const m = loadPure();
    const e = m.tmElevation([
        { lat: 31.0, lon: 121.0, ele: 10, time: null },
        { lat: 31.001, lon: 121.0, ele: 20, time: null },
        { lat: 31.002, lon: 121.0, ele: 15, time: null },
    ]);
    const d = m.tmElevPathD(e.xs, e.eles, 800, 220, 30);
    assert.ok(d.line.startsWith('M'));
    assert.ok(d.area.endsWith('Z'));
    const empty = m.tmElevPathD([0, 1], [null, null], 800, 220, 30);
    assert.equal(empty.line, '');
    assert.equal(empty.area, '');
});

test('tmSummary：缺失为 —、距离/配速口径', () => {
    const m = loadPure();
    const s = m.tmSummary({ start_time: '2026-11-16T00:00:00+00:00', distance_m: 5000, duration_s: 1800, source: 'gpx' });
    assert.equal(s.distance, '5.00 km');
    assert.equal(s.pace, '6:00 /km');
    assert.match(s.date, /2026-11-16/);
    const empty = m.tmSummary({ start_time: '2026-11-16T00:00:00+00:00', distance_m: null, duration_s: null, source: 'manual' });
    assert.equal(empty.distance, '—');
    assert.equal(empty.pace, '—');
});

test('tmAriaLabel：含点数与距离，供读屏', () => {
    const m = loadPure();
    const label = m.tmAriaLabel(123, 5.67);
    assert.match(label, /123/);
    assert.match(label, /5\.67/);
});