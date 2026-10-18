"""P0-A 单元主链测试：建单元 / 产出闸门 / 尝试 / 闭环 / 复测入队。

覆盖 REF-2 的 P0-01（无建单元）与 P0-04（复测不入队）。
全部经 HTTP 端点，断言落库结果与审计；零 DDL 由 `count_user_objects == 16` 守护。
"""

from hpsl import db
from hpsl.engine.review_queue import _add_days


def _project(client, csrf, name="P0项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _unit(client, csrf, title="单元A", band=None):
    body = {"title": title}
    if band:
        body["difficulty_band"] = band
    r = client.post("/api/v1/units", headers=csrf, json=body)
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _move(client, csrf, uid, cur, nxt, **extra):
    return client.post(
        f"/api/v1/units/{uid}/move", headers=csrf, json={"cur": cur, "nxt": nxt, **extra}
    )


def _to_judge(client, csrf, uid):
    for cur, nxt in (("INIT", "PICK"), ("PICK", "LEARN")):
        assert _move(client, csrf, uid, cur, nxt).json()["code"] == "0"
    assert client.post(
        f"/api/v1/units/{uid}/production", headers=csrf, json={"production": "跑通并汇总"}
    ).json()["code"] == "0"
    for cur, nxt in (("LEARN", "VERIFY"), ("VERIFY", "JUDGE")):
        assert _move(client, csrf, uid, cur, nxt).json()["code"] == "0"


def test_create_unit_ok(client, csrf_headers):
    pid = _project(client, csrf_headers)
    r = client.post(
        "/api/v1/units",
        headers=csrf_headers,
        json={"title": "用 pandas 做销售汇总", "difficulty_band": "拉伸"},
    )
    data = r.json()["data"]
    assert r.json()["code"] == "0"
    assert data["status"] == "INIT"
    assert data["allowed_next"] == ["PICK"]
    rows = db.query(pid, "SELECT * FROM unit WHERE id=?", (data["id"],))
    assert len(rows) == 1
    assert rows[0]["title"] == "用 pandas 做销售汇总"
    assert rows[0]["difficulty_band"] == "拉伸"
    audits = db.query(
        pid, "SELECT * FROM audit_log WHERE table_name='unit' AND row_id=?", (data["id"],)
    )
    assert len(audits) == 1 and audits[0]["actor"] == "user"


def test_create_unit_validation(client, csrf_headers):
    _project(client, csrf_headers)
    cases = [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": "t", "difficulty_band": "超难"},
    ]
    for body in cases:
        r = client.post("/api/v1/units", headers=csrf_headers, json=body)
        assert r.json()["code"] == "C400001", (body, r.text)


def test_create_unit_requires_active_project(client, csrf_headers):
    r = client.post("/api/v1/units", headers=csrf_headers, json={"title": "无项目"})
    assert r.json()["code"] == "A010001", r.text


def test_production_gate_and_produced_at(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    assert _move(client, csrf_headers, uid, "INIT", "PICK").json()["code"] == "0"
    assert _move(client, csrf_headers, uid, "PICK", "LEARN").json()["code"] == "0"

    # 未产出直接进入 VERIFY → A020003（PRODUCE 闸门）
    blocked = _move(client, csrf_headers, uid, "LEARN", "VERIFY")
    assert blocked.json()["code"] == "A020003", blocked.text
    assert db.query(pid, "SELECT status FROM unit WHERE id=?", (uid,))[0]["status"] == "LEARN"

    r = client.post(
        f"/api/v1/units/{uid}/production",
        headers=csrf_headers,
        json={"production": "完成汇总与 TOP5"},
    )
    assert r.json()["code"] == "0", r.text

    moved = _move(client, csrf_headers, uid, "LEARN", "VERIFY")
    assert moved.json()["code"] == "0", moved.text
    assert moved.json()["data"]["status"] == "VERIFY"
    row = db.query(pid, "SELECT production, produced_at FROM unit WHERE id=?", (uid,))[0]
    assert row["production"] == "完成汇总与 TOP5"
    assert row["produced_at"]  # 进入 VERIFY 时才写 produced_at


def test_production_empty_rejected(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    for body in ({"production": ""}, {"production": "   "}, {}):
        r = client.post(f"/api/v1/units/{uid}/production", headers=csrf_headers, json=body)
        assert r.json()["code"] == "C400001", (body, r.text)


def test_attempt_record(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/units/{uid}/attempts",
        headers=csrf_headers,
        json={"kind": "重述", "self_score": 0.8, "confirmed_score": 0.6, "seconds": 120},
    )
    assert r.json()["code"] == "0", r.text
    aid = r.json()["data"]["attempt_id"]
    row = db.query(pid, "SELECT * FROM attempt WHERE id=?", (aid,))[0]
    assert row["kind"] == "重述"
    assert row["self_score"] == 0.8
    assert row["confirmed_score"] == 0.6
    assert row["seconds"] == 120
    assert db.query(
        pid, "SELECT count(*) c FROM audit_log WHERE table_name='attempt' AND row_id=?", (aid,)
    )[0]["c"] == 1


def test_attempt_validation(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    cases = [
        {"kind": "默写"},
        {"kind": "重述", "self_score": 1.5},
        {"kind": "重述", "confirmed_score": -0.1},
        {"kind": "重述", "concept_id": "不存在"},
        {"kind": "重述", "seconds": "abc"},
        {"kind": "重述", "rubric": "not-a-dict"},
    ]
    for body in cases:
        r = client.post(f"/api/v1/units/{uid}/attempts", headers=csrf_headers, json=body)
        assert r.json()["code"] == "C400001", (body, r.text)


def test_close_pass(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    _to_judge(client, csrf_headers, uid)
    r = client.post(
        f"/api/v1/units/{uid}/close", headers=csrf_headers, json={"confirmed_score": 0.8}
    )
    assert r.json()["code"] == "0", r.text
    data = r.json()["data"]
    assert data["status"] == "INTEGRATE"
    assert "PICK" in data["allowed_next"] and "CLOSED" in data["allowed_next"]
    row = db.query(pid, "SELECT * FROM unit WHERE id=?", (uid,))[0]
    assert row["closed_at"]
    assert row["forced_close"] == 0


def test_close_missing_score(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    _to_judge(client, csrf_headers, uid)
    r = client.post(f"/api/v1/units/{uid}/close", headers=csrf_headers, json={})
    assert r.json()["code"] == "A050004", r.text


def test_close_requires_judge(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)  # INIT
    r = client.post(
        f"/api/v1/units/{uid}/close", headers=csrf_headers, json={"confirmed_score": 0.9}
    )
    assert r.json()["code"] == "A020001", r.text


def test_close_enqueues_review(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    cid = "c1"
    db.write(pid, "concept", {"id": cid, "name": "groupby", "verify_status": "未验证"}, "user")
    client.post(
        f"/api/v1/units/{uid}/attempts",
        headers=csrf_headers,
        json={"kind": "边界", "self_score": 0.7, "confirmed_score": 0.75, "concept_id": cid},
    )
    _to_judge(client, csrf_headers, uid)
    r = client.post(
        f"/api/v1/units/{uid}/close", headers=csrf_headers, json={"confirmed_score": 0.75}
    )
    assert r.json()["code"] == "0", r.text
    assert r.json()["data"]["reviews_enqueued"] == 1

    closed_at = db.query(pid, "SELECT closed_at FROM unit WHERE id=?", (uid,))[0]["closed_at"]
    reviews = db.query(pid, "SELECT * FROM review WHERE concept_id=?", (cid,))
    assert len(reviews) == 1
    assert reviews[0]["result"] is None  # 保持活跃，仅推进 due
    assert reviews[0]["due"] == _add_days(closed_at, 2)


def test_zero_ddl_after_p0_flows(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    _to_judge(client, csrf_headers, uid)
    client.post(f"/api/v1/units/{uid}/close", headers=csrf_headers, json={"confirmed_score": 0.9})
    assert db.count_user_objects(pid) == 16
