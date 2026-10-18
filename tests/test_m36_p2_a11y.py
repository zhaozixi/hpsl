"""M36 / P2（REF-3 C8/C10/C11）回归：可访问性、对比度、XSS 转义、help 同步。

覆盖：
- C8：main#main tabindex、表头 scope、stats SVG role/aria-label、异步结果区 aria-live、
      graph 节点键盘可操作（tabindex/role/aria-label + Enter/Space 委托）。
- C8：白字徽标对比度：琥珀 #f59e0b → #b45309、蓝 #3b82f6 → #1d4ed8（≥4.5:1）。
- C10：review question / arbitrate cid 拼入 HTML 前转义。
- C11：help.html 独立壳补 skip-link、main#main、未定义 token 修正、返回应用入口。

纪律：纯视图层；零 DDL；不新增错误码。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from hpsl import db
from hpsl.engine.review_queue import enqueue_reviews, _add_days


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _project(client, csrf, name="M36P2项目", goal="目标g"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": goal})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf)
    return d


def _htmx(csrf):
    return {**csrf, "HX-Request": "true", "Content-Type": "application/x-www-form-urlencoded"}


def _unit_and_gaps(pid):
    now = db._now_utc()
    db.write(pid, "unit", {"id": "u1", "title": "单元甲", "status": "INIT"}, "user")
    for gid, level in (("g2", 2), ("g1", 1)):
        db.write(
            pid,
            "gap",
            {
                "id": gid,
                "unit_id": "u1",
                "session_id": None,
                "level": level,
                "text": f"缺口{gid}",
                "status": "待补",
                "context": None,
                "created_at": now,
                "updated_at": now,
            },
            "user",
        )


def _due_review(pid):
    now = db._now_utc()
    db.write(pid, "concept", {"id": "c1", "name": "概念甲", "verify_status": "未验证"}, "user")
    enqueue_reviews(pid, ["c1"], _add_days(now, -10))
    return db.query(pid, "SELECT id FROM review WHERE concept_id='c1'")[0]["id"]


# ---------------------------------------------------------------------------
# C8：landmark / 表格 / SVG / aria-live
# ---------------------------------------------------------------------------
def test_base_main_has_tabindex(client):
    r = client.get("/")
    assert r.status_code == 200
    assert re.search(r'<main id="main"[^>]*tabindex="-1"', r.text)


def test_weekly_tables_have_scoped_headers(client, csrf_headers):
    d = _project(client, csrf_headers)
    _unit_and_gaps(d["id"])
    r = client.get("/weekly")
    assert r.status_code == 200
    assert 'id="weekly-gap-table"' in r.text
    assert 'id="weekly-backlog-table"' in r.text
    for head in ["单元", "级别", "缺口", "当前状态", "前置", "裁决", "下周再看"]:
        assert f'<th scope="col">{head}</th>' in r.text


def test_project_detail_table_has_scoped_headers(client, csrf_headers):
    d = _project(client, csrf_headers)
    r = client.get(f"/projects/{d['id']}")
    assert r.status_code == 200
    for head in ["项", "值", "含义"]:
        assert f'<th scope="col">{head}</th>' in r.text


def test_stats_svgs_have_role_and_descriptive_label(client, csrf_headers):
    d = _project(client, csrf_headers)
    pid = d["id"]
    now = db._now_utc()
    db.write(
        pid, "session",
        {"id": "s1", "unit_id": None, "started_at": now, "ended_at": None, "gaps_confirmed": 0},
        "user",
    )
    db.write(
        pid, "llm_call",
        {
            "id": "lc1", "role": "user", "provider": "x", "model": "m", "session_id": "s1",
            "unit_id": None, "input_hash": "h", "tokens_in": 0, "tokens_out": 0,
            "cost": 1.0, "ts": now,
        },
        "system",
    )
    db.write(
        pid, "concept",
        {"id": "c1", "name": "概念甲", "verify_status": "已验证", "last_verified_at": now},
        "user",
    )
    r = client.get("/stats?days=7")
    assert r.status_code == 200
    # 两个图表都声明为图片，并带能读出内容的 aria-label
    assert r.text.count('role="img"') >= 2
    assert 'aria-label="近 7 天每日 LLM 花费柱状图' in r.text
    assert 'aria-label="近 7 天每日完成复测验证的概念数时间线' in r.text
    assert "单日峰值" in r.text


def test_async_result_regions_have_aria_live(client, csrf_headers):
    d = _project(client, csrf_headers)
    rid = _due_review(d["id"])

    unit = client.get("/unit")
    assert unit.status_code == 200
    assert '<div id="create-result" aria-live="polite">' in unit.text

    knowledge = client.get("/knowledge")
    assert knowledge.status_code == 200
    assert '<div id="index-result" aria-live="polite">' in knowledge.text

    review = client.get("/review")
    assert review.status_code == 200
    assert 'id="review-list" aria-live="polite"' in review.text
    assert f'id="panel-{rid}" aria-live="polite"' in review.text


# ---------------------------------------------------------------------------
# C8：graph 键盘可操作 + 对比度
# ---------------------------------------------------------------------------
def test_graph_nodes_are_keyboard_operable(client, csrf_headers):
    d = _project(client, csrf_headers)
    pid = d["id"]
    db.write(pid, "concept", {"id": "c1", "name": "概念甲", "verify_status": "未验证"}, "user")
    r = client.get("/graph")
    assert r.status_code == 200
    node = re.search(r'<g class="node"[^>]*>', r.text)
    assert node, "缺少节点 <g>"
    tag = node.group(0)
    assert 'tabindex="0"' in tag
    assert 'role="button"' in tag
    assert 'aria-label="概念：概念甲' in tag
    # Enter / Space 复用既有事件委托（不引入内联 onclick）
    assert "vp.addEventListener('keydown'" in r.text
    assert "e.key !== 'Enter'" in r.text
    assert "e.key !== ' '" in r.text
    assert 'onclick="selectNode(' not in r.text
    assert "closest('.node')" in r.text


def test_badge_contrast_tokens_darkened(client, csrf_headers):
    css = client.get("/static/app.css")
    assert css.status_code == 200
    # 旧的低对比色不再用于白字徽标底
    assert "#f59e0b" not in css.text
    assert "#3b82f6" not in css.text
    assert "#b45309" in css.text
    assert "#1d4ed8" in css.text

    _project(client, csrf_headers)
    graph = client.get("/graph")
    assert graph.status_code == 200
    assert "#f59e0b" not in graph.text
    assert "#b45309" in graph.text  # 图例色块仍存在，仅换新值


# ---------------------------------------------------------------------------
# C10：XSS 转义（question / cid）
# ---------------------------------------------------------------------------
def test_review_question_is_escaped(client, csrf_headers):
    d = _project(client, csrf_headers)
    rid = _due_review(d["id"])
    payload = "<img src=x onerror=alert(1)>"

    r = client.post(
        f"/api/v1/reviews/{rid}/start",
        headers=_htmx(csrf_headers),
        data={"question": payload},
    )
    assert r.status_code == 200, r.text
    assert "&lt;img" in r.text
    assert "<img" not in r.text
    assert f'id="panel-{rid}"' in r.text


def test_arbitrate_cid_is_escaped(client, csrf_headers, monkeypatch):
    """arbitrate 成功但行已不存在时，cid 会拼进 HTML：必须转义。"""
    from hpsl.routes import unit as unit_routes

    _project(client, csrf_headers)
    monkeypatch.setattr(unit_routes.cc, "arbitrate", lambda *a, **k: None)

    payload = "<img src=x onerror=alert(1)>"
    cid = quote(payload, safe="")
    r = client.post(
        f"/api/v1/units/u1/claim-check/{cid}/arbitrate",
        headers=_htmx(csrf_headers),
        data={"ruling": "采纳"},
    )
    assert r.status_code == 200, r.text
    assert "&lt;img" in r.text
    assert "<img" not in r.text
    assert "不存在" in r.text


# ---------------------------------------------------------------------------
# C11：help.html 独立壳同步
# ---------------------------------------------------------------------------
def test_help_page_has_skip_link_main_and_fixed_spacing(client):
    r = client.get("/static/help.html")
    assert r.status_code == 200
    assert 'class="skip-link" href="#main"' in r.text
    assert "跳到主要内容" in r.text
    assert '<main id="main"' in r.text
    assert "var(--sp-10)" not in r.text  # 未定义 token 已修正
    assert "var(--sp-8)" in r.text
    assert '<a href="/">返回应用</a>' in r.text


def test_help_glossary_headers_scoped(client):
    r = client.get("/static/help.html")
    assert r.status_code == 200
    assert '<table class="gloss">' in r.text
    assert '<th scope="col">界面上的词</th>' in r.text
    assert '<th scope="col">意思（大白话）</th>' in r.text
    assert "<thead>" in r.text
