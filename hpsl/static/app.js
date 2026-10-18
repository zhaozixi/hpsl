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
