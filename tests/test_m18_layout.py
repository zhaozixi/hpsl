"""M18 验收：概念图自动布局算法（客户端力导向，复用 M15 持久化，零 DDL）。

- AC1：GET /graph 渲染「自动布局」按钮与 autoLayout 函数（前端力导向入口）。
- AC2：自动布局的输出经 M15 的 /api/v1/graph/layout 持久化；多节点整组坐标落盘并恢复。
- AC3：零 DDL（count_user_objects == 16）。

纪律：布局算前端 JS（力导向），坐标存文件（graph_layout.json），不新增表/列、不触 db.write。
"""

import json

import pytest

from hpsl import db, projects  # noqa: F401
from hpsl import config as cfg


def _create_and_switch(client, csrf_headers, name="M18项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed_many(pid, n=10):
    for i in range(1, n + 1):
        db.write(pid, "concept", {"id": f"c{i}", "name": f"概念{i}", "verify_status": "未验证"}, "user")
    for i in range(1, n):
        db.write(pid, "concept_edge", {"id": f"e{i}", "src": f"c{i}", "dst": f"c{i+1}", "kind": "prereq"}, "user")


def test_graph_auto_layout_control_present(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_many(pid)
    r = client.get("/graph")
    assert r.status_code == 200
    assert "自动布局" in r.text
    assert "autoLayout" in r.text


def test_auto_layout_persists_full_set(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_many(pid, n=10)
    layout = {f"c{i}": {"x": i * 10.0, "y": i * 5.0} for i in range(1, 11)}
    r = client.post("/api/v1/graph/layout", headers=csrf_headers, json={"layout": layout})
    assert r.status_code == 200
    assert r.json()["data"]["saved"] == 10
    lp = cfg.project_dir(pid) / "graph_layout.json"
    assert lp.exists()
    data = json.loads(lp.read_text(encoding="utf-8"))
    assert data["layout"]["c10"]["x"] == 100.0
    # 恢复：重新打开页面，节点坐标应为持久化值
    r2 = client.get("/graph")
    assert 'cx="10.0"' in r2.text
    assert 'cx="100.0"' in r2.text


def test_zero_ddl_after_auto_layout(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_many(pid, n=10)
    layout = {f"c{i}": {"x": i * 10.0, "y": i * 5.0} for i in range(1, 11)}
    client.post("/api/v1/graph/layout", headers=csrf_headers, json={"layout": layout})
    assert db.count_user_objects(pid) == 16
