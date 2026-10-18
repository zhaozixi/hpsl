"""M29 / P1-02：验证判分组端点（自评 / 确认得分 / 评分要点守卫 / 强行闭环 / 挑错）。"""

from hpsl import db


def _project(client, csrf, name="判定项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    return r.json()["data"]["id"]


def _unit(client, csrf, title="判定单元"):
    return client.post("/api/v1/units", headers=csrf, json={"title": title}).json()["data"]["id"]


def _attempt(client, csrf, uid, *, kind="边界", rubric=None, self_score=None, confirmed=None):
    body = {"kind": kind}
    if rubric is not None:
        body["rubric"] = rubric
    if self_score is not None:
        body["self_score"] = self_score
    if confirmed is not None:
        body["confirmed_score"] = confirmed
    r = client.post(
        f"/api/v1/units/{uid}/attempts", headers=csrf, json=body
    )
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["attempt_id"]


def _to_judge(client, csrf, uid):
    for cur, nxt in (("INIT", "PICK"), ("PICK", "LEARN")):
        client.post(f"/api/v1/units/{uid}/move", headers=csrf, json={"cur": cur, "nxt": nxt})
    client.post(f"/api/v1/units/{uid}/production", headers=csrf, json={"production": "产出"})
    for cur, nxt in (("LEARN", "VERIFY"), ("VERIFY", "JUDGE")):
        client.post(f"/api/v1/units/{uid}/move", headers=csrf, json={"cur": cur, "nxt": nxt})


def test_self_and_confirm_score(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    aid = _attempt(client, csrf_headers, uid)

    r1 = client.post(
        f"/api/v1/units/{uid}/attempts/{aid}/self-score",
        headers=csrf_headers,
        json={"self_score": 0.8},
    )
    assert r1.json()["code"] == "0", r1.text
    r2 = client.post(
        f"/api/v1/units/{uid}/attempts/{aid}/confirm-score",
        headers=csrf_headers,
        json={"confirmed_score": 0.6},
    )
    assert r2.json()["code"] == "0", r2.text
    row = db.query(pid, "SELECT self_score, confirmed_score FROM attempt WHERE id=?", (aid,))[0]
    assert row["self_score"] == 0.8 and row["confirmed_score"] == 0.6


def test_score_validation(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    aid = _attempt(client, csrf_headers, uid)
    for bad in ("abc", 1.5, -0.1):
        r = client.post(
            f"/api/v1/units/{uid}/attempts/{aid}/self-score",
            headers=csrf_headers,
            json={"self_score": bad},
        )
        assert r.json()["code"] == "C400001", (bad, r.text)


def test_rubric_hidden_until_self_scored(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    aid = _attempt(client, csrf_headers, uid, rubric={"points": ["必须答出边界条件"]})

    # 自评前不可见
    hidden = client.get(f"/api/v1/units/{uid}/attempts/{aid}/rubric")
    assert hidden.json()["code"] == "A050001", hidden.text

    client.post(
        f"/api/v1/units/{uid}/attempts/{aid}/self-score",
        headers=csrf_headers,
        json={"self_score": 0.7},
    )
    shown = client.get(f"/api/v1/units/{uid}/attempts/{aid}/rubric")
    assert shown.json()["code"] == "0", shown.text
    assert shown.json()["data"]["rubric"]["points"] == ["必须答出边界条件"]


def test_attempt_must_belong_to_unit(client, csrf_headers):
    _project(client, csrf_headers)
    u1 = _unit(client, csrf_headers, "单元一")
    u2 = _unit(client, csrf_headers, "单元二")
    aid = _attempt(client, csrf_headers, u1)
    r = client.get(f"/api/v1/units/{u2}/attempts/{aid}/rubric")
    assert r.json()["code"] == "C400001", r.text


def test_force_close_requires_reason(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    _to_judge(client, csrf_headers, uid)
    r = client.post(
        f"/api/v1/units/{uid}/force-close",
        headers=csrf_headers,
        json={"confirmed_score": 0.3},
    )
    assert r.json()["code"] == "A050003", r.text


def test_force_close_marks_unit(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    _to_judge(client, csrf_headers, uid)
    r = client.post(
        f"/api/v1/units/{uid}/force-close",
        headers=csrf_headers,
        json={"confirmed_score": 0.3, "close_reason": "时间不够，先收尾"},
    )
    assert r.json()["code"] == "0", r.text
    assert r.json()["data"]["status"] == "INTEGRATE"
    row = db.query(pid, "SELECT forced_close, close_reason FROM unit WHERE id=?", (uid,))[0]
    assert row["forced_close"] == 1
    assert row["close_reason"] == "时间不够，先收尾"


def test_critic_same_source_warning(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/units/{uid}/critic",
        headers=csrf_headers,
        json={
            "claim": "groupby 不会打乱行序",
            "critic_model_id": "deepseek-chat",
            "answer_model_id": "deepseek-chat",
            "manual": True,
        },
    )
    assert r.json()["code"] == "0", r.text
    d = r.json()["data"]
    assert d["same_source"] is True
    assert len(d["created"]) == 1
    row = db.query(pid, "SELECT * FROM claim_check WHERE id=?", (d["created"][0],))[0]
    assert row["agree"] is None
    assert row["residual"] == 0


def test_critic_no_trigger_returns_empty(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/units/{uid}/critic",
        headers=csrf_headers,
        json={
            "claim": "普通断言",
            "critic_model_id": "deepseek-chat",
            "answer_model_id": "gpt-4o",
        },
    )
    assert r.json()["code"] == "0", r.text
    assert r.json()["data"]["created"] == []


def test_critic_claim_required(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(f"/api/v1/units/{uid}/critic", headers=csrf_headers, json={})
    assert r.json()["code"] == "C400001", r.text


def test_zero_ddl_after_judging(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    aid = _attempt(client, csrf_headers, uid, rubric={"p": 1})
    client.post(
        f"/api/v1/units/{uid}/attempts/{aid}/self-score",
        headers=csrf_headers,
        json={"self_score": 0.5},
    )
    assert db.count_user_objects(pid) == 16
