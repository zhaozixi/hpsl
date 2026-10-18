"""M20 验收：概念验证时间线（concept.last_verified_at 按日聚合，零 DDL）。

- AC1：GET /stats 渲染『概念验证时间线（近 {{ days }} 天）』区块。
- AC2：GET /api/v1/stats 返回 verify_timeline（近 days 天每日已验证概念数）、verify_total、verify_max。
- AC3：窗口外（> days 天前）的 last_verified_at 不计；随 days 增大计入。
- AC4：零 DDL（count_user_objects == 16）。

纪律：只读聚合既有 concept 表（last_verified_at 列已存在，M2 由 submit_review 写回），read_security；零 DDL，无新错误码。
"""

from datetime import datetime, timedelta, timezone

import pytest

from hpsl import db, projects  # noqa: F401


def _create_and_switch(client, csrf_headers, name="M20项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed(pid):
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    db.write(pid, "concept", {"id": "c1", "name": "概念甲", "verify_status": "已验证", "last_verified_at": today}, "user")
    db.write(pid, "concept", {"id": "c2", "name": "概念乙", "verify_status": "已验证", "last_verified_at": today}, "user")
    db.write(pid, "concept", {"id": "c3", "name": "概念丙", "verify_status": "已验证", "last_verified_at": old}, "user")
    # 未验证概念（无 last_verified_at）不应计入
    db.write(pid, "concept", {"id": "c4", "name": "概念丁", "verify_status": "未验证", "last_verified_at": None}, "user")


def test_verify_timeline_page_and_api(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed(d["id"])
    r = client.get("/stats")
    assert r.status_code == 200
    assert "概念验证时间线（近 30 天）" in r.text

    r30 = client.get("/api/v1/stats?days=30")
    data = r30.json()["data"]
    assert len(data["verify_timeline"]) == 30
    assert data["verify_total"] == 2  # 今天 2 个，40 天前排除
    assert data["verify_max"] == 2


def test_verify_timeline_window_expands(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed(d["id"])
    r90 = client.get("/api/v1/stats?days=90")
    data = r90.json()["data"]
    assert len(data["verify_timeline"]) == 90
    assert data["verify_total"] == 3  # 40 天前那条落入 90 天窗口
    assert data["verify_max"] == 2


def test_zero_ddl_after_verify_timeline(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed(d["id"])
    client.get("/stats?days=90")
    client.get("/api/v1/stats?days=90")
    assert db.count_user_objects(d["id"]) == 16
