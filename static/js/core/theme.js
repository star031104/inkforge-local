// Theme preference: "auto" follows the OS, "light"/"dark" are explicit.
// Loaded synchronously in <head> so the first paint already uses the right theme.
(function(){
  const KEY = 'inkforge.theme';
  const ORDER = ['auto', 'light', 'dark'];
  const LABELS = {auto: '跟随系统', light: '宣纸（浅色）', dark: '墨夜（深色）'};
  const ICONS = {auto: '◐', light: '☀', dark: '☾'};

  function read(){
    try { const value = localStorage.getItem(KEY); return ORDER.includes(value) ? value : 'auto'; }
    catch (_) { return 'auto'; }
  }
  function write(value){
    try { localStorage.setItem(KEY, value); } catch (_) { /* storage may be blocked */ }
  }
  function effective(pref){
    if(pref !== 'auto') return pref;
    try { return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
    catch (_) { return 'light'; }
  }
  function apply(pref){
    const root = document.documentElement;
    if(pref === 'auto') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', pref);
    root.dataset.themePref = pref;
    const meta = document.querySelector('meta[name="theme-color"]');
    if(meta) meta.setAttribute('content', effective(pref) === 'dark' ? '#1b1815' : '#a63a28');
    const button = document.getElementById('themeToggleBtn');
    if(button){
      button.textContent = ICONS[pref];
      button.title = `界面主题：${LABELS[pref]}（点击切换）`;
      button.setAttribute('aria-label', button.title);
    }
  }
  function cycle(){
    const next = ORDER[(ORDER.indexOf(read()) + 1) % ORDER.length];
    write(next);
    apply(next);
    return next;
  }

  apply(read());
  document.addEventListener('DOMContentLoaded', () => {
    apply(read());
    const button = document.getElementById('themeToggleBtn');
    if(button) button.addEventListener('click', cycle);
    // Header "工具" menu: close after choosing an item, on outside click or Escape.
    const menus = () => document.querySelectorAll('details.tool-menu[open]');
    document.addEventListener('click', event => {
      menus().forEach(menu => {
        if(!menu.contains(event.target) || event.target.closest('.menu-item')) menu.removeAttribute('open');
      });
    });
    document.addEventListener('keydown', event => {
      if(event.key === 'Escape') menus().forEach(menu => menu.removeAttribute('open'));
    });
  });
  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => apply(read()));
  } catch (_) { /* old engines */ }
  window.InkTheme = {read, apply, cycle, effective};
})();
