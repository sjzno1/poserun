/* Shared theme tokens ported from design-demos. No health/credential storage. */
(() => {
  const themes = {clarion:'澄明训练台', nightlab:'夜航负荷室', fieldnote:'林间训练札'};
  const key = 'running-management.theme';
  function apply(id) {
    id = Object.hasOwn ? (Object.hasOwn(themes,id) ? id : 'clarion') : (Object.keys(themes).includes(id) ? id : 'clarion');
    document.documentElement.dataset.theme = id;
    try { localStorage.setItem(key, id); } catch (_) { /* storage disabled */ }
    document.querySelectorAll('[data-theme-select]').forEach(el => {el.value = id;});
  }
  try { apply(localStorage.getItem(key)); } catch (_) { apply('clarion'); }
  const init = async () => {
    const bar = document.createElement('label');
    bar.className = 'theme-bar'; bar.textContent = '外观 ';
    const select = document.createElement('select'); select.dataset.themeSelect = '';
    select.setAttribute('aria-label','外观主题');
    Object.entries(themes).forEach(([id,name]) => select.add(new Option(name,id)));
    bar.append(select); document.querySelector('.topbar')?.append(bar);
    document.querySelectorAll('[data-theme-select]').forEach(el => {
      if (!el.options.length) Object.entries(themes).forEach(([id,name]) => el.add(new Option(name,id)));
      el.addEventListener('change', async () => {
        apply(el.value);
        if (typeof authToken !== 'undefined' && authToken) {
          try { const r = await apiFetch('/api/profile/theme', {method:'PUT', headers:{'Content-Type':'application/json'},body:JSON.stringify({theme:el.value})}); if(!r.ok) throw Error(); }
          catch (_) { document.dispatchEvent(new CustomEvent('theme-save-error')); }
        }
      });
    });
    apply(document.documentElement.dataset.theme);
    if (typeof authToken !== 'undefined' && authToken) {
      try {const r = await apiFetch('/api/profile/theme'); if(r.ok) apply((await r.json()).theme);} catch (_) { /* offline preference */ }
    }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
