// CSRF 双提交：从 hpsl_csrf cookie 读取令牌并注入 HTMX 请求头 X-CSRF-Token。
// 写请求同时需带 Origin（由浏览器自动附加），两者一致才放行（security.write_security）。
(function () {
  function getCookie(name) {
    var m = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return m ? decodeURIComponent(m[2]) : null;
  }
  document.addEventListener('htmx:configRequest', function (evt) {
    var t = getCookie('hpsl_csrf');
    if (t) evt.detail.headers['X-CSRF-Token'] = t;
  });
})();

// 术语提示气泡：给所有 [data-tip]（包括 .tip 行内术语）做悬浮解释，替代晦涩学术名词。
// 在页面加载与每次 HTMX 局部刷新后都重新绑定，保证动态内容也有提示。
(function () {
  var pop = null;
  function ensurePop() {
    if (pop) return pop;
    pop = document.createElement('div');
    pop.className = 'tip-pop';
    pop.setAttribute('role', 'tooltip');
    document.body.appendChild(pop);
    return pop;
  }
  function show(el) {
    var text = el.getAttribute('data-tip') || el.getAttribute('title') || '';
    if (!text) return;
    var p = ensurePop();
    p.textContent = text;
    p.classList.add('show');
    var r = el.getBoundingClientRect();
    var pr = p.getBoundingClientRect();
    var left = r.left + window.scrollX;
    var top = r.bottom + window.scrollY + 6;
    if (left + pr.width > window.scrollX + document.documentElement.clientWidth - 8) {
      left = window.scrollX + document.documentElement.clientWidth - pr.width - 8;
    }
    if (left < window.scrollX + 8) left = window.scrollX + 8;
    p.style.left = left + 'px';
    p.style.top = top + 'px';
  }
  function hide() { if (pop) pop.classList.remove('show'); }
  function bind() {
    var els = document.querySelectorAll('[data-tip]');
    for (var i = 0; i < els.length; i++) {
      (function (el) {
        if (el.__tipBound) return;
        el.__tipBound = true;
        el.addEventListener('mouseenter', function () { show(el); });
        el.addEventListener('mouseleave', hide);
        el.addEventListener('focus', function () { show(el); });
        el.addEventListener('blur', hide);
      })(els[i]);
    }
  }
  document.addEventListener('DOMContentLoaded', bind);
  document.body && document.addEventListener('htmx:afterSwap', bind);
  // 兜底：若脚本在 DOMContentLoaded 之后才执行
  if (document.readyState !== 'loading') bind();
})();


// 统一错误提示（M24）：BizError 以 JSON + 4xx 返回，HTMX 默认不 swap 4xx，
// 这里兜底成 toast，避免「点了没反应」。
(function () {
  var box = null, timer = null;
  function toast(text) {
    if (!box) {
      box = document.createElement('div');
      box.className = 'toast';
      box.setAttribute('role', 'alert');
      document.body.appendChild(box);
    }
    box.textContent = text;
    box.classList.add('show');
    clearTimeout(timer);
    timer = setTimeout(function () { box.classList.remove('show'); }, 5000);
  }
  function messageOf(xhr) {
    try {
      var d = JSON.parse(xhr.responseText);
      if (d && d.msg) return d.msg + (d.code ? '（' + d.code + '）' : '');
    } catch (e) {}
    return '请求失败（HTTP ' + ((xhr && xhr.status) || 0) + '）';
  }
  document.addEventListener('htmx:responseError', function (evt) { toast(messageOf(evt.detail.xhr)); });
  document.addEventListener('htmx:sendError', function () { toast('网络不可达，请检查本地服务是否在运行'); });
  window.hpslToast = toast;  // 供非 HTMX 的 fetch 流程（如项目切换）复用同一提示样式
})();


// 成功提示 + 刷新（M36/P1-B2）：写接口成功后不再直接 HX-Refresh（提示会被新页面冲掉），
// 改由服务端下发 HX-Trigger: {"hpsl:refresh": {"msg": "…"}}；先弹提示，再把页面刷新到最新。
(function () {
  document.addEventListener('hpsl:refresh', function (evt) {
    var detail = (evt && evt.detail) || {};
    var msg = detail.msg || detail.value || '操作成功，正在刷新…';
    if (window.hpslToast) window.hpslToast(msg);
    setTimeout(function () { window.location.reload(); }, 550);
  });
})();


// 弹层可关闭（无障碍）：Esc 关闭页面上所有 .modal；配合弹层内的「关闭」按钮。
(function () {
  document.addEventListener('keydown', function (evt) {
    if (evt.key !== 'Escape' && evt.key !== 'Esc') return;
    var modals = document.querySelectorAll('.modal');
    for (var i = 0; i < modals.length; i++) {
      modals[i].remove();
    }
  });
})();


// 项目切换：服务重启后 active 句柄会丢失，用户需要一个「回到某个项目」的入口。
// 走 POST /api/v1/projects/{pid}/switch（写接口，需 Origin + X-CSRF-Token 双提交）。
(function () {
  function csrfToken() {
    var parts = document.cookie.split('; ');
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].indexOf('hpsl_csrf=') === 0) return parts[i].slice('hpsl_csrf='.length);
    }
    return '';
  }
  window.hpslSwitchProject = function (pid, btn) {
    var label = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '切换中…'; }
    function restore() {
      if (btn) { btn.disabled = false; btn.textContent = label || '切换到此项目'; }
    }
    fetch('/api/v1/projects/' + encodeURIComponent(pid) + '/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
      body: '{}'
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.code === '0') { window.location.href = '/unit'; return; }
      restore();
      if (window.hpslToast) { window.hpslToast('切换失败：' + ((d && d.msg) || '未知错误')); }
    }).catch(function () {
      restore();
      if (window.hpslToast) { window.hpslToast('切换失败：网络或服务异常'); }
    });
  };
})();
