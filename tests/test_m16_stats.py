"""M16 验收：学习分析仪表盘 /stats（只读聚合，零 DDL）。

- AC1：GET /stats 渲染仪表盘（概念/卡片分布、复习队列、累计花费、单元数）。
- AC2：GET /api/v1/stats 返回聚合数据，分布计数正确。
- AC3：无激活项目时 /stats 提示「尚未选择项目」。
- AC4：零 DDL（count_user_objects == 16）。

纪律：只读消费既有表，read_security；零 DDL，无新错误码。
"""

import pytest

from hpsl import db, projects  # noqa: F401


def _create_and_switch(client, csrf_headers, name="M16项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed(pid):
    db.write(pid, "concept", {"id": "c1", "name": "概念甲", "verify_status": "已验证"}, "user")
    db.write(pid, "concept", {"id": "c2", "name": "概念乙", "verify_status": "未验证"}, "user")
    db.write(pid, "concept", {"id": "c3", "name": "概念丙", "verify_status": "待验证"}, "user")
    db.write(pid, "card", {"id": "k1", "concept_id": "c1", "my_words": "x", "links": [], "verify_status": "已验证"}, "user")
    db.write(pid, "card", {"id": "k2", "concept_id": "c2", "my_words": "y", "links": [], "verify_status": "未验证"}, "user")
    # 复习：r1 逾期（due 过去）、r2 活跃未逾期
    db.write(pid, "review", {"id": "r1", "concept_id": "c1", "attempt_id": None, "kind": "到期", "due": "2020-01-01T00:00:00+00:00", "result": None}, "user")
    db.write(pid, "review", {"id": "r2", "concept_id": "c2", "attempt_id": None, "kind": "到期", "due": "2099-01-01T00:00:00+00:00", "result": None}, "user")
    db.write(pid, "llm_call", {"id": "l1", "role": "assistant", "provider": "p", "model": "m", "session_id": None, "unit_id": None, "input_hash": "h", "tokens_in": 1, "tokens_out": 1, "cost": 2.5, "ts": "2020-01-01T00:00:00+00:00"}, "user")
    db.write(pid, "unit", {"id": "u1", "title": "单元一", "status": "CLOSED"}, "user")


def test_stats_page_renders(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed(pid)
    r = client.get("/stats")
    assert r.status_code == 200
    for token in ["概念", "卡片", "复习队列", "累计花费", "单元", "$2.50", "¥18.00"]:
        assert token in r.text, f"缺少: {token}"


def test_stats_api_aggregates(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed(pid)
    r = client.get("/api/v1/stats")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    data = r.json()["data"]
    assert data["concept"]["total"] == 3
    assert data["concept"]["dist"] == {"已验证": 1, "未验证": 1, "待验证": 1}
    assert data["card"]["total"] == 2
    assert data["card"]["dist"] == {"已验证": 1, "未验证": 1}
    assert data["review"]["active"] == 2
    assert data["review"]["overdue"] == 1
    assert data["cost_total"] == 2.5
    assert data["unit_count"] == 1


def test_stats_no_active(client):
    r = client.get("/stats")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


def test_zero_ddl_after_stats(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed(pid)
    client.get("/stats")
    client.get("/api/v1/stats")
    assert db.count_user_objects(pid) == 16
