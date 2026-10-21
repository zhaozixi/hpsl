// M36/C9：严格 CSP 下模板不再使用 hx-on（eval 型内联）。关闭 HTMX 的 eval 能力，
// 让任何残留的 hx-on / js: 写法直接失效，而不是被静默执行。
if (window.htmx) { window.htmx.config.allowEval = false; }

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


// M36/C9：统一事件委托。严格 CSP 下不再有内联 onclick/onchange/onsubmit/oninput，
// 模板统一用 data-action 标记意图，这里分发到各页已定义的全局函数。
// 页面函数缺失时静默降级（例如局部片段被单独渲染、脚本尚未加载）。
(function () {
  function actionOf(el) {
    return el && el.getAttribute ? el.getAttribute('data-action') : null;
  }
  function actionClosest(target) {
    return target && target.closest ? target.closest('[data-action]') : null;
  }

  document.addEventListener('click', function (evt) {
    var el = actionClosest(evt.target);
    if (!el) return;
    var action = actionOf(el);
    if (action === 'toggle-theme') {
      if (window.hpslToggleTheme) window.hpslToggleTheme();
    } else if (action === 'graph-zoom') {
      var factor = parseFloat(el.getAttribute('data-factor'));
      if (window.zoomBy && isFinite(factor)) window.zoomBy(factor);
    } else if (action === 'graph-fit') {
      if (window.fitView) window.fitView();
    } else if (action === 'graph-auto') {
      if (window.autoLayout) window.autoLayout();
    } else if (action === 'graph-hier') {
      if (window.hierarchicalLayout) window.hierarchicalLayout();
    } else if (action === 'graph-reset') {
      if (window.resetView) window.resetView();
    } else if (action === 'export') {
      var kind = el.getAttribute('data-kind');
      // 模板用 data-kind="projects"（入口名），导出接口的 kind 为单数 project。
      if (kind === 'projects') kind = 'project';
      var out = el.getAttribute('data-out') || ('r-' + kind);
      if (kind && window.doExport) window.doExport(kind, out);
    } else if (action === 'switch-project') {
      var pid = el.getAttribute('data-pid');
      if (pid && window.hpslSwitchProject) window.hpslSwitchProject(pid, el);
    } else if (action === 'wizard-test') {
      if (window.stepTest) window.stepTest();
    } else if (action === 'close-modal') {
      var modal = el.closest('.modal');
      if (modal) modal.remove();
    }
  });

  document.addEventListener('change', function (evt) {
    var el = actionClosest(evt.target);
    if (!el || actionOf(el) !== 'role-call') return;
    // 与原内联 onchange 一致：改角色后重设 hx-post，再让 HTMX 重新处理该表单。
    var form = document.getElementById('role-form');
    if (!form || !window.htmx || !el.value) return;
    form.setAttribute('hx-post', '/api/v1/roles/' + el.value + '/call');
    window.htmx.process(form);
  });

  document.addEventListener('input', function (evt) {
    var el = actionClosest(evt.target);
    if (!el || actionOf(el) !== 'graph-search') return;
    if (window.onSearchInput) window.onSearchInput();
  });

  document.addEventListener('keydown', function (evt) {
    var el = actionClosest(evt.target);
    if (!el || actionOf(el) !== 'graph-search') return;
    if (evt.key === 'Enter' && window.onSearchKey) window.onSearchKey(evt);
  });

  document.addEventListener('submit', function (evt) {
    var form = evt.target;
    if (!form || !form.getAttribute) return;
    var action = form.getAttribute('data-action');
    if (!action) return;
    evt.preventDefault();  // 原内联 onsubmit 均先阻止默认提交
    if (action === 'save-settings' && window.saveSettings) window.saveSettings(evt);
    else if (action === 'wizard-step1' && window.stepCreate) window.stepCreate();
    else if (action === 'wizard-step2' && window.stepSaveKey) window.stepSaveKey();
  });

  // create.html：原 hx-on::after-request 的逻辑迁到外部脚本（严格 CSP 禁止 hx-on）。
  // 仅当项目真的创建成功时重置表单并沿用原有「创建后自动 switch」行为。
  document.addEventListener('htmx:afterRequest', function (evt) {
    var el = (evt.detail && evt.detail.elt) || evt.target;
    if (!el || el.id !== 'create-form') return;
    if (!(evt.detail && evt.detail.successful)) return;
    // 业务错误也返回 200（HTMX 分支固定 200 才能被 swap），故 successful 不足以
    // 判定成败：只认成功片段带回来的 data-project-id。否则重名/校验失败会把
    // 用户已填的名称与目标一并清空（M36 回归 R-01）。
    var box = document.getElementById('result');
    var created = !!(box && box.querySelector('[data-project-id]'));
    if (created && el.reset) el.reset();
    if (typeof window.hpslAfterCreate === 'function') window.hpslAfterCreate();
  });

  // 建单元成功：服务端用 HX-Trigger 发事件（不再塞 <script>，严格 CSP 下内联脚本会被拦）。
  document.body.addEventListener('hpsl:unit-created', function () {
    var f = document.querySelector('form[hx-post="/api/v1/units"]');
    if (!f) return;
    if (f.reset) f.reset();
    var input = f.querySelector('input[name="title"]');
    if (input) input.focus();
  });
})();
