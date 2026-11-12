// ===== PoseRun 共享脚本 v2.8.0（多页面版）=====
// 页面在引入本脚本前设置 window.PAGE = 'home' | 'upload' | 'results' | 'history' | 'camera' | 'users'
// 本脚本负责：登录/注册遮罩、导航栏、页脚注入，令牌校验，退出登录
window.PAGE = window.PAGE || 'home';

let authToken = localStorage.getItem('poserun_token') || null;
let authUser = null;
try { authUser = JSON.parse(localStorage.getItem('poserun_user') || 'null'); } catch (e) { authUser = null; }

// ===== 带鉴权的 fetch 封装 =====
async function apiFetch(url, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (authToken) headers['Authorization'] = 'Bearer ' + authToken;
    options.headers = headers;
    const resp = await fetch(url, options);
    if (resp.status === 401 && !url.startsWith('/api/auth/login') && !url.startsWith('/api/auth/register')) {
        clearSession();
        showAuthOverlay('登录已过期，请重新登录');
        throw new Error('unauthorized');
    }
    return resp;
}

function clearSession() {
    authToken = null;
    authUser = null;
    localStorage.removeItem('poserun_token');
    localStorage.removeItem('poserun_user');
    localStorage.removeItem('poserun_last_result');
}

// HTML转义
function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ===== 页面骨架注入：登录遮罩 + 侧边栏 + 顶栏 + 页脚（Portainer 风格） =====
function buildShell() {
    const pageTitles = {
        home: '概览',
        upload: '自动主体分析',
        results: '分析结果',
        history: '历史记录',
        camera: '摄像头预览',
        training: '训练联动',
        users: '人员管理',
        logs: '操作日志',
        profile: '跑者画像',
        trend: '趋势追踪',
    };
    const navItems = [
        ['home', '/', '首页', 'fa-running', '分析'],
        ['upload', '/upload.html', '上传分析', 'fa-upload', '分析'],
        ['results', '/results.html', '分析结果', 'fa-chart-line', '分析'],
        ['camera', '/camera.html', '摄像头预览', 'fa-video', '分析'],
        ['history', '/history.html', '历史记录', 'fa-history', '数据'],
        ['trend', '/trend.html', '趋势追踪', 'fa-chart-area', '数据'],
        ['profile', '/profile.html', '跑者画像', 'fa-user-edit', '数据'],
        ['training', '/training.html', '训练联动', 'fa-link', '数据'],
        ['users', '/users.html', '人员管理', 'fa-users-cog', '系统'],
        ['logs', '/logs.html', '操作日志', 'fa-list-alt', '系统'],
    ];
    // 按分组渲染侧边栏（users / logs 项仅管理员可见，占位 id 保留）
    let sections = [];
    navItems.forEach(function (it) {
        const active = window.PAGE === it[0] ? ' active' : '';
        const hidden = (it[0] === 'users' || it[0] === 'logs') ? ' style="display: none;"' : '';
        const liId = it[0] === 'users' ? 'navUsersItem' : (it[0] === 'logs' ? 'navLogsItem' : '');
        const li = '<li class="nav-item"' + (liId ? ' id="' + liId + '"' : '') + (hidden ? ' style="display: none;"' : '') + '>' +
            '<a class="nav-link' + active + '" href="' + it[1] + '"><i class="fas ' + it[3] + '"></i>' + it[2] + '</a></li>';
        const last = sections[sections.length - 1];
        if (last && last[0] === it[4]) last[1] += li;
        else sections.push([it[4], li]);
    });
    const sectionHtml = sections.map(function (s) {
        return '<div class="sidebar-section">' + s[0] + '</div><ul class="sidebar-nav">' + s[1] + '</ul>';
    }).join('');

    const title = pageTitles[window.PAGE] || 'PoseRun';

    document.body.insertAdjacentHTML('afterbegin',
        '<div id="authOverlay" class="auth-overlay" style="display: none;">' +
        '  <div class="auth-card">' +
        '    <div class="auth-header">' +
        '      <div class="auth-logo"><i class="fas fa-running"></i></div>' +
        '      <h4>PoseRun 跑姿分析系统</h4>' +
        '      <p>登录后开始跑姿分析</p>' +
        '    </div>' +
        '    <div class="auth-body">' +
        '      <ul class="nav nav-tabs auth-tabs" id="authTabs">' +
        '        <li class="nav-item"><a class="nav-link active" id="tabLogin" data-bs-toggle="tab" href="#paneLogin">登录</a></li>' +
        '        <li class="nav-item"><a class="nav-link" id="tabRegister" data-bs-toggle="tab" href="#paneRegister">注册</a></li>' +
        '      </ul>' +
        '      <div class="tab-content mt-3">' +
        '        <div class="tab-pane fade show active" id="paneLogin">' +
        '          <div class="mb-3"><label class="form-label" for="loginUsername">用户名</label>' +
        '          <input type="text" class="form-control" id="loginUsername" autocomplete="username" maxlength="32"></div>' +
        '          <div class="mb-3"><label class="form-label" for="loginPassword">密码</label>' +
        '          <input type="password" class="form-control" id="loginPassword" autocomplete="current-password"></div>' +
        '          <button class="btn btn-primary w-100" id="loginBtn">登 录</button>' +
        '        </div>' +
        '        <div class="tab-pane fade" id="paneRegister">' +
        '          <div class="mb-3"><label class="form-label" for="regUsername">用户名 <span class="text-muted small">（3-32位，字母/数字/下划线）</span></label>' +
        '          <input type="text" class="form-control" id="regUsername" autocomplete="username" maxlength="32"></div>' +
        '          <div class="mb-3"><label class="form-label">姓名（用于报告署名）</label>' +
        '          <input type="text" class="form-control" id="regName" maxlength="20"></div>' +
        '          <div class="mb-3"><label class="form-label" for="regPassword">密码 <span class="text-muted small">（至少6位）</span></label>' +
        '          <input type="password" class="form-control" id="regPassword" autocomplete="new-password"></div>' +
        '          <div class="mb-3"><label class="form-label" for="regPassword2">确认密码</label>' +
        '          <input type="password" class="form-control" id="regPassword2" autocomplete="new-password"></div>' +
        '          <button class="btn btn-primary w-100" id="registerBtn">注 册</button>' +
        '        </div>' +
        '      </div>' +
        '      <div id="authError" class="alert mt-3" style="display: none;"></div>' +
        '      <p class="auth-hint text-center mt-2 mb-0">首次使用？切换到"注册"创建账号</p>' +
        '    </div>' +
        '  </div>' +
        '</div>');

    // 侧边栏 + 顶栏
    document.body.insertAdjacentHTML('afterbegin',
        '<aside class="sidebar" id="appSidebar">' +
        '  <a class="sidebar-brand" href="/">' +
        '    <i class="fas fa-running"></i>' +
        '    <span>PoseRun<small>跑姿分析系统</small></span>' +
        '  </a>' +
        sectionHtml +
        '  <div class="sidebar-footer">v__POSERUN_VERSION__ · YOLO Pose</div>' +
        '</aside>' +
        '<div class="sidebar-backdrop" id="sidebarBackdrop"></div>' +
        '<header class="topbar">' +
        '  <button class="topbar-toggle d-lg-none" id="sidebarToggle" type="button" aria-label="打开菜单" aria-controls="appSidebar" aria-expanded="false"><i class="fas fa-bars"></i></button>' +
        '  <span class="topbar-title d-none d-sm-inline"><i class="fas fa-circle me-2"></i>' + title + '</span>' +
        '  <div class="topbar-right">' +
        '    <div class="dropdown">' +
        '      <button class="topbar-user dropdown-toggle" type="button" id="navUserLabel" aria-controls="accountMenu" aria-expanded="false">' +
        '        <span class="avatar"><i class="fas fa-user"></i></span><span id="navUserName">-</span>' +
        '      </button>' +
        '      <ul class="dropdown-menu dropdown-menu-end" id="accountMenu" hidden>' +
        '        <li><span class="dropdown-item-text small text-muted" id="navUserDetail"></span></li>' +
        '        <li><hr class="dropdown-divider"></li>' +
        '        <li><a class="dropdown-item" href="#" id="navEditNameBtn"><i class="fas fa-pen me-2"></i>修改姓名</a></li>' +
        '        <li><a class="dropdown-item" href="#" id="navChangePwdBtn"><i class="fas fa-key me-2"></i>修改密码</a></li>' +
        '        <li><hr class="dropdown-divider"></li>' +
        '        <li><a class="dropdown-item text-danger" href="#" id="navDeleteAccountBtn"><i class="fas fa-user-slash me-2"></i>注销账号</a></li>' +
        '        <li><hr class="dropdown-divider"></li>' +
        '        <li><a class="dropdown-item" href="#" id="navLogoutBtn"><i class="fas fa-sign-out-alt me-2"></i>退出登录</a></li>' +
        '      </ul>' +
        '    </div>' +
        '  </div>' +
        '</header>');

    // 移动端菜单：可重复切换，支持遮罩/Escape关闭并同步可访问状态。
    const menuButton = document.getElementById('sidebarToggle');
    const sidebar = document.getElementById('appSidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    function setSidebar(open) {
        sidebar.classList.toggle('open', open);
        backdrop.classList.toggle('show', open);
        menuButton.setAttribute('aria-expanded', String(open));
        menuButton.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
        document.body.classList.toggle('menu-open', open);
    }
    menuButton.addEventListener('click', () => setSidebar(!sidebar.classList.contains('open')));
    backdrop.addEventListener('click', () => setSidebar(false));
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && sidebar.classList.contains('open')) { setSidebar(false); menuButton.focus(); } });
    window.addEventListener('resize', () => { if (window.innerWidth >= 992) setSidebar(false); });
    sidebar.querySelectorAll('a').forEach(a => a.addEventListener('click', () => setSidebar(false)));
    sidebar.querySelectorAll('.nav-link.active').forEach(a => a.setAttribute('aria-current', 'page'));
    // Account menu owns its interactions; do not double-bind Bootstrap's data API.
    const accountButton = document.getElementById('navUserLabel');
    const accountMenu = document.getElementById('accountMenu');
    function setAccountMenu(open) {
        accountMenu.hidden = !open;
        accountMenu.classList.toggle('show', open);
        accountButton.setAttribute('aria-expanded', String(open));
    }
    accountButton.addEventListener('click', () => setAccountMenu(accountMenu.hidden));
    document.addEventListener('click', e => {
        if (!accountButton.contains(e.target) && !accountMenu.contains(e.target)) setAccountMenu(false);
    });
    accountMenu.addEventListener('click', e => {
        if (e.target.closest('a,button')) setAccountMenu(false);
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !accountMenu.hidden) {
            setAccountMenu(false);
            accountButton.focus();
        }
    });
    // Login/register still works if the local Bootstrap bundle fails to load.
    ['tabLogin', 'tabRegister'].forEach((id, index) => {
        const tab = document.getElementById(id);
        tab.removeAttribute('data-bs-toggle');
        tab.addEventListener('click', e => {
            e.preventDefault();
            ['paneLogin', 'paneRegister'].forEach((pane, i) => {
                const active = i === index;
                document.getElementById(pane).classList.toggle('active', active);
                document.getElementById(pane).classList.toggle('show', active);
                document.getElementById(i ? 'tabRegister' : 'tabLogin').classList.toggle('active', active);
            });
        });
    });

    document.body.insertAdjacentHTML('beforeend',
        '<div class="modal fade" id="editNameModal" tabindex="-1">' +
        '  <div class="modal-dialog"><div class="modal-content">' +
        '    <div class="modal-header"><h5 class="modal-title">修改姓名</h5>' +
        '      <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>' +
        '    <div class="modal-body">' +
        '      <div class="mb-2"><label class="form-label">姓名（用于报告署名）</label>' +
        '        <input type="text" class="form-control" id="editNameInput" maxlength="20"></div>' +
        '      <div id="editNameError" class="alert alert-danger py-2" style="display:none;"></div>' +
        '    </div>' +
        '    <div class="modal-footer">' +
        '      <button class="btn btn-secondary" data-bs-dismiss="modal">取消</button>' +
        '      <button class="btn btn-primary" id="editNameSaveBtn">保存</button>' +
        '    </div>' +
        '  </div></div>' +
        '</div>' +
        '<div class="modal fade" id="changePwdModal" tabindex="-1">' +
        '  <div class="modal-dialog"><div class="modal-content">' +
        '    <div class="modal-header"><h5 class="modal-title">修改密码</h5>' +
        '      <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>' +
        '    <div class="modal-body">' +
        '      <div class="mb-3"><label class="form-label">当前密码</label>' +
        '        <input type="password" class="form-control" id="pwdOld" autocomplete="current-password"></div>' +
        '      <div class="mb-3"><label class="form-label">新密码 <span class="text-muted small">（至少6位）</span></label>' +
        '        <input type="password" class="form-control" id="pwdNew" autocomplete="new-password"></div>' +
        '      <div class="mb-2"><label class="form-label">确认新密码</label>' +
        '        <input type="password" class="form-control" id="pwdNew2" autocomplete="new-password"></div>' +
        '      <div id="pwdError" class="alert alert-danger py-2" style="display:none;"></div>' +
        '    </div>' +
        '    <div class="modal-footer">' +
        '      <button class="btn btn-secondary" data-bs-dismiss="modal">取消</button>' +
        '      <button class="btn btn-primary" id="pwdSaveBtn">修改密码</button>' +
        '    </div>' +
        '  </div></div>' +
        '</div>' +
        '<footer class="footer">' +
        '  <div class="container">' +
        '    <p class="mb-1"><i class="fas fa-running me-1"></i>PoseRun 跑姿分析系统 v__POSERUN_VERSION__ | 基于 YOLO 的姿态识别技术</p>' +
        '    <p class="mb-0 small">使用问题请在项目仓库提交 Issue 反馈</p>' +
        '  </div>' +
        '</footer>');
}

