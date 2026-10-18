"""M13 批次2 验收：概念图交互再打磨（纯前端 + graph.py 只读补字段）。

- AC1：GET /graph 注入缩放控件（＋ / − / 重置 / 适配）与搜索框。
- AC2：节点携带 verify_status（data-status 注入；NODES JSON 含字段），状态着色图例齐备。
- AC3：既有渲染契约不变（节点文本、关系标注、selectNode/resetView 钩子）。
- AC4：零 DDL（count_user_objects == 16）。

纪律：零 DDL、无新端点、graph.py 仅读 concept.verify_status、前端仅改 graph.html。
"""

import pytest

from hpsl import db, projects  # noqa: F401
from hpsl import config as cfg  # noqa: F401


def _create_and_switch(client, csrf_headers, name="M13G项目", goal="目标g"):
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


def test_graph_has_zoom_controls_and_search(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/graph")
    assert r.status_code == 200
    for token in [
        'data-action="graph-zoom"',
        'data-factor="1.2"',
        'data-factor="0.8333"',
        'data-action="graph-reset"',
        'data-action="graph-fit"',
        'id="graph-search"',
        "重置",
        "适配",
    ]:
        assert token in r.text, f"缺少交互控件: {token}"


def test_graph_nodes_carry_verify_status(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_edge(pid)
    r = client.get("/graph")
    assert r.status_code == 200
    # 节点 data-status 注入（来自 concept.verify_status，只读）
    assert 'data-status="未验证"' in r.text
    assert 'data-status="已验证"' in r.text
    # 服务端注入的 NODES JSON 含 verify_status 字段
    assert '"verify_status"' in r.text


def test_graph_status_legend_present(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/graph")
    assert r.status_code == 200
    # 三态图例色块：已验证=绿 / 未验证=灰 / 待验证=深琥珀（≥4.5:1 白字对比）
    assert "#0a7d2c" in r.text
    assert "#9ca3af" in r.text
    assert "#b45309" in r.text


def test_graph_existing_contract_unchanged(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_edge(pid)
    r = client.get("/graph")
    assert r.status_code == 200
    assert "概念甲" in r.text
    assert "prereq" in r.text
    assert "概念节点（2）" in r.text
    assert 'data-id="c1"' in r.text
    assert "selectNode" in r.text
    assert "resetView" in r.text
    assert 'id="concept-graph"' in r.text
    assert 'id="viewport"' in r.text


def test_zero_ddl_after_graph_enhancements(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_edge(pid)
    client.get("/graph")
    assert db.count_user_objects(pid) == 16
