"""P1-05 验收：概念 / 卡片 / 概念边的写入能力 + 卡片页新建表单。

- AC1：POST /api/v1/concepts 建概念；同名（COLLATE NOCASE）→ C400001。
- AC2：POST /api/v1/cards 建卡片（含 links 对象/数组）；概念不存在 / my_words 越界 → C400001。
- AC3：POST /api/v1/concept-edges 建边；重复边（src,dst,kind）→ C400001。
- AC4：/cards 页顶部含「新建概念 / 新建卡片」两个 HTMX 表单，concepts 注入下拉。
- AC5：零 DDL（count_user_objects == 16），全部写经 db.write（audit_log actor=user）。
"""

import json

from hpsl import db


def _create_and_switch(client, csrf_headers, name="P1-05项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _new_concept(client, csrf_headers, name="概念甲"):
    r = client.post("/api/v1/concepts", headers=csrf_headers, json={"name": name})
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]


# ---------------------------------------------------------------------------
# AC1：概念
# ---------------------------------------------------------------------------
def test_create_concept(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    data = _new_concept(client, csrf_headers, "DataFrame 索引对齐")
    assert len(data["id"]) == 32
    assert data["name"] == "DataFrame 索引对齐"
    assert data["verify_status"] == "未验证"

    rows = db.query(pid, "SELECT * FROM concept WHERE id=?", (data["id"],))
    assert len(rows) == 1
    assert rows[0]["verify_status"] == "未验证"
    assert rows[0]["last_verified_at"] is None
    # 写经 db.write（actor=user），未直写 audit_log
    audit = db.query(pid, "SELECT actor, table_name FROM audit_log ORDER BY id DESC LIMIT 1")
    assert audit[0]["actor"] == "user"
    assert audit[0]["table_name"] == "concept"


def test_duplicate_concept_rejected(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _new_concept(client, csrf_headers, "DataFrame")
    r = client.post("/api/v1/concepts", headers=csrf_headers, json={"name": "dataframe"})
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"
    assert len(db.query(pid, "SELECT id FROM concept")) == 1


# ---------------------------------------------------------------------------
# AC2：卡片
# ---------------------------------------------------------------------------
def test_create_card_with_links(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    c = _new_concept(client, csrf_headers)
    links = {"sources": ["book#p12"], "tags": ["索引"]}
    r = client.post(
        "/api/v1/cards",
        headers=csrf_headers,
        json={"concept_id": c["id"], "my_words": "按索引对齐后再相加", "links": links},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["code"] == "0"
    card = payload["data"]
    assert card["concept_id"] == c["id"]
    assert card["my_words"] == "按索引对齐后再相加"
    assert card["links"] == links
    assert card["verify_status"] == "未验证"

    rows = db.query(pid, "SELECT * FROM card WHERE id=?", (card["id"],))
    assert len(rows) == 1
    assert json.loads(rows[0]["links"]) == links


def test_card_concept_missing_rejected(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    r = client.post(
        "/api/v1/cards",
        headers=csrf_headers,
        json={"concept_id": "不存在的概念id", "my_words": "我的理解"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"
    assert db.query(pid, "SELECT id FROM card") == []


def test_card_my_words_validation(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    c = _new_concept(client, csrf_headers)

    for my_words in ("", "x" * 501):
        r = client.post(
            "/api/v1/cards",
            headers=csrf_headers,
            json={"concept_id": c["id"], "my_words": my_words},
        )
        assert r.status_code == 400, (len(my_words), r.text)
        assert r.json()["code"] == "C400001"
    assert db.query(pid, "SELECT id FROM card") == []

    # 边界：500 字符可写入，links 缺省为 NULL
    ok = client.post(
        "/api/v1/cards",
        headers=csrf_headers,
        json={"concept_id": c["id"], "my_words": "y" * 500},
    )
    assert ok.status_code == 200
    assert ok.json()["code"] == "0"
    assert ok.json()["data"]["links"] is None
    assert len(db.query(pid, "SELECT id FROM card")) == 1


# ---------------------------------------------------------------------------
# AC3：概念边
# ---------------------------------------------------------------------------
def test_create_edge(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    a = _new_concept(client, csrf_headers, "概念甲")
    b = _new_concept(client, csrf_headers, "概念乙")
    r = client.post(
        "/api/v1/concept-edges",
        headers=csrf_headers,
        json={"src": a["id"], "dst": b["id"], "kind": "prereq"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["code"] == "0"
    edge = payload["data"]
    assert (edge["src"], edge["dst"], edge["kind"]) == (a["id"], b["id"], "prereq")
    rows = db.query(pid, "SELECT * FROM concept_edge WHERE id=?", (edge["id"],))
    assert rows and rows[0]["kind"] == "prereq"

    # src/dst 相同 → C400001
    same = client.post(
        "/api/v1/concept-edges",
        headers=csrf_headers,
        json={"src": a["id"], "dst": a["id"], "kind": "prereq"},
    )
    assert same.status_code == 400
    assert same.json()["code"] == "C400001"


def test_duplicate_edge_rejected(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    a = _new_concept(client, csrf_headers, "概念甲")
    b = _new_concept(client, csrf_headers, "概念乙")
    body = {"src": a["id"], "dst": b["id"], "kind": "prereq"}
    first = client.post("/api/v1/concept-edges", headers=csrf_headers, json=body)
    assert first.status_code == 200
    assert first.json()["code"] == "0"
    dup = client.post("/api/v1/concept-edges", headers=csrf_headers, json=body)
    assert dup.status_code == 400
    assert dup.json()["code"] == "C400001"
    assert len(db.query(pid, "SELECT id FROM concept_edge")) == 1


# ---------------------------------------------------------------------------
# AC4：页面表单（HTMX form-urlencoded，成功 HX-Refresh）
# ---------------------------------------------------------------------------
def test_cards_page_has_create_forms(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    c = _new_concept(client, csrf_headers, "页面概念")
    r = client.get("/cards")
    assert r.status_code == 200
    assert "新建概念" in r.text
    assert "新建卡片" in r.text
    assert 'hx-post="/api/v1/concepts"' in r.text
    assert 'hx-post="/api/v1/cards"' in r.text
    assert 'enctype="application/x-www-form-urlencoded"' in r.text
    assert 'name="concept_id"' in r.text
    assert f'value="{c["id"]}"' in r.text
    assert "页面概念" in r.text

    # HTMX form 提交（form-urlencoded）成功 → HX-Refresh: true
    htmx = {**csrf_headers, "HX-Request": "true"}
    created = client.post("/api/v1/concepts", headers=htmx, data={"name": "HTMX概念"})
    assert created.status_code == 200, created.text
    assert created.headers.get("HX-Refresh") == "true"
    card = client.post(
        "/api/v1/cards",
        headers=htmx,
        data={"concept_id": c["id"], "my_words": "HTMX 建卡"},
    )
    assert card.status_code == 200, card.text
    assert card.headers.get("HX-Refresh") == "true"


# ---------------------------------------------------------------------------
# AC5：未激活项目 + 零 DDL
# ---------------------------------------------------------------------------
def test_write_requires_active_project(client, csrf_headers, monkeypatch):
    monkeypatch.setattr("hpsl.routes.graph.proj.active_project", lambda: None)
    r = client.post("/api/v1/concepts", headers=csrf_headers, json={"name": "任意概念"})
    assert r.status_code == 200
    assert r.json()["code"] == "A010001"


def test_zero_ddl_after_writes(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    a = _new_concept(client, csrf_headers, "概念甲")
    b = _new_concept(client, csrf_headers, "概念乙")
    client.post(
        "/api/v1/cards",
        headers=csrf_headers,
        json={"concept_id": a["id"], "my_words": "零 DDL 卡片", "links": []},
    )
    client.post(
        "/api/v1/concept-edges",
        headers=csrf_headers,
        json={"src": a["id"], "dst": b["id"], "kind": "supports"},
    )
    assert db.count_user_objects(pid) == 16