// ===== 登录/注册界面 =====
// 注意：遮罩 DOM 由 buildShell() 在 DOMContentLoaded 时注入，
// 脚本解析时尚不存在，必须延迟获取，否则顶层 const 拿到 null 导致登录框无法显示
function getAuthOverlay() { return document.getElementById('authOverlay'); }
function getAuthErrorBox() { return document.getElementById('authError'); }

function showAuthOverlay(msg) {
    const overlay = getAuthOverlay();
    if (!overlay) return;
    overlay.style.display = 'flex';
    if (msg) {
        const box = getAuthErrorBox();
        box.textContent = msg;
        box.style.display = 'block';
    }
}

function hideAuthOverlay() {
    const overlay = getAuthOverlay();
    const box = getAuthErrorBox();
    if (overlay) overlay.style.display = 'none';
    if (box) box.style.display = 'none';
}

function applyAuthedUI() {
    hideAuthOverlay();
    const name = (authUser && (authUser.name || authUser.username)) || '用户';
    const isAdmin = authUser && authUser.role === 'admin';
    document.getElementById('navUserName').textContent = name;
    document.getElementById('navUserDetail').innerHTML =
        '用户名: ' + escapeHtml(authUser.username) +
        ' <span class="badge ' + (isAdmin ? 'badge-admin' : 'badge-user') + ' ms-1">' +
        (isAdmin ? '管理员' : '普通用户') + '</span>';
    document.getElementById('navUsersItem').style.display = isAdmin ? '' : 'none';
    const logsItem = document.getElementById('navLogsItem');
    if (logsItem) logsItem.style.display = isAdmin ? '' : 'none';
    // 页面级钩子：各页面可定义 onAuthReady(user) 做初始化
    if (typeof onAuthReady === 'function') onAuthReady(authUser);
}

