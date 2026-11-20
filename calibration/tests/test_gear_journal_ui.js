'use strict';
// 跑鞋＋训练日志页：自包含（无 CDN/第三方字体）、证据标签、键盘可达、纯函数、三主题令牌齐备。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const gearHtml = fs.readFileSync(path.resolve(__dirname, '../../running/gear.html'), 'utf8');
const journalHtml = fs.readFileSync(path.resolve(__dirname, '../../running/journal.html'), 'utf8');
const themeCss = fs.readFileSync(path.resolve(__dirname, '../../static/themes/theme-system.css'), 'utf8');

function inlineScript(html) {
    const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
    return scripts.at(-1);
}
function loadPure(html, filename) {
    const mod = { exports: {} };
    const context = vm.createContext({ module: mod });
    vm.runInContext(inlineScript(html), context, { filename });
    return mod.exports;
}

for (const [name, html] of [['gear', gearHtml], ['journal', journalHtml]]) {
    test(`${name} 自包含：无 http(s) 外链、无 @import、无外部样式/脚本`, () => {
        assert.doesNotMatch(html, /https?:\/\//);
        assert.doesNotMatch(html, /@import/);
        assert.doesNotMatch(html, /<link[^>]+href="http/);
        assert.doesNotMatch(html, /<script[^>]+src="http/);
        assert.match(html, /<style>/);
        assert.match(html, /href="\/common\.css"/);
        assert.match(html, /src="\/common\.js"/);
    });

    test(`${name} 澄明训练台风格与证据口径`, () => {
        assert.match(html, /澄明训练台/);
        assert.match(html, /rm-main/);
        assert.match(html, /rm-panel/);
        assert.match(html, /未核验/);
        assert.match(html, /window\.PAGE='(gear|journal)'/);
        // 数值对齐与单位邻接
        assert.match(html, /tabular-nums/);
        // 窄屏可读
        assert.match(html, /@media\(max-width:700px\)/);
        // 空数据引导，不预填样例
        assert.match(html, /暂无/);
        assert.doesNotMatch(html, /<input[^>]*\svalue="/);
        assert.doesNotMatch(html, /<textarea[^>]*>[^<]+<\/textarea>/);
    });

    test(`${name} 键盘可达：原生控件、输入均有 label、按钮有 id、焦点可见、无 div onclick`, () => {
        assert.doesNotMatch(html, /<div[^>]*\sonclick=/);
        for (const m of html.matchAll(/<(input|select|textarea)[^>]*\bid="([\w-]+)"/g)) {
            assert.match(html, new RegExp(`<label[^>]*for="${m[2]}"`), 'label for ' + m[2]);
        }
        for (const m of html.matchAll(/<button[^>]*>/g)) assert.match(m[0], /\bid="/);
        assert.match(html, /:focus-visible/);
        assert.match(html, /aria-live="polite"/);
    });

    test(`${name} 三主题令牌齐备：页面所用 CSS 变量在 clarion/nightlab/fieldnote 均有定义`, () => {
        const used = new Set([...html.matchAll(/var\((--[\w-]+)/g)].map(m => m[1]));
        assert.ok(used.size > 0);
        for (const theme of ['clarion', 'nightlab', 'fieldnote']) {
            const rule = themeCss.split('\n').find(line => line.includes(`data-theme=${theme}]`) && line.includes('{'));
            assert.ok(rule, 'theme rule ' + theme);
            for (const token of used) {
                assert.ok(rule.includes(token + ':'), `${theme} 缺少 ${token}`);
            }
        }
        // 共享 rm- 布局类由 theme-system.css 提供（三主题统一）
        assert.match(themeCss, /\.rm-main\{/);
        assert.match(themeCss, /\.rm-panel\{/);
        assert.match(themeCss, /\.rm-list/);
    });
}

test('gear 证据标签：已导入活动内合计；缺距离标注；空态引导', () => {
    assert.match(gearHtml, /已导入活动内合计 · 未核验/);
    assert.match(gearHtml, /缺距离活动不计入/);
    assert.match(gearHtml, /暂无跑鞋。新增一双/);
    assert.match(gearHtml, /每活动至多关联一双鞋|每个活动至多关联一双鞋/);
});

test('journal 证据标签：跑者自填；私密备注隔离声明；每日一条', () => {
    assert.match(journalHtml, /跑者自填 · 未核验/);
    assert.match(journalHtml, /私密备注仅本人可见/);
    assert.match(journalHtml, /不进入任何列表、分享或导出/);
    assert.match(journalHtml, /每天一条/);
    assert.match(journalHtml, /暂无日志。写下第一条/);
});

test('gear 纯函数：里程格式、缺测标注、阈值提示（不插算、不臆造）', () => {
    const { fmtKm, mileageText, thresholdHint } = loadPure(gearHtml, 'gear-inline.js');
    assert.equal(fmtKm(7), '7.00 km');
    assert.equal(fmtKm(0), '0.00 km');
    assert.equal(fmtKm(null), '—');
    assert.equal(mileageText(7, 0), '7.00 km');
    assert.equal(mileageText(7, 2), '7.00 km（另有 2 条缺距离活动未计入）');
    assert.equal(mileageText(null, 1), '—（另有 1 条缺距离活动未计入）');
    assert.equal(thresholdHint(5, 600), '距提示阈值 600.00 km 还差 595.00 km');
    assert.equal(thresholdHint(600, 600), '已达提示阈值 600.00 km（仅提示，非诊断）');
    assert.equal(thresholdHint(700, 600), '已达提示阈值 600.00 km（仅提示，非诊断）');
    assert.equal(thresholdHint(5, null), '');
    assert.equal(thresholdHint(5, 0), '');
    assert.equal(thresholdHint(null, 600), '');
});

test('journal 纯函数：日期、版本、私密标记', () => {
    const { fmtEntryDate, revisionLabel, privacyLabel } = loadPure(journalHtml, 'journal-inline.js');
    assert.equal(fmtEntryDate('2026-11-18T00:00:00+00:00'), '2026-11-18');
    assert.equal(fmtEntryDate('2026-11-18'), '2026-11-18');
    assert.equal(fmtEntryDate(''), '未填');
    assert.equal(revisionLabel(0), '第 1 版');
    assert.equal(revisionLabel(2), '第 3 版');
    assert.equal(privacyLabel(1), '含私密备注（仅本人可见）');
    assert.equal(privacyLabel(0), '无私密备注');
});