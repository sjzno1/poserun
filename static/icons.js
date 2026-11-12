// ===== PoseRun 内嵌运动风 SVG 图标库 v2.6 =====
// 零外部依赖：把页面里的 <i class="fas fa-xxx"> 全部替换为内联 SVG（线性运动风图标）。
// 用法：本脚本在 common.js 之后引入即可，页面/JS 动态插入的 <i> 图标也会被 MutationObserver 自动替换。
(function () {
    'use strict';

    var P = {
        running: '<path d="M13 4.2a1.7 1.7 0 1 0 0-3.4 1.7 1.7 0 0 0 0 3.4z"/><path d="M9.5 21l2-4.8-2.6-3.4.9-5.2 4-1.5 2.6 3.6 3.1 1.2"/><path d="M9.8 12.8L6.5 14l-1.6 4.6"/><path d="M12.4 16.2l2.1 2 1.2 3.4"/><path d="M2 12.5h3.4"/><path d="M3.2 8.6l2.8.9"/><path d="M2.6 16.6l2.9-.6"/>',
        'arrow-right': '<path d="M4.5 12h15"/><path d="M13.5 6l6 6-6 6"/>',
        'chart-line': '<path d="M3 3v18h18"/><path d="M6.5 15.5l3.6-4.4 3 2.6 5.4-6.7"/><path d="M18.5 7h2v2"/>',
        video: '<rect x="2.5" y="6" width="13" height="12" rx="2.5"/><path d="M15.5 10.5l6-3.5v10l-6-3.5"/>',
        upload: '<path d="M12 16V4.5"/><path d="M7 9l5-5 5 5"/><path d="M4.5 20h15"/>',
        'cloud-upload-alt': '<path d="M7 18.5A4.5 4.5 0 0 1 7.4 9.5 5.5 5.5 0 0 1 18 11a4 4 0 0 1-.5 7.5"/><path d="M12 21v-8"/><path d="M8.8 15.8l3.2-3.2 3.2 3.2"/>',
        'users-cog': '<circle cx="9" cy="8.5" r="3.5"/><path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"/><circle cx="17.5" cy="8" r="2.4"/><path d="M17.5 11.6v2.2"/><path d="M15.7 12.7h3.6"/>',
        users: '<circle cx="9" cy="8.5" r="3.5"/><path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"/><circle cx="17.5" cy="8" r="2.4"/><path d="M17.5 11.6v2.2"/><path d="M15.7 12.7h3.6"/>',
        history: '<path d="M12 7.5V12l3 2"/><path d="M12 3a9 9 0 1 1-8.2 5.2"/><path d="M3.5 4v4.2H7.7"/>',
        download: '<path d="M12 4.5V16"/><path d="M7 11.5l5 5 5-5"/><path d="M4.5 20h15"/>',
        'file-alt': '<path d="M6 2.5h7.5L18 7v14.5H6z"/><path d="M13.5 2.5V7H18"/><path d="M9 12.5h6M9 16h6"/>',
        file: '<path d="M6 2.5h7.5L18 7v14.5H6z"/><path d="M13.5 2.5V7H18"/>',
        'user-circle': '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="10" r="3.2"/><path d="M5.8 19.2a6.5 6.5 0 0 1 12.4 0"/>',
        user: '<circle cx="12" cy="8" r="3.6"/><path d="M4.5 20.5c.8-3.8 3.9-6 7.5-6s6.7 2.2 7.5 6"/>',
        'user-slash': '<circle cx="12" cy="8" r="3.6"/><path d="M4.5 20.5c.8-3.8 3.9-6 7.5-6s6.7 2.2 7.5 6"/><path d="M4 4l16 16"/>',
        'user-lock': '<rect x="5.5" y="10.5" width="9" height="7" rx="1.5"/><path d="M7.5 10.5V8.5a2.5 2.5 0 0 1 5 0v2"/><circle cx="20" cy="9" r="2.2"/><path d="M20 11.2v2.6"/>',
        'sign-out-alt': '<path d="M14.5 8V5.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2V16"/><path d="M9 12h11.5"/><path d="M17 8.5l3.5 3.5-3.5 3.5"/>',
        pen: '<path d="M4 20l1-4L16.5 4.5a2.1 2.1 0 0 1 3 3L8 19z"/><path d="M14.5 6.5l3 3"/>',
        key: '<circle cx="8" cy="14.5" r="4.5"/><path d="M11.5 11L20 2.5"/><path d="M16.5 6l3 3"/><path d="M13.5 9l2.5 2.5"/>',
        play: '<path d="M8 5.5l11 6.5-11 6.5z"/>',
        stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
        sync: '<path d="M20.5 5v5h-5"/><path d="M3.5 19v-5h5"/><path d="M19.6 10a8 8 0 0 0-14-3.5"/><path d="M4.4 14a8 8 0 0 0 14 3.5"/>',
        'exchange-alt': '<path d="M4 7.5h13"/><path d="M13.5 3.5l4 4-4 4"/><path d="M20 16.5H7"/><path d="M10.5 12.5l-4 4 4 4"/>',
        database: '<ellipse cx="12" cy="5.5" rx="7.5" ry="3"/><path d="M4.5 5.5v13c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-13"/><path d="M4.5 12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3"/>',
        times: '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
        'arrow-up': '<path d="M12 19V5"/><path d="M6 11l6-6 6 6"/>',
        'arrow-down': '<path d="M12 5v14"/><path d="M6 13l6 6 6-6"/>',
        minus: '<path d="M5 12h14"/>',
        bars: '<path d="M4 6h16M4 12h16M4 18h16"/>',
        circle: '<circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/>',
        'user-edit': '<circle cx="9" cy="7" r="3"/><path d="M3 20v-2a6 6 0 0 1 9-5M14 20l1-4 5-5 3 3-5 5z"/>',
        link: '<path d="M10 14l4-4M8 16l-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0M16 8l1-1a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0" transform="translate(1 0) scale(.92)"/>',
        'chart-area': '<path d="M3 3v18h18M6 17V9l5 3 5-7 5 5v7z"/>',
        'list-alt': '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h1m3 0h6M7 12h1m3 0h6M7 16h1m3 0h6"/>',
        camera: '<path d="M3 7h4l2-3h6l2 3h4v14H3z"/><circle cx="12" cy="13" r="4"/>',
        dumbbell: '<path d="M7 12h10M3 8v8m4-11v14M21 8v8m-4-11v14"/>',
        'exclamation-triangle': '<path d="M12 3L2 21h20zM12 9v5m0 3v.5"/>',
        'info-circle': '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.5"/>',
        lightbulb: '<path d="M8 17c0-3-3-3-3-7a7 7 0 0 1 14 0c0 4-3 4-3 7M8 17h8M9 21h6"/>',
        ruler: '<path d="M3 16L16 3l5 5L8 21zM7 12l3 3m1-7 3 3m1-7 3 3"/>',
        save: '<path d="M3 3h15l3 3v15H3zM7 3v6h10V3M7 21v-8h10v8"/>',
        spinner: '<path d="M21 12a9 9 0 1 1-9-9"/>',
        'tachometer-alt': '<path d="M3 18a10 10 0 1 1 18 0zM12 14l5-6"/><circle cx="12" cy="14" r="2"/>',
        trophy: '<path d="M7 3h10v6a5 5 0 0 1-10 0zM7 5H3v3a4 4 0 0 0 4 4m10-7h4v3a4 4 0 0 1-4 4M12 14v7m-5 0h10"/>',
        undo: '<path d="M3 3v6h6M3 9a9 9 0 1 1 0 7"/>',
        check: '<path d="M4.5 12.5l5 5 10-11"/>'
    };
    P['line-chart'] = P['chart-line'];
    P['file-code'] = P['file-alt'];
    P['file-csv'] = P['file-alt'];

    function svgFor(name) {
        var body = P[name];
        if (!body) return null;
        return '<svg class="pr-icon" data-pr-icon="' + name + '" xmlns="http://www.w3.org/2000/svg" ' +
            'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ' +
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + '</svg>';
    }

    function replaceIcon(i) {
        if (!i || i.nodeType !== 1 || i.tagName.toLowerCase() !== 'i') return false;
        if (i.getAttribute('data-pr-replaced')) return false;
        var cls = (i.className || '').toString();
        if (cls.indexOf('fa-') === -1) return false;
        var name = null;
        cls.split(/\s+/).some(function (c) {
            if (c.indexOf('fa-') === 0 && P[c.slice(3)]) { name = c.slice(3); return true; }
            return false;
        });
        if (!name) return false;
        var svg = svgFor(name);
        if (!svg) return false;
        i.setAttribute('data-pr-replaced', '1');
        var wrap = document.createElement('span');
        wrap.innerHTML = svg;
        var el = wrap.firstChild;
        if (i.classList) i.classList.forEach(function (c) { el.classList.add(c); });
        if (i.parentNode) i.parentNode.replaceChild(el, i);
        return true;
    }

    function replaceAll(root) {
        (root || document).querySelectorAll('i.fa, i.fas, i.fab, i[class*="fa-"]').forEach(replaceIcon);
    }

    // 页面/JS 动态插入的图标也要替换（渲染完成后统一补一次 + 观察后续变更）
    function init() {
        replaceAll(document);
        if (window.MutationObserver) {
            var obs = new MutationObserver(function (muts) {
                muts.forEach(function (m) {
                    m.addedNodes.forEach(function (n) {
                        if (n.nodeType !== 1) return;
                        if (n.classList && n.classList.contains('pr-icon')) return;
                        if (n.tagName.toLowerCase() === 'i') { replaceIcon(n); return; }
                        replaceAll(n);
                    });
                });
            });
            obs.observe(document.body, { childList: true, subtree: true });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
