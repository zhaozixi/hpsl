"""M31 / E-07：周盘点增强（下周再看 / 前置 / 一次提交）。"""

import json
from datetime import datetime, timedelta, timezone

from hpsl import db


def _project(client, csrf, name="周盘点项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    return r.json()["data"]["id"]


def _gap(pid, gid="g1", level=2, status="待补", context=None, text="缺口甲"):
    db.write(
        pid,
        "gap",
        {
            "id": gid,
            "unit_id": None,
            "level": level,
            "text": text,
            "status": status,
            "context": json.dumps(context, ensure_ascii=False) if context is not None else None,
            "created_at": db._now_utc(),
        },
        "user",
    )


def _ctx(pid, gid="g1"):
    raw = db.query(pid, "SELECT context FROM gap WHERE id=?", (gid,))[0]["context"]
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def test_weekly_batch_status_defer_prereq(client, csrf_headers):
    pid = _project(client, csrf_headers)
    _gap(pid)
    r = client.post(
        "/api/v1/gaps/weekly-batch",
        headers=csrf_headers,
        json={"items": [{"id": "g1", "status": "已补", "defer_days": 7, "prereq": "先补 pandas 基础"}]},
    )
    assert r.json()["code"] == "0", r.text
    assert r.json()["data"] == {"updated": 1, "skipped": 0}
    row = db.query(pid, "SELECT status FROM gap WHERE id='g1'")[0]
    assert row["status"] == "已补"
    ctx = _ctx(pid)
    assert ctx["prereq"] == "先补 pandas 基础"
    expected = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
    assert ctx["defer_until"] == expected


def test_defer_moves_gap_to_next_week_section(client, csrf_headers):
    pid = _project(client, csrf_headers)
    _gap(pid, text="推迟的缺口")
    client.post(
        "/api/v1/gaps/weekly-batch",
        headers=csrf_headers,
        json={"items": [{"id": "g1", "defer_days": 7}]},
    )
    page = client.get("/weekly")
    assert "下周再看（1）" in page.text
    assert "推迟的缺口" in page.text
    # 仍在待补，但已不在「待裁决」表
    assert 'id="weekly-gap-table"' not in page.text


def test_weekly_batch_form_submission(client, csrf_headers):
    pid = _project(client, csrf_headers)
    _gap(pid)
    h = {**csrf_headers, "HX-Request": "true"}
    r = client.post(
        "/api/v1/gaps/weekly-batch",
        headers=h,
        data={"status-g1": "废弃", "prereq-g1": "不需要", "defer-g1": "7"},
    )
    assert r.headers.get("HX-Refresh") == "true"
    assert "已更新 1 条" in r.text
    assert db.query(pid, "SELECT status FROM gap WHERE id='g1'")[0]["status"] == "废弃"
    assert _ctx(pid)["prereq"] == "不需要"


def test_invalid_status_rejects_all(client, csrf_headers):
    pid = _project(client, csrf_headers)
    _gap(pid, "g1")
    _gap(pid, "g2", text="缺口乙")
    r = client.post(
        "/api/v1/gaps/weekly-batch",
        headers=csrf_headers,
        json={
            "items": [
                {"id": "g1", "status": "已补"},
                {"id": "g2", "status": "乱写"},
            ]
        },
    )
    assert r.json()["code"] == "C400001", r.text
    assert db.query(pid, "SELECT status FROM gap WHERE id='g1'")[0]["status"] == "待补"


def test_invalid_defer_days(client, csrf_headers):
    _project(client, csrf_headers)
    for bad in ("abc", 0, 400):
        r = client.post(
            "/api/v1/gaps/weekly-batch",
            headers=csrf_headers,
            json={"items": [{"id": "g1", "defer_days": bad}]},
        )
        assert r.json()["code"] == "C400001", (bad, r.text)


def test_item_without_action_is_skipped(client, csrf_headers):
    pid = _project(client, csrf_headers)
    _gap(pid)
    r = client.post(
        "/api/v1/gaps/weekly-batch",
        headers=csrf_headers,
        json={"items": [{"id": "g1"}]},
    )
    assert r.json()["data"] == {"updated": 0, "skipped": 1}


def test_existing_context_preserved(client, csrf_headers):
    pid = _project(client, csrf_headers)
    _gap(pid, context={"note": "已有备注"})
    client.post(
        "/api/v1/gaps/weekly-batch",
        headers=csrf_headers,
        json={"items": [{"id": "g1", "prereq": "前置 A"}]},
    )
    ctx = _ctx(pid)
    assert ctx["note"] == "已有备注"
    assert ctx["prereq"] == "前置 A"


def test_zero_ddl_after_weekly(client, csrf_headers):
    pid = _project(client, csrf_headers)
    _gap(pid)
    client.post(
        "/api/v1/gaps/weekly-batch",
        headers=csrf_headers,
        json={"items": [{"id": "g1", "status": "已补", "defer_days": 3}]},
    )
    assert db.count_user_objects(pid) == 16


def test_undefer_clears_defer_and_returns_to_list(client, csrf_headers):
    pid = _project(client, csrf_headers)
    _gap(pid, text="推迟后又取消")
    client.post(
        "/api/v1/gaps/weekly-batch",
        headers=csrf_headers,
        json={"items": [{"id": "g1", "defer_days": 7}]},
    )
    r = client.post("/api/v1/gaps/g1/undefer", headers=csrf_headers)
    assert r.json()["code"] == "0", r.text
    assert r.json()["data"]["cleared"] is True
    assert "defer_until" not in _ctx(pid)

    page = client.get("/weekly")
    assert "推迟后又取消" in page.text
    assert 'id="weekly-gap-table"' in page.text


def test_undefer_missing_gap(client, csrf_headers):
    _project(client, csrf_headers)
    r = client.post("/api/v1/gaps/nope/undefer", headers=csrf_headers)
    assert r.json()["code"] == "C400001", r.text


def test_undefer_is_idempotent(client, csrf_headers):
    pid = _project(client, csrf_headers)
    _gap(pid)
    r = client.post("/api/v1/gaps/g1/undefer", headers=csrf_headers)
    assert r.json()["code"] == "0"
    assert r.json()["data"]["cleared"] is False
