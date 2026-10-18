"""M26 / P0-C：turn 落库 + 会话回放。

- `db.write` 对 `turn`（INTEGER PK）允许省略 id，由 SQLite 生成，并同事务审计；
- 角色调用一次写两轮（user 输入 + 角色输出）；
- 单元详情与 REST 返回会话记录。
"""

import pytest

from hpsl import db, projects
from hpsl.models.base import Reply
from hpsl.routes import roles as roles_routes


class _FakeAdapter:
    name = "fake"
    supports_json = False

    def __init__(self, text="这是角色回答"):
        self.text = text

    def complete(self, messages, *, json_schema=None, stream=False):
        return Reply(
            text=self.text,
            finish_reason="stop",
            model="deepseek-chat",
            raw={"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        )


@pytest.fixture
def fake(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(roles_routes.registry, "pick_default_model", lambda role=None: "deepseek-chat")
    monkeypatch.setattr(roles_routes.registry, "build_adapter", lambda model_id, **kw: adapter)
    return adapter


def _project(client, csrf, name="Turn项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    return r.json()["data"]["id"]


def _unit(client, csrf, title="Turn单元"):
    r = client.post("/api/v1/units", headers=csrf, json={"title": title})
    return r.json()["data"]["id"]


def _session(pid, unit_id=None, sid="sess-t1"):
    db.write(
        pid,
        "session",
        {"id": sid, "unit_id": unit_id, "started_at": db._now_utc()},
        "user",
    )
    return sid


def test_write_turn_autoincrement_and_audit():
    vo = projects.create_project("Turn写入", "g")
    sid = _session(vo.id)
    rid1 = db.append_turn(vo.id, sid, "user", "我的重述", actor="user")
    rid2 = db.append_turn(vo.id, sid, "clerk", "候选缺口 A", actor="system")
    assert rid1.isdigit() and rid2.isdigit()
    assert int(rid2) == int(rid1) + 1

    rows = db.list_turns(vo.id, session_id=sid)
    assert [r["content"] for r in rows] == ["我的重述", "候选缺口 A"]
    assert [r["role"] for r in rows] == ["user", "clerk"]

    audit = db.query(
        vo.id, "SELECT * FROM audit_log WHERE table_name='turn' AND row_id=?", (rid1,)
    )
    assert len(audit) == 1 and audit[0]["actor"] == "user"


def test_write_still_requires_id_for_non_turn():
    vo = projects.create_project("Turn主键", "g")
    with pytest.raises(ValueError):
        db.write(vo.id, "concept", {"name": "无主键"}, "user")


def test_role_call_writes_two_turns(client, csrf_headers, fake):
    pid = _project(client, csrf_headers)
    r = client.post(
        "/api/v1/roles/questioner/call",
        headers=csrf_headers,
        json={"learner_input": "groupby 是先分组再聚合"},
    )
    assert r.json()["code"] == "0", r.text
    sid = r.json()["data"]["session_id"]

    rows = db.list_turns(pid, session_id=sid)
    assert len(rows) == 2
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "groupby 是先分组再聚合"
    assert rows[0]["provider"] is None
    assert rows[1]["role"] == "questioner"
    assert rows[1]["content"] == "这是角色回答"
    assert rows[1]["provider"] == "deepseek"
    assert rows[1]["model"] == "deepseek-chat"


def test_second_role_call_appends(client, csrf_headers, fake):
    pid = _project(client, csrf_headers)
    r1 = client.post(
        "/api/v1/roles/clerk/call", headers=csrf_headers, json={"learner_input": "第一轮"}
    )
    sid = r1.json()["data"]["session_id"]
    r2 = client.post(
        "/api/v1/roles/clerk/call",
        headers=csrf_headers,
        json={"learner_input": "第二轮", "session_id": sid},
    )
    assert r2.json()["code"] == "0", r2.text
    rows = db.list_turns(pid, session_id=sid)
    assert len(rows) == 4
    assert [r["content"] for r in rows] == ["第一轮", "这是角色回答", "第二轮", "这是角色回答"]
    assert all(int(rows[i]["id"]) < int(rows[i + 1]["id"]) for i in range(len(rows) - 1))


def test_unit_detail_renders_turns(client, csrf_headers, fake):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/roles/questioner/call",
        headers=csrf_headers,
        json={"unit_id": uid, "learner_input": "会话里应出现的句子"},
    )
    assert r.json()["code"] == "0", r.text
    page = client.get(f"/unit/{uid}")
    assert "⑨ 会话记录" in page.text
    assert "提问者" in page.text
    assert "会话里应出现的句子" in page.text
    assert "这是角色回答" in page.text


def test_unit_api_includes_turns(client, csrf_headers, fake):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    client.post(
        "/api/v1/roles/clerk/call",
        headers=csrf_headers,
        json={"unit_id": uid, "learner_input": "接口里的内容"},
    )
    d = client.get(f"/api/v1/units/{uid}").json()["data"]
    assert len(d["turns"]) == 2
    assert d["turns"][0]["content"] == "接口里的内容"


def test_turns_scoped_to_unit(client, csrf_headers, fake):
    _project(client, csrf_headers)
    u1 = _unit(client, csrf_headers, "单元一")
    u2 = _unit(client, csrf_headers, "单元二")
    client.post(
        "/api/v1/roles/clerk/call",
        headers=csrf_headers,
        json={"unit_id": u1, "learner_input": "只属于单元一"},
    )
    page2 = client.get(f"/unit/{u2}")
    assert "只属于单元一" not in page2.text
    assert "还没有对话记录" in page2.text


def test_zero_ddl_after_turns(client, csrf_headers, fake):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    client.post(
        "/api/v1/roles/clerk/call",
        headers=csrf_headers,
        json={"unit_id": uid, "learner_input": "零 DDL"},
    )
    assert db.count_user_objects(pid) == 16
