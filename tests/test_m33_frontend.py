"""E-03 工程债收口：前端脚本的**可执行**校验（不再只是字符串断言）。

背景：此前对 `templates/*.html` 的前端断言只能检查"某个字符串在不在文件里"，
一旦内联 JS 出现语法错误（少个括号、模板变量拼错），测试仍然全绿，只有真人打开
浏览器才会发现。本文件把模板内的 `<script>` 抽出来，交给 Node 做真正的语法检查。

判定口径：
- 有 Node 时：`node --check` 必须通过（语法错误 → 测试失败）。
- 无 Node 时：相关用例 `skip`（不伪装成通过）。

同时保留一组"结构不变量"断言（这些是 M27/M30 的修复成果，不允许回退）：
- 不出现 `innerHTML` 赋值（改用 DOM API 建节点，杜绝注入）；
- 不出现内联 `onclick=`（改用 data-* + 事件委托）；
- HTMX 走本地文件而非 CDN。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TPL = Path(__file__).resolve().parent.parent / "hpsl" / "templates"
STATIC = Path(__file__).resolve().parent.parent / "hpsl" / "static"

# 允许通过环境变量指定解释器（CI / 无 PATH 的机器）
_NODE_CANDIDATES = [
    os.environ.get("HPSL_NODE"),
    shutil.which("node"),
    r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe",
]


def _node() -> str | None:
    for p in _NODE_CANDIDATES:
        if p and Path(p).exists():
            return p
    return None


NODE = _node()
requires_node = pytest.mark.skipif(NODE is None, reason="未找到 node 可执行文件，跳过我 JS 语法校验")


def _strip_jinja(js: str) -> str:
    """把 Jinja 表达式/语句替换成合法 JS 占位，使内联脚本可被语法解析。

    - `{{ expr }}` → null（表达式位置）
    - `{% stmt %}` → 空（控制流语句）
    - `{# comment #}` → 空
    仅用于语法检查，不参与运行时语义。
    """
    js = re.sub(r"\{#.*?#\}", "", js, flags=re.S)
    js = re.sub(r"\{\{.*?\}\}", "null", js, flags=re.S)
    js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)
    return js


def _extract_scripts(html: str) -> str:
    """返回模板中所有**内联** `<script>` 的拼接内容（忽略带 src 的外链标签）。"""
    blocks = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, flags=re.S):
        blocks.append(_strip_jinja(m.group(1)))
    return "\n;\n".join(blocks)


def _check_syntax(tmp_path: Path, name: str, code: str) -> None:
    """交给 node --check；语法错误时把 stderr 一并抛出，便于定位。"""
    f = tmp_path / name
    f.write_text(code, encoding="utf-8")
    proc = subprocess.run(
        [NODE, "--check", str(f)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"{name} 语法检查失败：\n{proc.stderr[:2000]}"


# ---------------------------------------------------------------------------
# 1) 内联脚本语法合法（真正执行 node --check）
# ---------------------------------------------------------------------------
@requires_node
@pytest.mark.parametrize(
    "tpl",
    ["graph.html", "unit_detail.html", "settings.html", "stats.html", "weekly.html"],
)
def test_inline_scripts_are_syntactically_valid(tmp_path, tpl):
    html = (TPL / tpl).read_text(encoding="utf-8")
    code = _extract_scripts(html)
    if not code.strip():
        pytest.skip(f"{tpl} 无内联脚本")
    _check_syntax(tmp_path, f"{tpl}.js", code)


@requires_node
def test_static_app_js_syntax(tmp_path):
    _check_syntax(tmp_path, "app.js", (STATIC / "app.js").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 2) 结构不变量：防注入 / 事件委托 / 本地 HTMX
# ---------------------------------------------------------------------------
def test_no_innerhtml_assignment():
    """M27 起 graph 用 DOM API 建节点；任何回退到 innerHTML 拼接都要拦下。"""
    offenders = []
    for p in TPL.glob("*.html"):
        text = p.read_text(encoding="utf-8")
        if re.search(r"\.innerHTML\s*(\+=|=)", text):
            offenders.append(p.name)
    assert offenders == [], f"这些模板又用 innerHTML 赋值了：{offenders}"


def test_graph_uses_text_nodes_and_delegation():
    """M27/M30 的修复成果：节点文本用 textContent 写入，交互靠 data-id + 事件委托。

    服务端已渲出 SVG 骨架，前端只做属性/文本更新，不拼 HTML 字符串。
    """
    html = (TPL / "graph.html").read_text(encoding="utf-8")
    assert "textContent" in html, "文本应通过 textContent 写入（防注入）"
    assert "addEventListener" in html, "应有事件监听"
    assert "data-id" in html or "data-src" in html, "节点/边应带 data-* 供委托识别"
    assert ".innerHTML" not in html, "graph 不得回退到 innerHTML"


def test_htmx_is_served_locally():
    base = (TPL / "base.html").read_text(encoding="utf-8")
    assert "/static/htmx.min.js" in base
    assert "cdn." not in base and "unpkg" not in base and "jsdelivr" not in base
    assert (STATIC / "htmx.min.js").stat().st_size > 40_000


def test_graph_scripts_expose_expected_functions():
    """布局/持久化函数必须还在（node --check 只保证语法，这里保证功能入口不被误删）。"""
    html = (TPL / "graph.html").read_text(encoding="utf-8")
    for fn in ("autoLayout", "hierarchicalLayout", "persistLayout"):
        assert f"function {fn}" in html or f"const {fn}" in html, f"缺少函数 {fn}"


# ---------------------------------------------------------------------------
# 3) JSON 注入面：模板注入给 JS 的数据必须是 `|tojson`
# ---------------------------------------------------------------------------
def test_js_data_injection_uses_tojson():
    """直接 `{{ nodes }}` 塞进 JS 会被 Jinja 转义成 HTML 实体，且可能破坏脚本。

    graph.html 的三份注入必须走 `tojson`（产出合法 JS 字面量）。
    """
    html = (TPL / "graph.html").read_text(encoding="utf-8")
    for var in ("nodes", "edge_views", "concepts"):
        assert re.search(rf"{var}\s*\|\s*tojson", html), f"{var} 未使用 tojson 注入"


@requires_node
def test_graph_injected_payload_is_valid_json(tmp_path):
    """用一组真实形状的数据渲染 graph.html，再校验注入的 JSON 能被解析。

    这补上"模板 → 浏览器"之间最容易被忽视的一环：注入的 JSON 是否真的合法。
    """
    from hpsl.routes import graph as graph_routes

    nodes = [{"id": "c1", "name": "概念一", "x": 10.0, "y": 20.0, "verify_status": "已验证"}]
    edge_views = [
        {"src": "c1", "dst": "c1", "kind": "relate", "x1": 0, "y1": 0, "x2": 10, "y2": 10}
    ]
    ctx = {
        "project": {"name": "前端测试", "goal": "g"},
        "nodes": nodes,
        "edges": edge_views,
        "edge_views": edge_views,
        "concepts": [{"id": "c1", "name": "概念一"}],
        "svg": {"w": 720, "h": 520},
    }
    html = graph_routes._TEMPLATES.get_template("graph.html").render(**ctx)
    m = re.search(r"const NODES\s*=\s*(\[.*?\]);", html, flags=re.S)
    assert m, "未找到 NODES 注入"
    parsed = json.loads(m.group(1))
    assert parsed[0]["name"] == "概念一"