async function doLogin() {
    const username = document.getElementById('loginUsername').value.trim();
    const password = document.getElementById('loginPassword').value;
    if (!username || !password) { showAuthOverlay('请输入用户名和密码'); return; }
    try {
        const resp = await apiFetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username, password: password })
        });
        const data = await resp.json();
        if (!resp.ok) { showAuthOverlay(data.error || '登录失败'); return; }
        authToken = data.token;
        authUser = data.user;
        localStorage.setItem('poserun_token', authToken);
        localStorage.setItem('poserun_user', JSON.stringify(authUser));
        localStorage.removeItem('poserun_last_result');
        location.reload(); // 新会话重建页面，防止沿用上一账号的内存数据。
    } catch (err) {
        showAuthOverlay('网络错误，请稍后重试');
    }
}

async function doRegister() {
    const username = document.getElementById('regUsername').value.trim();
    const name = document.getElementById('regName').value.trim();
    const p1 = document.getElementById('regPassword').value;
    const p2 = document.getElementById('regPassword2').value;
    if (!username || !p1) { showAuthOverlay('请填写用户名和密码'); return; }
    if (p1 !== p2) { showAuthOverlay('两次输入的密码不一致'); return; }
    try {
        const resp = await apiFetch('/api/auth/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username, name: name, password: p1 })
        });
        const data = await resp.json();
        if (!resp.ok) { showAuthOverlay(data.error || '注册失败'); return; }
        // 注册成功后自动登录
        document.getElementById('loginUsername').value = username;
        document.getElementById('loginPassword').value = p1;
        const errBox = getAuthErrorBox();
        if (errBox) errBox.style.display = 'none';
        await doLogin();
    } catch (err) {
        showAuthOverlay('网络错误，请稍后重试');
    }
}

