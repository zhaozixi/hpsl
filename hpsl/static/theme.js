// M36/C9：主题初始化与切换（自 base.html 内联脚本原样外置）。
// 严格 CSP 下不再允许内联 <script>；本文件在 <head> 中同步加载（非 defer），
// 先于 body 渲染执行，保证深色偏好不会闪白。偏好存 localStorage（零 DDL）。
(function () {
  try {
    if (localStorage.getItem('hpsl-theme') === 'dark') {
      document.documentElement.classList.add('dark');
    }
  } catch (e) {}
})();

function hpslToggleTheme() {
  var d = document.documentElement;
  var isDark = d.classList.toggle('dark');
  try { localStorage.setItem('hpsl-theme', isDark ? 'dark' : 'light'); } catch (e) {}
  var b = document.getElementById('theme-toggle-btn');
  if (b) {
    b.textContent = isDark ? '主题：浅色' : '主题：深色';
    b.setAttribute('aria-pressed', isDark ? 'true' : 'false');
  }
}

function hpslSyncThemeLabel() {
  var d = document.documentElement;
  var b = document.getElementById('theme-toggle-btn');
  if (b) {
    b.textContent = d.classList.contains('dark') ? '主题：浅色' : '主题：深色';
    b.setAttribute('aria-pressed', d.classList.contains('dark') ? 'true' : 'false');
  }
}

document.addEventListener('DOMContentLoaded', hpslSyncThemeLabel);
