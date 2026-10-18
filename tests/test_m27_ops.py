"""M27：撤销 / 账本 REST / 成本 REST / 前端转义。"""

from hpsl import db, projects
from hpsl.engine import cost as cost_engine
from hpsl.models.base import Reply


def _project(client, csrf, name="Ops项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    return r.json()["data"]["id"]


def _unit(client, csrf, title="Ops单元"):
    return client.post("/api/v1/units", headers=csrf, json={"title": title}).json()["data"]["id"]


# ---------------------------------------------------------------------------
# 账本 REST
# ---------------------------------------------------------------------------
def test_ledger_summary_and_gaps(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    db.write(
        pid,
        "gap",
        {
            "id": "g1",
            "unit_id": uid,
            "level": 2,
            "text": "不阻塞的缺口",
            "status": "待补",
            "created_at": db._now_utc(),
        },
        "user",
    )
    summary = client.get("/api/v1/ledger/summary").json()["data"]
    assert summary["gaps"]["total"] == 1
    assert summary["gaps"]["待补"] == 1

    gaps = client.get("/api/v1/ledger/gaps?level=2").json()["data"]
    assert len(gaps) == 1 and gaps[0]["id"] == "g1"
    assert client.get("/api/v1/ledger/gaps?level=1").json()["data"] == []
    assert client.get("/api/v1/ledger/gaps?level=5").json()["code"] == "C400001"


def test_ledger_audit_list(client, csrf_headers):
    pid = _project(client, csrf_headers)
    rows = client.get("/api/v1/ledger/audit?limit=5").json()["data"]
    assert 1 <= len(rows) <= 5
    assert rows[0]["table_name"] == "project"  # 最新一条是建项目
    assert set(rows[0].keys()) == {"id", "ts", "actor", "table_name", "row_id"}


# ---------------------------------------------------------------------------
# 撤销
# ---------------------------------------------------------------------------
def _concept_audits(pid, row_id="k"):
    return db.query(
        pid,
        "SELECT id FROM audit_log WHERE table_name='concept' AND row_id=? ORDER BY id",
        (row_id,),
    )


def test_undo_update_restores_prev(client, csrf_headers):
    pid = _project(client, csrf_headers)
    db.write(pid, "concept", {"id": "k", "name": "v1"}, "user")
    db.write(pid, "concept", {"id": "k", "name": "v2"}, "user")
    audits = _concept_audits(pid)
    assert len(audits) == 2

    r = client.post(f"/api/v1/audit/{audits[1]['id']}/undo", headers=csrf_headers, json={})
    assert r.json()["code"] == "0", r.text
    assert db.query(pid, "SELECT name FROM concept WHERE id='k'")[0]["name"] == "v1"
    # 撤销动作本身留痕
    assert len(_concept_audits(pid)) == 3


def test_undo_stale_rejected_a070002(client, csrf_headers):
    pid = _project(client, csrf_headers)
    db.write(pid, "concept", {"id": "k", "name": "v1"}, "user")
    db.write(pid, "concept", {"id": "k", "name": "v2"}, "user")
    first = _concept_audits(pid)[0]
    r = client.post(f"/api/v1/audit/{first['id']}/undo", headers=csrf_headers, json={})
    assert r.json()["code"] == "A070002", r.text


def test_undo_missing_a070001(client, csrf_headers):
    _project(client, csrf_headers)
    r = client.post("/api/v1/audit/999999/undo", headers=csrf_headers, json={})
    assert r.json()["code"] == "A070001", r.text


def test_undo_htmx_refreshes(client, csrf_headers):
    pid = _project(client, csrf_headers)
    db.write(pid, "concept", {"id": "k", "name": "v1"}, "user")
    aid = _concept_audits(pid)[0]["id"]
    h = {**csrf_headers, "HX-Request": "true"}
    r = client.post(f"/api/v1/audit/{aid}/undo", headers=h)
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") == "true"
    assert "已撤销" in r.text


def test_ledger_page_lists_audit_with_undo(client, csrf_headers):
    _project(client, csrf_headers)
    page = client.get("/ledger")
    assert "最近操作（可撤销）" in page.text
    assert "/api/v1/audit/" in page.text
    assert "撤销" in page.text


# ---------------------------------------------------------------------------
# 成本 REST
# ---------------------------------------------------------------------------
def test_session_and_monthly_cost(client, csrf_headers):
    pid = _project(client, csrf_headers)
    sid = "sess-cost"
    db.write(
        pid,
        "session",
        {"id": sid, "unit_id": None, "started_at": db._now_utc()},
        "user",
    )
    cost_engine.record_call(
        "clerk",
        Reply(
            text="记账",
            finish_reason="stop",
            model="deepseek-chat",
            raw={"usage": {"prompt_tokens": 1000, "completion_tokens": 1000}},
        ),
        sid,
        None,
    )

    d = client.get(f"/api/v1/sessions/{sid}/cost").json()["data"]
    assert d["session_id"] == sid
    assert d["tokens_in"] == 1000 and d["tokens_out"] == 1000
    assert d["cost_usd"] > 0
    assert d["budget_cap_usd"] == 10.0
    assert d["status"] in ("ok", "warn", "blocked")

    m = client.get("/api/v1/cost/monthly").json()["data"]
    assert len(m["month"]) == 7 and m["month"][4] == "-"
    assert m["cumulative_usd"] == d["cumulative_usd"]
    assert m["status"] in ("ok", "warn", "blocked")


# ---------------------------------------------------------------------------
# 角色契约与 F3.3 输入预览
# ---------------------------------------------------------------------------
def test_role_contracts_lists_six_roles(client):
    rows = client.get("/api/v1/roles/contracts").json()["data"]
    roles = {r["role"] for r in rows}
    assert roles == {
        "topic_picker",
        "answer_machine",
        "questioner",
        "boundary_setter",
        "critic",
        "clerk",
    }
    for r in rows:
        assert isinstance(r["allow"], list) and r["allow"]
        assert "answer_machine_output" not in r["allow"]


def test_input_preview_only_allowed_fields(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    db.write(pid, "concept", {"id": "c1", "name": "groupby", "verify_status": "未验证"}, "user")
    d = client.get(f"/api/v1/units/{uid}/input-preview?role=topic_picker").json()["data"]
    assert d["role"] == "topic_picker"
    assert set(d["visible_fields"]) <= set(d["allowed_fields"])
    assert "unit_title" in d["visible_fields"]
    assert "concept_summary" in d["visible_fields"]
    assert "groupby" in d["visible_values"]["concept_summary"]
    assert "answer_machine_output" not in d["visible_fields"]


def test_input_preview_unknown_role(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.get(f"/api/v1/units/{uid}/input-preview?role=nobody")
    assert r.json()["code"] == "A030001", r.text


def test_input_preview_missing_unit(client, csrf_headers):
    _project(client, csrf_headers)
    r = client.get("/api/v1/units/nope/input-preview?role=critic")
    assert r.json()["code"] == "A020001", r.text


# ---------------------------------------------------------------------------
# 前端转义（P1-12）与 HTMX 本地化（P1-11）
# ---------------------------------------------------------------------------
def test_graph_uses_dom_text_not_innerhtml():
    html = open("hpsl/templates/graph.html", encoding="utf-8").read()
    assert "tooltip.innerHTML" not in html
    assert "d.innerHTML" not in html
    assert "document.createTextNode" in html


def test_htmx_is_local_and_served(client):
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert 'version:"1.9.12"' in r.text
    page = client.get("/")
    assert "/static/htmx.min.js" in page.text
    assert "unpkg.com" not in page.text


def test_zero_ddl_after_ops(client, csrf_headers):
    pid = _project(client, csrf_headers)
    db.write(pid, "concept", {"id": "k", "name": "v1"}, "user")
    aid = _concept_audits(pid)[0]["id"]
    client.post(f"/api/v1/audit/{aid}/undo", headers=csrf_headers, json={})
    assert db.count_user_objects(pid) == 16
