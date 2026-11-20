// P2 切片7 训练负荷页 UI 契约（node --test，TDD 红）。AGPL-3.0
// 锁定：离线零外链、三主题 token、窄屏/a11y、空态、纯函数（缩放/路径/卡片/假设）、
// 导航入口旗标探针（common.js）、dashboard loadEntry（index.html＋running.js）。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

const LOAD_PATH = 'running/load.html';
const exists = fs.existsSync(path.join(ROOT, LOAD_PATH));
const html = exists ? read(LOAD_PATH) : '';
const common = read('static/common.js');
const running = read('static/running.js');
const index = read('static/index.html');
const themeCss = read('static/themes/theme-system.css');

// 提取页面内联脚本（最后一个 <script> 无 src 块），在 vm 沙箱中加载纯函数
function loadPageModule() {
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  const code = scripts[scripts.length - 1] || '';
  const els = [];
  const makeEl = (tag) => {
    const el = {
      tagName: tag, attrs: {}, children: [], style: {}, dataset: {},
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] ?? null; },
      append(...nodes) { this.children.push(...nodes); },
      appendChild(n) { this.children.push(n); return n; },
      replaceChildren(...nodes) { this.children = nodes; },
      addEventListener() {}, remove() {},
      set textContent(v) { this._text = String(v); }, get textContent() { return this._text || ''; },
      classList: { add() {}, remove() {} },
    };
    els.push(el);
    return el;
  };
  const sandbox = {
    module: { exports: {} },
    console,
    window: { addEventListener() {}, PAGE: 'training-load', location: { reload() {} } },
    document: {
      getElementById: () => null,
      createElement: makeEl,
      createElementNS: (ns, tag) => makeEl(tag),
      createTextNode: (t) => ({ text: t }),
      documentElement: { setAttribute() {}, getAttribute: () => 'clarion' },
      addEventListener() {},
    },
    localStorage: { getItem: () => 'synthetic-token', setItem() {}, removeItem() {} },
    location: { href: '', reload() {} },
    apiFetch: async () => { throw new Error('不应在加载期发请求'); },
    setTimeout, clearTimeout,
  };
  sandbox.window.localStorage = sandbox.localStorage;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'load.html:inline' });
  return { exports: sandbox.module.exports, els, onAuthReady: sandbox.window.onAuthReady };
}

