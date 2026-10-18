"""P0-A 角色接线测试：`POST /api/v1/roles/{role}/call`。

覆盖 REF-2 的 P0-02（六角色未接线）与 P0-03（记账未接线）：
- 调用后 `llm_call` 落账、`session` 自动创建；
- 隔离不变量：`answer_machine_output` 绝不进入 messages；
- 错误路径：未知角色 A030001、无密钥 A040003、适配器失败 A040001、会话非法 C400001。
"""

import pytest

from hpsl import db
from hpsl.common.errors import BizError
from hpsl.models import registry
from hpsl.models.base import Reply
from hpsl.routes import roles as roles_routes


class _FakeAdapter:
    """记录 messages 的假适配器（不发网络请求）。"""

    name = "fake"
    supports_json = False

    def __init__(self, text="这是角色回答", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def complete(self, messages, *, json_schema=None, stream=False):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return Reply(
            text=self.text,
            finish_reason="stop",
            model="deepseek-chat",
            raw={"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )


@pytest.fixture
def fake(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(roles_routes.registry, "pick_default_model", lambda role=None: "deepseek-chat")
    monkeypatch.setattr(roles_routes.registry, "build_adapter", lambda model_id, **kw: adapter)
    return adapter


def _project(client, csrf, name="角色项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _unit(client, csrf, title="单元R"):
    r = client.post("/api/v1/units", headers=csrf, json={"title": title})
    return r.json()["data"]["id"]


def test_role_call_success_writes_llm_call(client, csrf_headers, fake):
    pid = _project(client, csrf_headers)
    r = client.post(
        "/api/v1/roles/answer_machine/call",
        headers=csrf_headers,
        json={"learner_input": "groupby 是什么？"},
    )
    assert r.json()["code"] == "0", r.text
    data = r.json()["data"]
    assert data["text"] == "这是角色回答"
    assert data["provider"] == "deepseek"
    assert data["model"] == "deepseek-chat"
    assert data["tokens_in"] == 10 and data["tokens_out"] == 5
    assert data["cost_status"] in ("ok", "warn", "blocked")

    calls = db.query(pid, "SELECT * FROM llm_call")
    assert len(calls) == 1
    assert calls[0]["role"] == "answer_machine"
    assert calls[0]["provider"] == "deepseek"
    assert calls[0]["tokens_in"] == 10

    sessions = db.query(pid, "SELECT * FROM session")
    assert len(sessions) == 1
    assert sessions[0]["id"] == data["session_id"]


def test_role_call_isolation_excludes_answer_machine_output(client, csrf_headers, fake):
    _project(client, csrf_headers)
    r = client.post(
        "/api/v1/roles/questioner/call",
        headers=csrf_headers,
        json={
            "learner_input": "我的重述",
            "context": {"topic": "T", "answer_machine_output": "哨兵-LEAK-42"},
        },
    )
    assert r.json()["code"] == "0", r.text
    sent = "\n".join(m.content for m in fake.calls[0])
    assert "哨兵-LEAK-42" not in sent
    assert "我的重述" in sent  # 学习者输入本身作为 user 消息


def test_role_call_unknown_role(client, csrf_headers, fake):
    _project(client, csrf_headers)
    r = client.post("/api/v1/roles/nobody/call", headers=csrf_headers, json={})
    assert r.json()["code"] == "A030001", r.text


def test_role_call_without_key_returns_a040003(client, csrf_headers):
    pid = _project(client, csrf_headers)
    r = client.post(
        "/api/v1/roles/clerk/call", headers=csrf_headers, json={"learner_input": "转录"}
    )
    assert r.json()["code"] == "A040003", r.text
    # 无密钥时在创建 session 之前失败，不应留下孤立会话
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0


def test_role_call_adapter_failure(client, csrf_headers, fake):
    pid = _project(client, csrf_headers)
    fake.error = BizError("A040001", msg="上游不可用")
    r = client.post(
        "/api/v1/roles/critic/call", headers=csrf_headers, json={"learner_input": "我的解释"}
    )
    assert r.json()["code"] == "A040001", r.text
    assert db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"] == 0


def test_role_call_existing_session(client, csrf_headers, fake):
    pid = _project(client, csrf_headers)
    db.write(
        pid,
        "session",
        {"id": "sess-1", "unit_id": None, "started_at": db._now_utc()},
        "user",
    )
    r = client.post(
        "/api/v1/roles/clerk/call",
        headers=csrf_headers,
        json={"learner_input": "转录", "session_id": "sess-1"},
    )
    assert r.json()["code"] == "0", r.text
    assert r.json()["data"]["session_id"] == "sess-1"
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 1


def test_role_call_invalid_session(client, csrf_headers, fake):
    _project(client, csrf_headers)
    r = client.post(
        "/api/v1/roles/clerk/call",
        headers=csrf_headers,
        json={"session_id": "not-exist"},
    )
    assert r.json()["code"] == "C400001", r.text


def test_role_call_unit_not_found(client, csrf_headers, fake):
    pid = _project(client, csrf_headers)
    r = client.post(
        "/api/v1/roles/critic/call",
        headers=csrf_headers,
        json={"unit_id": "nope", "learner_input": "x"},
    )
    assert r.json()["code"] == "A020001", r.text
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0


def test_pick_default_model_without_key_a040003():
    with pytest.raises(BizError) as exc:
        registry.pick_default_model()
    assert exc.value.code == "A040003"


def test_zero_ddl_after_role_calls(client, csrf_headers, fake):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    client.post(
        "/api/v1/roles/topic_picker/call",
        headers=csrf_headers,
        json={"unit_id": uid, "learner_input": "帮我选题"},
    )
    assert db.count_user_objects(pid) == 16
