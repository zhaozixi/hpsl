"""M21 验收：概念图分层布局（前端最长路径分层，复用 M15 持久化，零 DDL）。

- AC1：GET /graph 渲染「分层布局」按钮与 hierarchicalLayout 函数（前端分层入口）。
- AC2：分层布局的输出经 M15 的 /api/v1/graph/layout 持久化；分层坐标整组落盘并恢复（顶层在上、底层在下）。
- AC3：零 DDL（count_user_objects == 16）。

纪律：分层算法前端 JS（最长路径分层，含环落层 0）；坐标存文件（graph_layout.json），不新增表/列、不触 db.write。
（算法本身为前端 JS，TestClient 无法执行；本测试通过「分层坐标经 M15 端点落盘→重渲染顺序正确」验证其驱动的全链路，与 M18 验证口径一致；算法正确性另经代码评审。）
"""

from collections import deque

import json

import pytest

from hpsl import db, projects  # noqa: F401
from hpsl import config as cfg


def _create_and_switch(client, csrf_headers, name="M21项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed_dag(pid, *, ids=("c1", "c2", "c3", "c4"), edges=(("c1", "c2"), ("c2", "c3"), ("c1", "c4"))):
    for cid in ids:
        db.write(pid, "concept", {"id": cid, "name": f"概念{cid[1:]}", "verify_status": "未验证"}, "user")
    for i, (s, d) in enumerate(edges):
        db.write(pid, "concept_edge", {"id": f"e{i}", "src": s, "dst": d, "kind": "prereq"}, "user")


def _longest_path_layers(ids, edges):
    adj = {n: [] for n in ids}
    indeg = {n: 0 for n in ids}
    for s, d in edges:
        adj[s].append(d)
        indeg[d] += 1
    layer = {n: 0 for n in ids}
    indeg2 = dict(indeg)
    q = deque([n for n in ids if indeg[n] == 0])
    while q:
        u = q.popleft()
        for v in adj[u]:
            if layer[v] < layer[u] + 1:
                layer[v] = layer[u] + 1
            indeg2[v] -= 1
            if indeg2[v] == 0:
                q.append(v)
    return layer


def test_graph_hier_layout_control_present(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    _seed_dag(d["id"])
    r = client.get("/graph")
    assert r.status_code == 200
    assert "分层布局" in r.text
    assert "hierarchicalLayout" in r.text


def test_hier_layout_persists_and_restores(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    ids = ("c1", "c2", "c3", "c4")
    edges = (("c1", "c2"), ("c2", "c3"), ("c1", "c4"))
    _seed_dag(pid, ids=ids, edges=edges)
    # 复刻最长路径分层：根 c1=0，c2/c4=1，c3=2
    layer = _longest_path_layers(ids, edges)
    assert layer["c1"] == 0 and layer["c2"] == 1 and layer["c3"] == 2 and layer["c4"] == 1
    layout = {n: {"x": 400.0, "y": layer[n] * 100.0} for n in ids}
    r = client.post("/api/v1/graph/layout", headers=csrf_headers, json={"layout": layout})
    assert r.status_code == 200
    assert r.json()["data"]["saved"] == 4
    lp = cfg.project_dir(pid) / "graph_layout.json"
    assert lp.exists()
    # 恢复：重渲染后顶层(c1, layer0)在上、底层(c3, layer2)在下
    r2 = client.get("/graph")
    assert 'id="node-c1"' in r2.text and 'cy="0.0"' in r2.text
    assert 'id="node-c3"' in r2.text and 'cy="200.0"' in r2.text
    # c3 的 y 应大于 c1 的 y（分层自上而下）
    c1 = r2.text[r2.text.index('id="node-c1"'):]
    c3 = r2.text[r2.text.index('id="node-c3"'):]
    assert 'cy="0.0"' in c1
    assert 'cy="200.0"' in c3


def test_zero_ddl_after_hier_layout(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    ids = ("c1", "c2", "c3", "c4")
    _seed_dag(pid, ids=ids)
    layout = {n: {"x": 400.0, "y": 0.0} for n in ids}
    client.post("/api/v1/graph/layout", headers=csrf_headers, json={"layout": layout})
    assert db.count_user_objects(pid) == 16
