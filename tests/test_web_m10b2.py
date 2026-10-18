"""M10B 第二批验收（规格 §3 AC1–AC6）：`/review` 复测页（消费 M6 引擎）。

- AC1：GET /review（激活项目）→ 200；列出到期复测（concept_name + 开始按钮）。
- AC2：GET /api/v1/reviews/due → 200；返回到期复测列表（含 concept_id）。
- AC3：POST /api/v1/reviews/{id}/start → 200；返回 ReviewStartVO（attempt_id 已建）。
- AC4：POST /api/v1/reviews/{id}/submit（score∈[0,1]）→ 200；写回隔离（wrote_to_current_unit=False），concept 准确率更新。
- AC5：A060001（条目不存在/已完成）；C400001（缺 score / score 越界）。
- AC6：零 DDL（count_user_objects == 16 仍成立）。
"""

import pytest

from hpsl import db, projects
from hpsl.engine.review_queue import enqueue_reviews, _add_days


def _create_and_switch(client, csrf_headers, name="M10B2项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed_due_review(client, csrf_headers, *, past_days=10, verify_status="未验证"):
    """建项目并切换，插入一条概念 + 一条已到期的复测，返回 (project_dict, pid, cid, rid)。"""
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    cid = "c1"
    db.write(pid, "concept", {"id": cid, "name": "概念甲", "verify_status": verify_status}, "user")
    closed_at = _add_days(db._now_utc(), -past_days)
    enqueue_reviews(pid, [cid], closed_at)
    rid = db.query(pid, "SELECT id FROM review WHERE concept_id=?", (cid,))[0]["id"]
    return d, pid, cid, rid


# ---------------------------------------------------------------------------
# AC1：复测页渲染（无激活 / 有到期项）
# ---------------------------------------------------------------------------
def test_review_page_no_active_project(client):
    r = client.get("/review")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


def test_review_page_lists_due(client, csrf_headers):
    _seed_due_review(client, csrf_headers)
    r = client.get("/review")
    assert r.status_code == 200
    assert "概念甲" in r.text
    assert "开始复测" in r.text
    assert "到期复测（1）" in r.text
    # 面板锚点存在
    assert 'id="panel-' in r.text


def test_review_page_empty_when_no_due(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/review")
    assert r.status_code == 200
    assert "暂无到期复测" in r.text


# ---------------------------------------------------------------------------
# AC2：due REST
# ---------------------------------------------------------------------------
def test_reviews_due_api(client, csrf_headers):
    _seed_due_review(client, csrf_headers)
    r = client.get("/api/v1/reviews/due")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    data = r.json()["data"]
    assert len(data) >= 1
    assert data[0]["concept_id"] == "c1"
    assert data[0]["concept_name"] == "概念甲"


def test_reviews_due_api_no_active(client):
    r = client.get("/api/v1/reviews/due")
    assert r.status_code == 200
    assert r.json()["code"] == "A010001"


# ---------------------------------------------------------------------------
# AC3：start REST
# ---------------------------------------------------------------------------
def test_start_review_api(client, csrf_headers):
    _, pid, _, rid = _seed_due_review(client, csrf_headers)
    r = client.post(f"/api/v1/reviews/{rid}/start", headers=csrf_headers)
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    d = r.json()["data"]
    assert d["review_id"] == rid
    assert d["concept_id"] == "c1"
    assert d["attempt_id"]  # 已建草稿卷
    # 草稿卷确实写入 attempt 表（隔离：不绑单元）
    a = db.query(pid, "SELECT kind, unit_id FROM attempt WHERE id=?", (d["attempt_id"],))[0]
    assert a["kind"] == "复测"
    assert a["unit_id"] is None


# ---------------------------------------------------------------------------
# AC4：submit REST（写回隔离 + 准确率更新）
# ---------------------------------------------------------------------------
def test_submit_review_api(client, csrf_headers):
    _, pid, cid, rid = _seed_due_review(client, csrf_headers)
    r = client.post(
        f"/api/v1/reviews/{rid}/submit",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json={"score": 0.9},
    )
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "0"
    d = r.json()["data"]
    assert d["review_id"] == rid
    assert d["passed"] is True
    assert d["wrote_to_current_unit"] is False  # 隔离不变量
    # 写回概念准确率（>=0.7 -> 已验证）
    c = db.query(pid, "SELECT verify_status FROM concept WHERE id=?", (cid,))[0]
    assert c["verify_status"] == "已验证"
    # review 仍保持活跃（result 维持 NULL）
    rev = db.query(pid, "SELECT result, attempt_id FROM review WHERE id=?", (rid,))[0]
    assert rev["result"] is None
    assert rev["attempt_id"] == d["attempt_id"]


def test_submit_review_below_threshold(client, csrf_headers):
    _, pid, cid, rid = _seed_due_review(client, csrf_headers, verify_status="已验证")
    r = client.post(
        f"/api/v1/reviews/{rid}/submit",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json={"score": 0.4},
    )
    assert r.json()["code"] == "0"
    assert r.json()["data"]["passed"] is False
    c = db.query(pid, "SELECT verify_status FROM concept WHERE id=?", (cid,))[0]
    assert c["verify_status"] == "待验证"


# ---------------------------------------------------------------------------
# AC5：错误码
# ---------------------------------------------------------------------------
def test_start_missing_review_a060001(client, csrf_headers):
    _seed_due_review(client, csrf_headers)
    r = client.post("/api/v1/reviews/no-such-review/start", headers=csrf_headers)
    assert r.status_code == 200
    assert r.json()["code"] == "A060001"


def test_submit_missing_review_a060001(client, csrf_headers):
    _seed_due_review(client, csrf_headers)
    r = client.post(
        "/api/v1/reviews/no-such-review/submit",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json={"score": 0.9},
    )
    assert r.status_code == 200
    assert r.json()["code"] == "A060001"


def test_submit_requires_score_c400001(client, csrf_headers):
    _, _, _, rid = _seed_due_review(client, csrf_headers)
    r = client.post(
        f"/api/v1/reviews/{rid}/submit",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json={},
    )
    # C 类客户端错误返回 HTTP 400（errors.py 注册 http_status=400）
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"


def test_submit_score_out_of_range_c400001(client, csrf_headers):
    _, _, _, rid = _seed_due_review(client, csrf_headers)
    r = client.post(
        f"/api/v1/reviews/{rid}/submit",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json={"score": 1.5},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# AC6：零 DDL 守护（不新增库对象）
# ---------------------------------------------------------------------------
def test_zero_ddl_after_review_features(client, csrf_headers):
    _, pid, _, rid = _seed_due_review(client, csrf_headers)
    # 跑一轮 start + submit，确保不触发任何 DDL
    client.post(f"/api/v1/reviews/{rid}/start", headers=csrf_headers)
    client.post(
        f"/api/v1/reviews/{rid}/submit",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json={"score": 0.9},
    )
    assert db.count_user_objects(pid) == 16
