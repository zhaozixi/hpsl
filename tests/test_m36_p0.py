"""M36 / P0 回归：预算闸门、强行闭环、复测 21 天档、--port 对齐、graph 空态/连线。"""

from datetime import datetime, timedelta, timezone

import pytest

from hpsl import app as app_mod, db
from hpsl.engine import cost as cost_engine
from hpsl.engine import review_queue
from hpsl.models.base import Reply
from hpsl.routes import providers as providers_routes
from hpsl.routes import roles as roles_routes


class _Fake:
    name = "fake"

    def complete(self, messages, *, json_schema=None, stream=False):
        return Reply(text="ok", finish_reason="stop", model="deepseek-flash", raw={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})


class _StreamFake(_Fake):
    def stream_complete(self, messages, *, json_schema=None):
        yield {"delta": "ok"}
        yield {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _project(client, csrf, name="P0 回归"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    body = r.json()
    assert body and body.get("code") == "0", r.text
    return body["data"]["id"]


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _session(pid, sid="sess-budget"):
    db.write(pid, "session", {"id": sid, "unit_id": None, "started_at": db._now_utc()}, "user")
    return sid


def _seed_over_budget(pid, sid):
    """记一笔 2000 万输出 token 的调用，让本月累计远超默认 $10。"""
    cost_engine.record_call(
        "clerk",
        Reply(text="x", finish_reason="stop", model="deepseek-flash", raw={"usage": {"prompt_tokens": 0, "completion_tokens": 20_000_000}}),
        sid,
        None,
    )


def _to_judge(client, csrf, uid):
    for cur, nxt in (("INIT", "PICK"), ("PICK", "LEARN")):
        client.post(f"/api/v1/units/{uid}/move", headers=csrf, json={"cur": cur, "nxt": nxt})
    client.post(f"/api/v1/units/{uid}/production", headers=csrf, json={"production": "产出"})
    for cur, nxt in (("LEARN", "VERIFY"), ("VERIFY", "JUDGE")):
        client.post(f"/api/v1/units/{uid}/move", headers=csrf, json={"cur": cur, "nxt": nxt})


# ---------------------------------------------------------------------------
# A1 预算闸门
# ---------------------------------------------------------------------------
def test_provider_chat_blocked_by_budget_and_released_after_confirm(client, csrf_headers, monkeypatch):
    pid = _project(client, csrf_headers, "预算闸门")
    sid = _session(pid)
    _seed_over_budget(pid, sid)
    monkeypatch.setattr(providers_routes.registry, "build_adapter", lambda *a, **k: _Fake())

    body = {"model": "deepseek-flash", "session_id": sid, "messages": [{"role": "user", "content": "hi"}]}
    blocked = client.post("/api/v1/providers/deepseek/chat", headers=csrf_headers, json=body)
    assert blocked.json()["code"] == "A080001", blocked.text

    confirm = client.post(
        "/api/v1/budget/confirm", headers=csrf_headers, json={"session_id": sid}
    )
    assert confirm.json()["code"] == "0", confirm.text
    assert confirm.json()["data"]["status"] == "warn"

    ok = client.post("/api/v1/providers/deepseek/chat", headers=csrf_headers, json=body)
    assert ok.json()["code"] == "0", ok.text


def test_role_call_blocked_by_budget(client, csrf_headers, monkeypatch):
    pid = _project(client, csrf_headers, "角色预算")
    _seed_over_budget(pid, _session(pid, "sess-role"))
    monkeypatch.setattr(roles_routes.registry, "pick_default_model", lambda role=None: "deepseek-flash")
    monkeypatch.setattr(roles_routes.registry, "build_adapter", lambda *a, **k: _Fake())
    r = client.post(
        "/api/v1/roles/clerk/call", headers=csrf_headers, json={"learner_input": "hi"}
    )
    assert r.json()["code"] == "A080001", r.text


def test_ensure_budget_unit(monkeypatch):
    from hpsl import projects

    vo = projects.create_project("预算单元", "g")
    db.write(vo.id, "session", {"id": "s", "unit_id": None, "started_at": db._now_utc()}, "user")
    assert cost_engine.ensure_budget("s") == "ok"
    _seed_over_budget(vo.id, "s")
    with pytest.raises(Exception) as exc:
        cost_engine.ensure_budget("s")
    assert getattr(exc.value, "code", None) == "A080001"


# ---------------------------------------------------------------------------
# A2 强行闭环 UI
# ---------------------------------------------------------------------------
def test_close_panel_has_force_close_checkbox(client, csrf_headers):
    _project(client, csrf_headers, "强关页")
    uid = client.post("/api/v1/units", headers=csrf_headers, json={"title": "U"}).json()["data"]["id"]
    page = client.get(f"/unit/{uid}")
    assert 'name="force_close"' in page.text
    assert "未达标，强行闭环" in page.text


def test_force_close_via_page_payload(client, csrf_headers):
    pid = _project(client, csrf_headers, "强关")
    uid = client.post("/api/v1/units", headers=csrf_headers, json={"title": "U"}).json()["data"]["id"]
    _to_judge(client, csrf_headers, uid)
    r = client.post(
        f"/api/v1/units/{uid}/close",
        headers=csrf_headers,
        data={"confirmed_score": "0.2", "force_close": "1", "close_reason": "时间不够"},
    )
    body = r.json()
    assert body["code"] == "0", r.text
    assert body["data"]["status"] == "INTEGRATE"
    row = db.query(pid, "SELECT forced_close, close_reason FROM unit WHERE id=?", (uid,))[0]
    assert row["forced_close"] == 1 and row["close_reason"] == "时间不够"


def test_close_form_bool_false_not_truthy(client, csrf_headers):
    _project(client, csrf_headers, "布尔")
    uid = client.post("/api/v1/units", headers=csrf_headers, json={"title": "U"}).json()["data"]["id"]
    _to_judge(client, csrf_headers, uid)
    r = client.post(
        f"/api/v1/units/{uid}/close",
        headers=csrf_headers,
        data={"confirmed_score": "0.2", "force_close": "false"},
    )
    assert r.json()["code"] != "0", "force_close=false 不应被当成强行闭环"


# ---------------------------------------------------------------------------
# A3 复测 21 天档
# ---------------------------------------------------------------------------
def test_review_on_time_reaches_21_days():
    from hpsl import projects

    vo = projects.create_project("复测档", "g")
    now = datetime.now(timezone.utc)
    db.write(
        vo.id,
        "concept",
        {
            "id": "c1",
            "name": "概念",
            "verify_status": "已验证",
            "last_verified_at": _iso(now - timedelta(days=7)),
        },
        "user",
    )
    db.write(
        vo.id,
        "review",
        {"id": "rv1", "concept_id": "c1", "kind": "到期", "due": _iso(now)},
        "system",
    )
    result = review_queue.submit_review_by_id(vo.id, "rv1", 0.9)
    delta_days = (review_queue._parse_iso(result.next_due) - now).total_seconds() / 86400
    assert 20.9 < delta_days < 21.1, f"应按 7→21 推进，实际 {delta_days}"


# ---------------------------------------------------------------------------
# A4 --port / Host 对齐
# ---------------------------------------------------------------------------
def test_prepare_serve_sets_port_env(monkeypatch):
    import os

    fake_env = dict(os.environ)
    fake_env.pop("HPSL_PORT", None)
    monkeypatch.setattr(app_mod.os, "environ", fake_env)
    host, port = app_mod._prepare_serve("127.0.0.1", 9001)
    assert (host, port) == ("127.0.0.1", 9001)
    assert fake_env["HPSL_PORT"] == "9001"


def test_prepare_serve_rejects_non_loopback(monkeypatch):
    monkeypatch.delenv("HPSL_ALLOW_NON_LOOPBACK", raising=False)
    with pytest.raises(SystemExit):
        app_mod._prepare_serve("0.0.0.0", 8765)


# ---------------------------------------------------------------------------
# A5 / A6 graph
# ---------------------------------------------------------------------------
def test_graph_empty_has_detail_anchor(client, csrf_headers):
    _project(client, csrf_headers, "空图")
    page = client.get("/graph")
    assert 'id="graph-detail"' in page.text
    assert "暂无概念节点" in page.text


def test_graph_edges_have_recalc(client, csrf_headers):
    _project(client, csrf_headers, "连线")
    import hpsl.projects as projects

    pid = projects.active_project().id
    for cid in ("c1", "c2"):
        db.write(pid, "concept", {"id": cid, "name": cid, "verify_status": "未验证"}, "user")
    db.write(
        pid,
        "concept_edge",
        {"id": "e1", "src": "c1", "dst": "c2", "kind": "前置"},
        "user",
    )
    page = client.get("/graph")
    assert 'data-src="c1"' in page.text
    assert "function updateEdges" in page.text
