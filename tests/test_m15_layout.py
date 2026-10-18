"""M15 验收：概念图节点拖拽 + 坐标持久化（存每项目 graph_layout.json，零 DDL）。

- AC1：POST /api/v1/graph/layout 持久化节点坐标到项目 graph_layout.json。
- AC2：重新打开 /graph 时节点坐标恢复为持久化值（cx/cy 匹配）。
- AC3：非法条目（不存在的 id / 非数值）被忽略，仅有效条目落盘，不报错。
- AC4：零 DDL（count_user_objects == 16），不新增表/列。

纪律：坐标存文件（与 backup 文件级 I/O 同构），不触 db.write、不新增 16 库对象。
"""

import json

import pytest

from hpsl import db, projects  # noqa: F401
from hpsl import config as cfg


def _create_and_switch(client, csrf_headers, name="M15项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed_concept_edge(pid, *, cid="c1", cid2="c2"):
    db.write(pid, "concept", {"id": cid, "name": "概念甲", "verify_status": "未验证"}, "user")
    db.write(pid, "concept", {"id": cid2, "name": "概念乙", "verify_status": "已验证"}, "user")
    db.write(pid, "concept_edge", {"id": "e1", "src": cid, "dst": cid2, "kind": "prereq"}, "user")


def test_graph_layout_persist_and_restore(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_edge(pid)
    r = client.post(
        "/api/v1/graph/layout", headers=csrf_headers,
        json={"layout": {"c1": {"x": 100.0, "y": 200.0}, "c2": {"x": 300.0, "y": 50.0}}},
    )
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    assert r.json()["data"]["saved"] == 2
    # 磁盘持久化
    lp = cfg.project_dir(pid) / "graph_layout.json"
    assert lp.exists()
    data = json.loads(lp.read_text(encoding="utf-8"))
    assert data["layout"]["c1"]["x"] == 100.0
    # 恢复：重新打开页面，节点坐标应为持久化值
    r2 = client.get("/graph")
    assert r2.status_code == 200
    assert 'cx="100.0"' in r2.text
    assert 'cy="200.0"' in r2.text


def test_graph_layout_ignores_invalid(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_edge(pid)
    r = client.post(
        "/api/v1/graph/layout", headers=csrf_headers,
        json={"layout": {
            "ghost": {"x": 1, "y": 2},          # 不存在的 id → 忽略
            "c1": {"x": "bad", "y": 3},          # 非数值 → 忽略
            "c2": {"x": 10, "y": 20},            # 有效
        }},
    )
    assert r.status_code == 200
    assert r.json()["data"]["saved"] == 1
    lp = cfg.project_dir(pid) / "graph_layout.json"
    data = json.loads(lp.read_text(encoding="utf-8"))
    assert "ghost" not in data["layout"]
    assert "c1" not in data["layout"]
    assert data["layout"]["c2"]["x"] == 10.0


def test_zero_ddl_after_layout(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_edge(pid)
    client.post("/api/v1/graph/layout", headers=csrf_headers, json={"layout": {"c1": {"x": 5, "y": 5}}})
    assert db.count_user_objects(pid) == 16