test('页面存在且离线：零外链、中文 lang、viewport', () => {
  assert.ok(exists, 'running/load.html 必须存在');
  assert.match(html, /<html lang="zh-CN"/);
  assert.match(html, /name="viewport"/);
  assert.match(html, /<title>[^<]*训练负荷/);
  // SVG 命名空间是 XML 标识符，非网络资源；其余 http(s) 一律禁止
  const stripped = html.replace(/http:\/\/www\.w3\.org\/2000\/svg/g, '');
  assert.doesNotMatch(stripped, /https?:\/\//, '页面不得含任何外链（含 CDN/字体/图床）');
  assert.doesNotMatch(html, /<link[^>]+href="(?!\/)/, '样式必须本地');
});

test('控件与区域：days 选择器＋label、状态区、summary/图表/假设/空态', () => {
  assert.match(html, /<label[^>]*for="loadDays"/);
  assert.match(html, /<select[^>]*id="loadDays"/);
  for (const v of ['7', '30', '90', '180', '365']) {
    assert.match(html, new RegExp(`<option value="${v}"`), '窗口选项 ' + v);
  }
  assert.match(html, /<option value="90" selected/, '缺省 90 天');
  assert.match(html, /id="loadStatus"[^>]*role="status"|role="status"[^>]*id="loadStatus"/);
  assert.match(html, /aria-live="polite"/);
  for (const id of ['loadSummary', 'loadInterpretation', 'loadChart', 'loadAssumptions', 'loadEmpty']) {
    assert.match(html, new RegExp(`id="${id}"`), id);
  }
  assert.match(html, /id="loadEmpty"[^>]*hidden|hidden[^>]*id="loadEmpty"/, '空态初始隐藏');
  assert.match(html, /aria-labelledby=/, '面板须有可访问名称');
});

test('三主题 token：页面样式只引用三主题都定义的变量', () => {
  const style = (html.match(/<style>([\s\S]*?)<\/style>/) || [])[1] || '';
  const used = [...new Set([...style.matchAll(/var\((--[a-z0-9-]+)/g)].map(m => m[1]))];
  assert.ok(used.length >= 4, '应使用主题 token 而非硬编码颜色');
  for (const theme of ['clarion', 'nightlab', 'fieldnote']) {
    const block = themeCss.split(`html[data-theme=${theme}]`)[1].split('}')[0];
    for (const token of used) {
      assert.ok(block.includes(token + ':'), `${theme} 缺少 ${token}`);
    }
  }
  assert.doesNotMatch(style, /#[0-9a-fA-F]{3,8}\b/, '页面样式不得硬编码颜色');
});

test('a11y 与窄屏：无 div onclick、无正 tabindex、focus-visible、窄屏媒体查询', () => {
  assert.doesNotMatch(html, /<(div|span|p|section)[^>]*onclick=/i);
  assert.doesNotMatch(html, /tabindex="[1-9]/);
  const style = (html.match(/<style>([\s\S]*?)<\/style>/) || [])[1] || '';
  assert.match(style, /:focus-visible/);
  assert.match(style, /@media\s*\(max-width/);
  assert.match(style, /font-variant-numeric:\s*tabular-nums|tabular-nums/);
});

test('纯函数 ldFmt/ldScale/ldPath：确定性缩放与路径', () => {
  const { exports: M } = loadPageModule();
  assert.equal(typeof M.ldFmt, 'function');
  assert.equal(M.ldFmt(null), '—');
  assert.equal(M.ldFmt(undefined, 2), '—');
  assert.equal(M.ldFmt(0, 1), '0.0');
  assert.equal(M.ldFmt(12.346, 2), '12.35');
  const series = [
    { date: '2026-11-01', daily_load: 100, ctl: 2.38, atl: 14.29, tsb: 0 },
    { date: '2026-11-02', daily_load: 0, ctl: 2.32, atl: 12.24, tsb: -11.9 },
    { date: '2026-11-03', daily_load: 50, ctl: 3.46, atl: 17.64, tsb: -9.92 },
  ];
  const sc = M.ldScale(series, 760, 300, 40);
  assert.equal(sc.xs.length, 3);
  assert.ok(sc.xs[0] < sc.xs[1] && sc.xs[1] < sc.xs[2], 'x 单调递增');
  assert.ok(sc.yMin <= -11.9 && sc.yMax >= 17.64, 'y 域覆盖三线极值');
  assert.ok(sc.yMin <= 0 && sc.yMax >= 0, 'y 域包含零基线（TSB 可负）');
  for (const key of ['ctl', 'atl', 'tsb']) {
    assert.equal(sc['y_' + key].length, 3);
    for (const y of sc['y_' + key]) assert.ok(y >= 0 && y <= 300, 'y 在画布内');
  }
  const d = M.ldPath(sc.xs, sc.y_ctl);
  assert.equal((d.match(/M/g) || []).length, 1);
  assert.equal((d.match(/L/g) || []).length, 2, 'n 点 → n-1 段 L');
  assert.equal(M.ldPath([], []), '');
  // 单点不崩
  const one = M.ldScale([series[0]], 760, 300, 40);
  assert.equal(one.xs.length, 1);
  assert.match(M.ldPath(one.xs, one.y_ctl), /^M/);
});

test('SVG 装配：三条线＋role=img＋aria-label＋零基线', () => {
  const { exports: M, els } = loadPageModule();
  const series = [
    { date: '2026-11-01', daily_load: 100, ctl: 2.38, atl: 14.29, tsb: 0 },
    { date: '2026-11-02', daily_load: 0, ctl: 2.32, atl: 12.24, tsb: -11.9 },
  ];
  const svg = M.ldRenderChart(series, { current_ctl: 2.32, current_atl: 12.24, current_tsb: -11.9 });
  assert.equal(svg.tagName, 'svg');
  assert.equal(svg.attrs.role, 'img');
  assert.ok((svg.attrs['aria-label'] || '').length > 10, 'SVG 须有描述性 aria-label');
  assert.ok(svg.attrs.viewBox, 'viewBox 响应式');
  const paths = svg.children.filter(c => c.tagName === 'path' && (c.attrs.class || '').includes('ld-line'));
  assert.equal(paths.length, 3, 'CTL/ATL/TSB 三条线');
  for (const p of paths) {
    assert.ok((p.attrs.d.match(/[ML]/g) || []).length >= 2, 'path 点数>0');
    assert.match(p.attrs.stroke, /var\(--(accent|green|muted)\)/, '线色用主题 token');
  }
  assert.ok(svg.children.some(c => c.tagName === 'line' && (c.attrs.class || '').includes('ld-zero')), '零基线');
  assert.ok(els.length > 0);
});

test('summary 卡片与假设清单：缺测显示—、假设完整披露', () => {
  const { exports: M } = loadPageModule();
  const cards = M.ldSummaryCards({
    current_ctl: 6.0467, current_atl: 19.5114, current_tsb: -16.5691,
    acute_chronic_ratio: 3.2268, load_7d: 140, interpretation: 'x',
  });
  assert.ok(Array.isArray(cards) && cards.length >= 5);
  const labels = cards.map(c => c.label).join('|');
  for (const want of ['CTL', 'ATL', 'TSB', '急性', '7']) assert.ok(labels.includes(want), want);
  assert.equal(cards.find(c => c.label.includes('CTL')).value, '6.05');
  assert.equal(cards.find(c => c.label.includes('TSB')).value, '-16.57');
  const empty = M.ldSummaryCards({ current_ctl: 0, current_atl: 0, current_tsb: 0, acute_chronic_ratio: null, load_7d: 0, interpretation: '' });
  assert.equal(empty.find(c => c.label.includes('急性')).value, '—');
  const list = M.ldAssumptionsList({
    hr_max: 190, hr_rest: 60, sex: 'male', hr_params_source: 'default',
    trimp: 'Banister TRIMP：男 t×ΔHR×0.64×e^(1.92ΔHR)，女 t×ΔHR×0.86×e^(1.67ΔHR)',
    fallback: '无平均心率时负荷=时长(分钟)×0.5',
    timezone: 'Asia/Shanghai（UTC+8）',
    ewma: { ctl_days: 42, atl_days: 7, seed: 0 },
    tsb: 'TSB_n = CTL_{n−1} − ATL_{n−1}',
    disclaimer: '训练负荷参考，非医疗建议',
  });
  const text = list.join('\n');
  for (const want of ['190', '60', 'male', 'TRIMP', '0.5', '42', '7', 'TSB', 'Asia/Shanghai', '非医疗']) {
    assert.ok(text.includes(want), '假设须披露 ' + want);
  }
});

test('页面接线：apiFetch load、days 联动、no-store、会话守卫、空态文案', () => {
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  const code = scripts[scripts.length - 1] || '';
  assert.match(code, /apiFetch\(['"`]\/api\/running\/load/);
  assert.match(code, /cache:\s*['"]no-store['"]/);
  assert.match(code, /loadDays/);
  assert.match(code, /window\.onAuthReady\s*=/);
  assert.match(code, /poserun_token/, '会话切换守卫');
  assert.match(code, /暂无活动记录/, '空态文案');
  assert.match(code, /module\.exports/, 'node 可测缝');
  const { onAuthReady } = loadPageModule();
  assert.equal(typeof onAuthReady, 'function');
});

test('导航入口：common.js 旗标探针＋默认隐藏＋页面标题', () => {
  assert.match(common, /\['training-load',\s*'\/training-load',\s*'训练负荷'/);
  assert.match(common, /navLoadItem/);
  assert.match(common, /apiFetch\('\/api\/running\/load\?days=7'/, '用负荷 API 自身探针判旗标');
  assert.match(common, /['"]?training-load['"]?:\s*'训练负荷'/);
  // 探针在认证成功后触发（applyAuthedUI 内）
  const apply = common.split('function applyAuthedUI')[1].split('\nfunction ')[0];
  assert.match(apply, /navLoadItem/);
});

test('dashboard 入口：index.html loadEntry 默认隐藏＋running.js 旗标接线', () => {
  assert.match(index, /id="loadEntry"[^>]*hidden|hidden[^>]*id="loadEntry"/);
  assert.match(index, /href="\/training-load"/);
  assert.match(running, /loadEntry[^;]*hidden\s*=\s*!d\.load_enabled/);
});