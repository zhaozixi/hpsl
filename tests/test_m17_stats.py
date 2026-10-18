"""M17 验收：学习分析增强（时间序列趋势 + 单元维度细分，零 DDL）。

- AC1：GET /stats 渲染『花费趋势（近 30 天）』与『单元维度细分』区块。
- AC2：GET /api/v1/stats 返回 cost_series（近 30 天，每日花费）、cost_max、unit_breakdown（status/difficulty/produced/degraded）。
- AC3：零 DDL（count_user_objects == 16）。

纪律：只读聚合既有表，read_security；零 DDL，无新错误码。
"""

from datetime import datetime, timedelta, timezone

import pytest

from hpsl import db, projects  # noqa: F401


def _create_and_switch(client, csrf_headers, name="M17项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed(pid):
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    # 花费：今天两笔（应计入近 30 天）、60 天前一笔（应忽略）
    for i, (ts, cost) in enumerate([(today, 1.0), (today, 2.0), (old, 9.9)]):
        db.write(pid, "llm_call", {"id": f"l{i}", "role": "assistant", "provider": "p", "model": "m",
                                    "session_id": None, "unit_id": None, "input_hash": f"h{i}",
                                    "tokens_in": 1, "tokens_out": 1, "cost": cost, "ts": ts}, "user")
    # 单元：不同状态/难度/产出/降级
    db.write(pid, "unit", {"id": "u1", "title": "单元一", "status": "CLOSED", "difficulty_band": "易", "production": "x", "degrade_count": 0}, "user")
    db.write(pid, "unit", {"id": "u2", "title": "单元二", "status": "INIT", "difficulty_band": "难", "production": None, "degrade_count": 2}, "user")
    db.write(pid, "unit", {"id": "u3", "title": "单元三", "status": "CLOSED", "difficulty_band": None, "production": "y", "degrade_count": 0}, "user")


def test_stats_timeseries_and_unit_breakdown(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed(pid)
    r = client.get("/stats")
    assert r.status_code == 200
    assert "花费趋势（近 30 天）" in r.text
    assert "单元维度细分" in r.text
    assert "按状态" in r.text
    assert "按难度带" in r.text
    # 近 30 天花费应排除 60 天前的 9.9
    assert "9.9000" not in r.text


def test_stats_api_timeseries(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed(pid)
    r = client.get("/api/v1/stats")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    data = r.json()["data"]
    assert len(data["cost_series"]) == 30
    assert data["cost_max"] == 3.0  # 今天 1.0+2.0，60 天前忽略
    # 单元细分
    ub = data["unit_breakdown"]
    assert ub["total"] == 3
    assert ub["status"].get("CLOSED") == 2
    assert ub["status"].get("INIT") == 1
    assert ub["produced"] == 2
    assert ub["degraded"] == 1
    assert ub["difficulty"].get("易") == 1
    assert ub["difficulty"].get("难") == 1
    assert ub["difficulty"].get("未分级") == 1


def test_zero_ddl_after_stats_enhancement(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed(pid)
    client.get("/stats")
    client.get("/api/v1/stats")
    assert db.count_user_objects(pid) == 16
