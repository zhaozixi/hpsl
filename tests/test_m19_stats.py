"""M19 验收：时间序列区间选择（7/30/90/180 天白名单，零 DDL）。

- AC1：GET /stats 默认渲染近 30 天；?days=7/90/180 改变窗口长度与区间链接高亮。
- AC2：GET /api/v1/stats?days=N 返回 cost_series 长度等于 N（N 在白名单内）。
- AC3：越界或非整数 ?days= 回退默认 30 天（防注入，区间仅白名单）。
- AC4：零 DDL（count_user_objects == 16）。

纪律：只读聚合既有表，read_security；零 DDL，无新错误码。
"""

from datetime import datetime, timedelta, timezone

import pytest

from hpsl import db, projects  # noqa: F401


def _create_and_switch(client, csrf_headers, name="M19项目", goal="目标g"):
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
    for i, (ts, cost) in enumerate([(today, 1.0), (today, 2.0), (old, 9.9)]):
        db.write(pid, "llm_call", {"id": f"l{i}", "role": "assistant", "provider": "p", "model": "m",
                                    "session_id": None, "unit_id": None, "input_hash": f"h{i}",
                                    "tokens_in": 1, "tokens_out": 1, "cost": cost, "ts": ts}, "user")


def test_stats_default_and_range_links(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed(d["id"])
    # 默认 30 天
    r = client.get("/stats")
    assert r.status_code == 200
    assert "花费趋势（近 30 天）" in r.text
    assert "range-links" in r.text
    # 选 90 天：标题与高亮同步
    r90 = client.get("/stats?days=90")
    assert r90.status_code == 200
    assert "花费趋势（近 90 天）" in r90.text
    assert 'class="active"' in r90.text  # 当前区间高亮


def test_stats_api_range_length(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed(d["id"])
    for n in (7, 30, 90, 180):
        r = client.get(f"/api/v1/stats?days={n}")
        assert r.status_code == 200
        assert r.json()["code"] == "0"
        assert len(r.json()["data"]["cost_series"]) == n


def test_stats_range_fallback_to_default(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed(d["id"])
    # 越界值回退 30
    r = client.get("/api/v1/stats?days=45")
    assert len(r.json()["data"]["cost_series"]) == 30
    # 非整数回退 30
    r2 = client.get("/api/v1/stats?days=abc")
    assert len(r2.json()["data"]["cost_series"]) == 30
    # 缺省回退 30
    r3 = client.get("/api/v1/stats")
    assert len(r3.json()["data"]["cost_series"]) == 30


def test_zero_ddl_after_range_selection(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed(d["id"])
    client.get("/stats?days=180")
    client.get("/api/v1/stats?days=180")
    assert db.count_user_objects(d["id"]) == 16
