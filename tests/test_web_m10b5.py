"""M10B 第五批验收（卡片与概念图 UI）：`/cards` 页 + `/graph` 页 + 三个 REST。

- AC1：GET /cards 渲染（无激活提示 / 卡片列表 / 空态）。
- AC2：GET /graph 渲染 SVG 概念图（节点 + 连线 / 空态）。
- AC3：GET /api/v1/cards / /concepts / /concept-edges REST 返回数据。
- AC4：零 DDL（count_user_objects == 16 仍成立）。
"""

import pytest

from hpsl import db, projects


def _create_and_switch(client, csrf_headers, name="M10B5项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed_concept_card_edge(pid, *, cid="c1", cid2="c2", kid="k1"):
    db.write(pid, "concept", {"id": cid, "name": "概念甲", "verify_status": "未验证"}, "user")
    db.write(pid, "concept", {"id": cid2, "name": "概念乙", "verify_status": "未验证"}, "user")
    db.write(
        pid, "card",
        {"id": kid, "concept_id": cid, "my_words": "我的理解A", "links": [], "verify_status": "未验证"},
        "user",
    )
    db.write(pid, "concept_edge", {"id": "e1", "src": cid, "dst": cid2, "kind": "prereq"}, "user")


# ---------------------------------------------------------------------------
# AC1：卡片页
# ---------------------------------------------------------------------------
def test_cards_page_no_active(client):
    r = client.get("/cards")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


def test_cards_page_lists(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_card_edge(pid)
    r = client.get("/cards")
    assert r.status_code == 200
    assert "我的理解A" in r.text
    assert "概念甲" in r.text
    assert 'id="card-k1"' in r.text
    assert "卡片列表（1）" in r.text


def test_cards_page_empty(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/cards")
    assert r.status_code == 200
    assert "暂无卡片" in r.text


# ---------------------------------------------------------------------------
# AC2：概念图页
# ---------------------------------------------------------------------------
def test_graph_page_renders_svg(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_card_edge(pid)
    r = client.get("/graph")
    assert r.status_code == 200
    assert "<svg" in r.text
    assert "概念甲" in r.text
    assert "概念乙" in r.text
    assert "prereq" in r.text  # 边关系标注
    assert "概念节点（2）" in r.text


def test_graph_page_empty(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/graph")
    assert r.status_code == 200
    assert "暂无概念节点" in r.text


# ---------------------------------------------------------------------------
# AC3：REST
# ---------------------------------------------------------------------------
def test_cards_api(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_card_edge(pid)
    r = client.get("/api/v1/cards")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    data = r.json()["data"]
    assert len(data) >= 1
    assert data[0]["my_words"] == "我的理解A"
    assert data[0]["concept_name"] == "概念甲"


def test_concepts_api(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_card_edge(pid)
    r = client.get("/api/v1/concepts")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    ids = {c["id"] for c in r.json()["data"]}
    assert {"c1", "c2"} <= ids


def test_concept_edges_api(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_card_edge(pid)
    r = client.get("/api/v1/concept-edges")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    assert r.json()["data"][0]["kind"] == "prereq"


# ---------------------------------------------------------------------------
# AC4：零 DDL 守护
# ---------------------------------------------------------------------------
def test_zero_ddl_after_graph_features(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_card_edge(pid)
    client.get("/api/v1/cards")
    client.get("/api/v1/concepts")
    client.get("/api/v1/concept-edges")
    assert db.count_user_objects(pid) == 16
