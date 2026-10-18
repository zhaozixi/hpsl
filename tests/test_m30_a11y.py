"""M30 可访问性收尾验收（P2-11 / P2-12，纯视图层：零 DDL、不改 .py）。

- AC1：所有页面（base.html）带「跳到主要内容」skip-link，且 <main id="main"> 存在。
- AC2：导航按当前路径加 aria-current="page"（/unit 命中「单元」且仅一处）。
- AC3：行内术语宏 tip 可键盘聚焦（tabindex="0" + role="note"），供 app.js focus 事件复用。
- AC4：配额弹层是标准对话框（role="dialog" / aria-modal / aria-labelledby）且可关闭。
- AC5：预算表表头 <th> 带 scope="col"。
- AC6：概念图节点改用 data-id + 事件委托，不再有内联 onclick="selectNode(...)"。
"""

import re

from jinja2 import Environment, FileSystemLoader

from hpsl import db


def _create_and_switch(client, csrf_headers, name="M30A11Y项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed_concept(pid, *, cid="c1", cid2="c2"):
    db.write(pid, "concept", {"id": cid, "name": "概念甲", "verify_status": "未验证"}, "user")
    db.write(pid, "concept", {"id": cid2, "name": "概念乙", "verify_status": "已验证"}, "user")
    db.write(pid, "concept_edge", {"id": "e1", "src": cid, "dst": cid2, "kind": "prereq"}, "user")


def _seed_blocked_session(pid, *, sid="s1", cost_usd=999.0):
    """会话 + 一笔高花费，使 /budget 渲染出带表头的预算表（并触发弹层）。"""
    db.write(
        pid, "session",
        {"id": sid, "unit_id": None, "started_at": db._now_utc(), "ended_at": None, "gaps_confirmed": 0},
        "user",
    )
    db.write(
        pid, "llm_call",
        {
            "id": "lc1", "role": "user", "provider": "x", "model": "m", "session_id": sid,
            "unit_id": None, "input_hash": "h", "tokens_in": 0, "tokens_out": 0,
            "cost": cost_usd, "ts": db._now_utc(),
        },
        "system",
    )


# ---------------------------------------------------------------------------
# AC1：skip-link + main landmark
# ---------------------------------------------------------------------------
def test_pages_have_skip_link_and_main_landmark(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'class="skip-link" href="#main"' in r.text
    assert "跳到主要内容" in r.text
    assert '<main id="main"' in r.text


# ---------------------------------------------------------------------------
# AC2：当前页导航 aria-current
# ---------------------------------------------------------------------------
def test_nav_marks_current_page(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/unit")
    assert r.status_code == 200
    # 「单元」链接被标记为当前页，且全页只有一处 aria-current
    assert re.search(r'<a href="/unit"[^>]*aria-current="page"', r.text)
    assert r.text.count('aria-current="page"') == 1
    # 主题按钮由 aria-pressed 表达按压态（浅色时 false）
    assert 'aria-pressed="false"' in r.text


# ---------------------------------------------------------------------------
# AC3：tip 宏可键盘聚焦
# ---------------------------------------------------------------------------
def test_tip_macro_is_keyboard_focusable():
    env = Environment(loader=FileSystemLoader("hpsl/templates"))
    t = env.from_string("{% from 'macros.html' import tip %}{{ tip('通过率', '解释文字') }}")
    html = t.render()
    assert 'tabindex="0"' in html
    assert 'role="note"' in html
    assert "通过率" in html


# ---------------------------------------------------------------------------
# AC4：配额弹层的对话框语义 + 关闭按钮
# ---------------------------------------------------------------------------
def test_quota_modal_dialog_semantics_and_close(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed_blocked_session(d["id"])
    r = client.get("/budget")
    assert r.status_code == 200
    assert 'role="dialog"' in r.text
    assert 'aria-modal="true"' in r.text
    assert 'aria-labelledby="quota-title-s1"' in r.text
    assert 'id="quota-title-s1"' in r.text
    assert "关闭" in r.text
    assert "this.closest('.modal').remove()" in r.text


# ---------------------------------------------------------------------------
# AC5：预算表表头 scope
# ---------------------------------------------------------------------------
def test_budget_table_headers_have_scope(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed_blocked_session(d["id"])
    r = client.get("/budget")
    assert r.status_code == 200
    assert 'id="budget-table"' in r.text
    for head in ["会话", "累计(USD)", "上限", "状态", "确认次数"]:
        assert f'<th scope="col">{head}</th>' in r.text


# ---------------------------------------------------------------------------
# AC6：概念图节点事件委托（无内联 onclick）
# ---------------------------------------------------------------------------
def test_graph_nodes_use_data_id_and_delegation(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed_concept(d["id"])
    r = client.get("/graph")
    assert r.status_code == 200
    assert 'data-id="c1"' in r.text
    assert 'onclick="selectNode(' not in r.text
    assert "selectNode" in r.text           # 选择逻辑仍在脚本中
    assert "closest('.node')" in r.text     # 事件委托已接入