// ===== 个人账户：改姓名 / 改密码 / 注销 =====
function showAccountError(elId, msg) {
    const el = document.getElementById(elId);
    el.textContent = msg;
    el.style.display = 'block';
}
function hideAccountError(elId) {
    document.getElementById(elId).style.display = 'none';
}

function openEditNameModal() {
    const input = document.getElementById('editNameInput');
    input.value = (authUser && authUser.name) || '';
    hideAccountError('editNameError');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('editNameModal')).show();
    setTimeout(function () { input.focus(); }, 300);
}

async function saveEditName() {
    const name = document.getElementById('editNameInput').value.trim();
    if (!name) { showAccountError('editNameError', '姓名不能为空'); return; }
    try {
        const resp = await apiFetch('/api/auth/me', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name })
        });
        const data = await resp.json();
        if (!resp.ok) { showAccountError('editNameError', data.error || '保存失败'); return; }
        authUser = data.user;
        localStorage.setItem('poserun_user', JSON.stringify(authUser));
        document.getElementById('navUserName').textContent = authUser.name || authUser.username;
        bootstrap.Modal.getInstance(document.getElementById('editNameModal')).hide();
    } catch (err) { showAccountError('editNameError', '网络错误，请稍后重试'); }
}

function openChangePwdModal() {
    ['pwdOld', 'pwdNew', 'pwdNew2'].forEach(function (id) { document.getElementById(id).value = ''; });
    hideAccountError('pwdError');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('changePwdModal')).show();
    setTimeout(function () { document.getElementById('pwdOld').focus(); }, 300);
}

