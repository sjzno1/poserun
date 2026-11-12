'use strict';
// Execute real buildShell against a small HTML tree/event double, without Bootstrap.
// This verifies DOM state/interactions, not pixel layout or browser CSS rendering.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../../static');
const source = fs.readFileSync(path.join(root, 'common.js'), 'utf8');
function shell(bootstrap) {
    const ids = new Map();
    let focused;
    class Element {
        constructor(tag = 'div', attrs = '') {
            this.tagName = tag; this.children = []; this.listeners = {}; this.attrs = {};
            this.style = {}; this.hidden = /\bhidden\b/.test(attrs);
            for (const a of attrs.matchAll(/([\w-]+)="([^"]*)"/g)) this.attrs[a[1]] = a[2];
            const classes = new Set((this.attrs.class || '').split(/\s+/));
            this.classList = { contains: c => classes.has(c), toggle(c, on) {
                if (on === undefined) on = !classes.has(c);
                if (on) classes.add(c); else classes.delete(c);
            } };
            if (this.attrs.id) ids.set(this.attrs.id, this);
        }
        setAttribute(k, v) { this.attrs[k] = v; }
        removeAttribute(k) { delete this.attrs[k]; }
        getAttribute(k) { return this.attrs[k]; }
        addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); }
        emit(k, e = {}) { for (const fn of this.listeners[k] || []) fn({ target: this, preventDefault() {}, ...e }); }
        contains(el) { return el === this || this.children.some(c => c.contains(el)); }
        closest(selector) {
            if (selector.split(',').includes(this.tagName)) return this;
            return this.parent && this.parent.closest(selector);
        }
        focus() { focused = this; }
        querySelectorAll(selector) {
            const all = this.children.flatMap(c => [c, ...c.querySelectorAll('*')]);
            return all.filter(c => selector === '*' || (selector === 'a' ? c.tagName === 'a' : selector.slice(1).split('.').every(n => c.classList.contains(n))));
        }
        insertAdjacentHTML(where, html) {
            const stack = [this];
            for (const m of html.matchAll(/<(\/)?([\w-]+)([^>]*)>/g)) {
                if (m[1]) { if (stack.length > 1) stack.pop(); continue; }
                const el = new Element(m[2], m[3]);
                el.parent = stack.at(-1); el.parent.children.push(el);
                if (!['input','hr','br','img','meta','link'].includes(m[2])) stack.push(el);
            }
        }
    }
    const body = new Element('body');
    const document = new Element('document');
    Object.assign(document, { body, readyState: 'loading', getElementById: id => ids.get(id) || null });
    const context = vm.createContext({ window: new Element(), document, bootstrap,
        localStorage: { getItem() { return null; } }, console });
    vm.runInContext(source, context, { filename: 'static/common.js' });
    context.buildShell();
    return { ids, body, document, focused: () => focused };
}
for (const present of [false, true]) {
    test(`账户菜单初始隐藏、显式开关、外点、Escape、动作后关闭（Bootstrap ${present ? '存在' : '缺失'}）`, () => {
        const h = shell(present ? {} : undefined);
        const button = h.ids.get('navUserLabel'), menu = h.ids.get('accountMenu');
        assert.equal(menu.hidden, true);
        assert.equal(button.getAttribute('aria-expanded'), 'false');
        assert.equal(button.getAttribute('data-bs-toggle'), undefined);
        button.emit('click'); assert.equal(menu.hidden, false);
        h.document.emit('click', { target: button.children[0] }); assert.equal(menu.hidden, false);
        button.emit('click'); assert.equal(menu.hidden, true);
        button.emit('click'); h.document.emit('click', { target: h.body }); assert.equal(menu.hidden, true);
        button.emit('click'); h.document.emit('keydown', { key: 'Escape' });
        assert.equal(menu.hidden, true); assert.equal(h.focused(), button);
        button.emit('click'); menu.emit('click', { target: h.ids.get('navLogoutBtn') });
        assert.equal(menu.hidden, true); assert.equal(button.getAttribute('aria-expanded'), 'false');
    });
}
test('缺 Bootstrap JS 仍能切换登录注册和移动侧栏', () => {
    const h = shell();
    h.ids.get('tabRegister').emit('click');
    assert.equal(h.ids.get('paneRegister').classList.contains('active'), true);
    assert.equal(h.ids.get('paneLogin').classList.contains('active'), false);
    h.ids.get('tabLogin').emit('click');
    assert.equal(h.ids.get('paneLogin').classList.contains('active'), true);
    h.ids.get('sidebarToggle').emit('click');
    assert.equal(h.ids.get('appSidebar').classList.contains('open'), true);
    h.document.emit('keydown', { key: 'Escape' });
    assert.equal(h.ids.get('appSidebar').classList.contains('open'), false);
});
test('所有真实页面使用同源 Bootstrap 5.3.0，保留 FA/SVG；本地库字节校验及许可', () => {
    for (const file of fs.readdirSync(root).filter(f => f.endsWith('.html'))) {
        const html = fs.readFileSync(path.join(root, file), 'utf8');
        if (!html.includes('common.js')) continue;
        assert.match(html, /href="\/vendor\/bootstrap-5\.3\.0\/bootstrap.min.css"/);
        assert.match(html, /src="\/vendor\/bootstrap-5\.3\.0\/bootstrap.bundle.min.js"/);
        assert.doesNotMatch(html, /https:\/\/[^"']*bootstrap@/);
        assert.match(html, /font-awesome\/4\.7\.0/); assert.match(html, /src="\/icons.js"/);
    }
    for (const [file, sha] of [
        ['bootstrap.min.css', '7f1d37f0d90b6385354c2ac10e2bb91563c46bd7a266ed351222ebcac8496c2a'],
        ['bootstrap.bundle.min.js', 'aa53d582f97eb594c2a5cc5824574707f9ba9837bce3046bfa5f3556860f4e04']
    ]) {
        const bytes = fs.readFileSync(path.join(root, 'vendor/bootstrap-5.3.0', file));
        assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), sha);
        assert.match(bytes.toString().slice(0, 400), /Bootstrap\s+v5\.3\.0/);
        assert.match(bytes.toString().slice(0, 400), /Licensed under MIT/);
    }
    assert.match(fs.readFileSync(path.join(root, 'vendor/bootstrap-5.3.0/LICENSE'), 'utf8'), /Permission is hereby granted/);
});
test('缺 Bootstrap CSS 时关键安全规则独立存在，真实 HTML spinner 初始双重隐藏', () => {
    const css = fs.readFileSync(path.join(root, 'common.css'), 'utf8');
    assert.match(css, /\[hidden\]\s*\{\s*display:\s*none\s*!important/);
    assert.match(css, /\.dropdown-menu:not\(\.show\)\s*\{\s*display:\s*none/);
    assert.match(css, /\.topbar-right \.dropdown-menu\s*\{[^}]*position: absolute;[^}]*list-style: none/);
    assert.match(css, /\.visually-hidden\s*\{[^}]*clip: rect/);
    assert.match(css, /:where\(\.row > \*\)/); // fallback must not override Bootstrap grid classes
    const html = fs.readFileSync(path.join(root, 'upload.html'), 'utf8');
    assert.match(html, /id="loadingSpinner" hidden style="display: none;"/);
    assert.match(html, /id="uploadError"[^>]*role="alert" hidden/);
});
