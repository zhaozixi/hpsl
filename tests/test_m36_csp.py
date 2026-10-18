"""M36 / C9：严格 CSP + 内联 JS 收敛回归。

覆盖：
- 每个页面响应带 `Content-Security-Policy`，script-src 为 `'self' 'nonce-…'`，无 unsafe-inline；
  object-src 'none' / base-uri 'self' / frame-ancestors 'none' 等硬化项齐备。
- 所有页面渲染结果不含内联 `on(click|change|submit|input)=`。
- 每个内联 `<script>` 都带本响应 CSP 的 nonce（`<script src=…>` 外链除外）。
- 主题脚本外置：base.html / help.html 引 `/static/theme.js`，函数与防闪烁逻辑在该文件里。
- app.js 设置 `htmx.config.allowEval = false`，且统一委托分发 data-action。
- create.html 不再使用 `hx-on::after-request`，改用 create-form + app.js 监听。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hpsl import db

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "hpsl" / "templates"
STATIC = ROOT / "hpsl" / "static"

# 内联 <script>（排除带 src 的外链标签）
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I)
_EVENT_ATTR = re.compile(r"\son(click|change|submit|input)\s*=", re.I)
_NONCE_IN_CSP = re.compile(r"'nonce-([^']+)'")


def _create_project(client, csrf, name="C9 CSP 项目", switch=True):
    r = client.post(
        "/api/v1/projects",
        headers=csrf,
        json={"name": name, "goal": "验证严格 CSP 与去内联 JS"},
    )
    body = r.json()
    assert body and body.get("code") == "0", r.text
    d = body["data"]
    if switch:
        switched = client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf)
        assert switched.json()["code"] == "0"
        # 单元详情页需要一条单元；直接写库（零 DDL，仅测试数据）
        db.write(d["id"], "unit", {"id": "u1", "title": "单元甲", "status": "INIT"}, "user")
    return d


@pytest.fixture
def page_responses(client, csrf_headers):
    """渲染全部页面（含 base 主题与外链脚本），供多条断言复用。"""
    # 先建非当前项目（创建会自动置为 active），再建并切换到主项目；
    # 这样首页仍有「切换到此项目」按钮可校验。
    other = _create_project(client, csrf_headers, name="C9 非当前项目", switch=False)
    d = _create_project(client, csrf_headers)
    pid = d["id"]
    paths = [
        "/",
        "/projects/new",
        f"/projects/{pid}",
        f"/projects/{other['id']}",
        "/unit",
        "/unit/u1",
        "/review",
        "/cards",
        "/graph",
        "/knowledge",
        "/stats",
        "/budget",
        "/weekly",
        "/settings",
        "/export",
        "/ledger",
        "/backup",
        "/wizard",
    ]
    assert len(paths) >= 13
    out = []
    for path in paths:
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
        out.append((path, r))
    return out


# ---------------------------------------------------------------------------
# 1) CSP 响应头
# ---------------------------------------------------------------------------
def test_csp_header_present_and_strict(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers.get("Content-Security-Policy")
    assert csp, "GET / 缺少 Content-Security-Policy 响应头"
    assert _NONCE_IN_CSP.search(csp), f"CSP 未包含每请求 nonce：{csp}"
    script_src = [
        part.strip() for part in csp.split(";") if part.strip().startswith("script-src")
    ]
    assert script_src, f"CSP 缺少 script-src：{csp}"
    assert "'unsafe-inline'" not in script_src[0], f"script-src 不应允许 unsafe-inline：{script_src[0]}"
    assert "'self'" in script_src[0]
    assert "object-src 'none'" in csp
    assert "default-src 'self'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_every_page_has_csp_with_fresh_nonce(page_responses):
    nonces = set()
    for path, r in page_responses:
        csp = r.headers.get("Content-Security-Policy")
        assert csp, f"{path} 缺少 CSP 响应头"
        m = _NONCE_IN_CSP.search(csp)
        assert m, f"{path} CSP 缺少 nonce"
        nonces.add(m.group(1))
    assert len(nonces) == len(page_responses), "CSP nonce 应每请求独立，不能复用"


# ---------------------------------------------------------------------------
# 2) 去内联 handler / 内联脚本带 nonce
# ---------------------------------------------------------------------------
def test_no_inline_event_handlers_on_pages(page_responses):
    offenders = []
    for path, r in page_responses:
        for m in _EVENT_ATTR.finditer(r.text):
            offenders.append((path, m.group(0)))
    assert offenders == [], f"页面仍有内联事件 handler：{offenders}"


def test_templates_have_no_inline_event_handlers():
    """模板源文件也扫一遍：防止只改了渲染期而漏掉条件分支里的内联写法。"""
    offenders = []
    for p in sorted(TPL.glob("*.html")):
        text = p.read_text(encoding="utf-8")
        for m in _EVENT_ATTR.finditer(text):
            offenders.append((p.name, m.group(0)))
    assert offenders == [], f"这些模板仍用内联事件 handler：{offenders}"


def test_inline_scripts_carry_page_nonce(page_responses):
    checked = 0
    for path, r in page_responses:
        nonce = _NONCE_IN_CSP.search(r.headers["Content-Security-Policy"]).group(1)
        for tag in _INLINE_SCRIPT.findall(r.text):
            assert re.search(rf'\bnonce="{re.escape(nonce)}"', tag), (
                f"{path} 的内联脚本缺少本响应 nonce：{tag}"
            )
            checked += 1
    # create / export / graph / settings / wizard 五处内联脚本至少各命中一次
    assert checked >= 5, f"内联脚本带 nonce 的校验只命中 {checked} 处"


# ---------------------------------------------------------------------------
# 3) 主题脚本外置
# ---------------------------------------------------------------------------
def test_theme_script_externalized(client):
    r = client.get("/")
    assert r.status_code == 200
    assert '<script src="/static/theme.js"></script>' in r.text
    assert "hpslToggleTheme" not in r.text, "主题函数不应再内联在页面里"
    # 防闪烁：外链脚本必须出现在 <body> 之前（同步加载）
    assert r.text.index('/static/theme.js') < r.text.index('<body>')

    js = client.get("/static/theme.js")
    assert js.status_code == 200
    assert "function hpslToggleTheme" in js.text
    assert "function hpslSyncThemeLabel" in js.text
    assert "hpsl-theme" in js.text
    assert "localStorage.getItem('hpsl-theme')" in js.text


def test_help_page_uses_external_theme_script(client):
    r = client.get("/static/help.html")
    assert r.status_code == 200
    assert '<script src="/static/theme.js"></script>' in r.text
    assert not _INLINE_SCRIPT.search(r.text), "help.html 不应再有内联 <script>"
    assert _NONCE_IN_CSP.search(r.headers.get("Content-Security-Policy", "")), (
        "help.html 也应带 CSP nonce 头"
    )


# ---------------------------------------------------------------------------
# 4) app.js：关闭 eval + 统一 data-action 委托
# ---------------------------------------------------------------------------
def test_app_js_disables_htmx_eval_and_delegates():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "allowEval = false" in js, "app.js 必须关闭 HTMX eval（严格 CSP）"
    for action in (
        "toggle-theme",
        "graph-zoom",
        "graph-fit",
        "graph-auto",
        "graph-hier",
        "graph-reset",
        "graph-search",
        "export",
        "switch-project",
        "save-settings",
        "role-call",
        "wizard-step1",
        "wizard-step2",
        "wizard-test",
        "close-modal",
    ):
        assert action in js, f"app.js 缺少 data-action 分发：{action}"
    assert "htmx:afterRequest" in js, "app.js 应监听 htmx:afterRequest 处理创建后逻辑"


# ---------------------------------------------------------------------------
# 5) create.html 去 hx-on；data-action 契约
# ---------------------------------------------------------------------------
def test_create_page_has_no_hx_on(page_responses):
    text = dict(page_responses)["/projects/new"].text
    assert "hx-on" not in text, "create.html 不应再使用 hx-on"
    assert 'id="create-form"' in text
    assert 'hpslAfterCreate' in text, "创建后自动切换的入口函数仍应由页面提供"


def test_data_action_contract(page_responses):
    by_path = {path: r.text for path, r in page_responses}

    graph = by_path["/graph"]
    for token in (
        'data-action="graph-zoom"',
        'data-factor="1.2"',
        'data-factor="0.8333"',
        'data-action="graph-fit"',
        'data-action="graph-auto"',
        'data-action="graph-hier"',
        'data-action="graph-reset"',
        'data-action="graph-search"',
    ):
        assert token in graph, f"/graph 缺少 {token}"

    export = by_path["/export"]
    for kind in ("projects", "units", "ledger", "concepts", "conversations"):
        assert f'data-action="export" data-kind="{kind}"' in export, f"/export 缺少 kind={kind}"
    for out in ("r-project", "r-units", "r-ledger", "r-concepts", "r-conversations"):
        assert f'data-out="{out}"' in export, f"/export 缺少输出锚点 {out}"

    switch_btn = re.search(
        r'data-action="switch-project" data-pid="([0-9a-f]+)"', by_path["/"]
    )
    assert switch_btn, "/ 缺少项目切换委托（或缺少 data-pid）"
    assert 'data-action="save-settings"' in by_path["/settings"]
    assert 'data-action="role-call"' in by_path["/unit/u1"]

    wizard = by_path["/wizard"]
    for token in (
        'data-action="wizard-step1"',
        'data-action="wizard-step2"',
        'data-action="wizard-test"',
    ):
        assert token in wizard, f"/wizard 缺少 {token}"

    # 预算弹层是片段，不在常规页面里：直接校验模板源
    quota = (TPL / "_quota_modal.html").read_text(encoding="utf-8")
    assert quota.count('data-action="close-modal"') == 2, "预算弹层两个关闭按钮都应改为委托"