async function saveChangePwd() {
    const oldPwd = document.getElementById('pwdOld').value;
    const p1 = document.getElementById('pwdNew').value;
    const p2 = document.getElementById('pwdNew2').value;
    if (!oldPwd || !p1) { showAccountError('pwdError', '请填写完整'); return; }
    if (p1.length < 6) { showAccountError('pwdError', '新密码至少 6 位'); return; }
    if (p1 !== p2) { showAccountError('pwdError', '两次输入的新密码不一致'); return; }
    try {
        const resp = await apiFetch('/api/auth/me/password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_password: oldPwd, new_password: p1 })
        });
        const data = await resp.json();
        if (!resp.ok) { showAccountError('pwdError', data.error || '修改失败'); return; }
        bootstrap.Modal.getInstance(document.getElementById('changePwdModal')).hide();
        alert('密码已修改，下次登录请使用新密码。');
    } catch (err) { showAccountError('pwdError', '网络错误，请稍后重试'); }
}

async function deleteMyAccount() {
    if (!confirm('确定要注销账号吗？注销后该账号将无法再登录，且不可恢复。')) return;
    if (!confirm('再次确认：注销后不可恢复，确定继续？')) return;
    try {
        const resp = await apiFetch('/api/auth/me', { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok) { alert(data.error || '注销失败'); return; }
        clearSession();
        location.href = '/';
        location.reload();
    } catch (err) { alert('网络错误，请稍后重试'); }
}

// ===== 初始化：注入骨架 + 绑定事件 + 校验本地令牌 =====
function initApp() {
    buildShell();
    function bind(id, event, fn) { var el = document.getElementById(id); if (el) el.addEventListener(event, fn); }
    bind('loginBtn', 'click', doLogin);
    bind('registerBtn', 'click', doRegister);
    bind('loginPassword', 'keydown', function (e) { if (e.key === 'Enter') doLogin(); });
    bind('navLogoutBtn', 'click', async function (e) {
        e.preventDefault();
        const logoutToken = authToken;
        clearSession();
        showAuthOverlay();
        try {
            await fetch('/api/auth/logout', {
                method: 'POST',
                headers: logoutToken ? { 'Authorization': 'Bearer ' + logoutToken } : {},
            });
        } catch (err) { /* 即使离线，本地也应退出 */ }
        location.reload(); // 清空上一账号留在页面内存中的记录与结果。
    });
    bind('navEditNameBtn', 'click', function (e) { e.preventDefault(); openEditNameModal(); });
    bind('navChangePwdBtn', 'click', function (e) { e.preventDefault(); openChangePwdModal(); });
    bind('navDeleteAccountBtn', 'click', function (e) { e.preventDefault(); deleteMyAccount(); });
    bind('editNameSaveBtn', 'click', saveEditName);
    bind('editNameInput', 'keydown', function (e) { if (e.key === 'Enter') saveEditName(); });
    bind('pwdSaveBtn', 'click', saveChangePwd);
    if (typeof exportToCsv === 'function') bind('exportCsvBtn', 'click', exportToCsv);
    if (typeof exportToJson === 'function') bind('exportJsonBtn', 'click', exportToJson);

    if (authToken) {
        apiFetch('/api/auth/me').then(async function (resp) {
            if (resp.ok) {
                const data = await resp.json();
                authUser = data.user;
                localStorage.setItem('poserun_user', JSON.stringify(authUser));
                // buildShell同步完成；认证初始化不能依赖后台标签页暂停的动画帧。
                applyAuthedUI();
            } else {
                clearSession();
                showAuthOverlay();
            }
        }).catch(function () {
            clearSession();
            showAuthOverlay();
        });
    } else {
        showAuthOverlay();
    }
}

// 如果 DOM 已加载完成，立即初始化；否则等待 DOMContentLoaded
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initApp);
} else {
    initApp();
}
