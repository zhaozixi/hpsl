"""M10B 第六批验收（超限确认弹层 A080001）：`/budget` 页 + 状态 REST + 确认（消费 M8 cost）。

- AC1：GET /budget 渲染（无激活提示 / 会话预算状态表 / 空态）。
- AC2：GET /api/v1/budget/status?session_id=X → 200；返回估算（status/cumulative_usd/budget_cap_usd/confirmations）。
- AC3：超限会话在 /budget 渲染 A080001 弹层（文案 + 确认按钮）。
- AC4：POST /api/v1/budget/confirm（session_id）→ 放行（blocked→warn），写审计；htmx 返回收起片段。
- AC5：错误路径（缺 session_id → C400001；A080001 文案一致）。
- AC6：零 DDL（count_user_objects == 16 仍成立）。
"""

import pytest

from hpsl import db, projects
from hpsl.engine import cost


def _create_and_switch(client, csrf_headers, name="M10B6项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed_blocked_session(pid, *, sid="s1", cost_usd=999.0):
    """插入一条会话 + 一笔高花费 llm_call，使当月累计 >= 预算上限 → blocked。"""
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
# AC1：页渲染
# ---------------------------------------------------------------------------
def test_budget_page_no_active(client):
    r = client.get("/budget")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


def test_budget_page_lists_sessions(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_blocked_session(pid)
    r = client.get("/budget")
    assert r.status_code == 200
    assert "会话预算状态（1）" in r.text
    assert "s1" in r.text
    assert "blocked" in r.text


# ---------------------------------------------------------------------------
# AC2：状态 REST
# ---------------------------------------------------------------------------
def test_budget_status_api_blocked(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_blocked_session(pid)
    r = client.get("/api/v1/budget/status?session_id=s1")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    data = r.json()["data"]
    assert data["status"] == "blocked"
    assert data["cumulative_usd"] >= data["budget_cap_usd"]


def test_budget_status_api_missing_session_id_c400001(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/api/v1/budget/status")
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# AC3：超限弹层（A080001 文案）
# ---------------------------------------------------------------------------
def test_budget_page_shows_quota_modal(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_blocked_session(pid)
    r = client.get("/budget")
    assert r.status_code == 200
    assert "预算超限待确认" in r.text
    assert "这次学习快到本月花费上限了，确认后继续" in r.text  # A080001 user_msg 一致
    assert 'id="quota-modal-s1"' in r.text
    assert "确认继续" in r.text


# ---------------------------------------------------------------------------
# AC4：确认放行
# ---------------------------------------------------------------------------
def test_budget_confirm_unblocks(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_blocked_session(pid)
    # 确认前应 blocked
    assert cost.budget_status("s1") == "blocked"
    r = client.post(
        "/api/v1/budget/confirm",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json={"session_id": "s1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "0"
    # 确认后 blocked→warn（状态机 §3.2.M8.5）
    assert r.json()["data"]["status"] == "warn"
    assert r.json()["data"]["confirmations"] >= 1
    # 引擎侧状态也随之翻转
    assert cost.budget_status("s1") == "warn"


def test_budget_confirm_htmx_returns_fragment(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_blocked_session(pid)
    r = client.post(
        "/api/v1/budget/confirm",
        headers={**csrf_headers, "HX-Request": "true", "Content-Type": "application/json"},
        json={"session_id": "s1"},
    )
    assert r.status_code == 200
    assert "已确认继续" in r.text
    assert 'id="quota-modal-s1"' in r.text


def test_budget_confirm_missing_session_id_c400001(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.post(
        "/api/v1/budget/confirm",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json={},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# AC6：零 DDL 守护
# ---------------------------------------------------------------------------
def test_zero_ddl_after_quota_features(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_blocked_session(pid)
    client.get("/api/v1/budget/status?session_id=s1")
    client.post(
        "/api/v1/budget/confirm",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json={"session_id": "s1"},
    )
    assert db.count_user_objects(pid) == 16
