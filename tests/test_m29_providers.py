"""M29 / P1-03：Provider 直连（非流式 + SSE 流式）。"""

from hpsl import db
from hpsl.models.base import Reply
from hpsl.routes import providers as providers_routes


class _FakeAdapter:
    name = "fake"
    supports_json = False

    def __init__(self, text="回答内容", deltas=("你", "好")):
        self.text = text
        self.deltas = deltas
        self.stream_calls = 0

    def complete(self, messages, *, json_schema=None, stream=False):
        return Reply(
            text=self.text,
            finish_reason="stop",
            model="deepseek-flash",
            raw={"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )


class _StreamAdapter(_FakeAdapter):
    def stream_complete(self, messages, *, json_schema=None):
        self.stream_calls += 1
        for d in self.deltas:
            yield {"delta": d}
        yield {"usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def _project(client, csrf, name="直连项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    return r.json()["data"]["id"]


def _patch(monkeypatch, adapter):
    monkeypatch.setattr(providers_routes.registry, "build_adapter", lambda model_id, **kw: adapter)


def test_chat_non_stream_records_cost_and_turns(client, csrf_headers, monkeypatch):
    pid = _project(client, csrf_headers)
    _patch(monkeypatch, _FakeAdapter("这是直连回答"))
    r = client.post(
        "/api/v1/providers/deepseek/chat",
        headers=csrf_headers,
        json={"messages": [{"role": "user", "content": "你好"}]},
    )
    assert r.json()["code"] == "0", r.text
    d = r.json()["data"]
    assert d["text"] == "这是直连回答"
    assert d["model"] == "deepseek-flash"
    assert d["tokens_in"] == 10 and d["tokens_out"] == 5
    assert d["cost_usd"] > 0

    assert db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"] == 1
    turns = db.list_turns(pid, session_id=d["session_id"])
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["content"] == "这是直连回答"


def test_chat_unknown_provider(client, csrf_headers):
    _project(client, csrf_headers)
    r = client.post(
        "/api/v1/providers/not-a-provider/chat",
        headers=csrf_headers,
        json={"messages": [{"role": "user", "content": "x"}]},
    )
    assert r.json()["code"] == "C400001", r.text


def test_chat_messages_validation(client, csrf_headers):
    _project(client, csrf_headers)
    for body in (
        {},
        {"messages": []},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": "user", "content": 123}]},
    ):
        r = client.post("/api/v1/providers/deepseek/chat", headers=csrf_headers, json=body)
        assert r.json()["code"] == "C400001", (body, r.text)


def test_chat_invalid_session(client, csrf_headers):
    _project(client, csrf_headers)
    r = client.post(
        "/api/v1/providers/deepseek/chat",
        headers=csrf_headers,
        json={"messages": [{"role": "user", "content": "x"}], "session_id": "nope"},
    )
    assert r.json()["code"] == "C400001", r.text


def test_chat_stream_sse(client, csrf_headers, monkeypatch):
    pid = _project(client, csrf_headers)
    adapter = _StreamAdapter()
    _patch(monkeypatch, adapter)
    with client.stream(
        "POST",
        "/api/v1/providers/deepseek/chat/stream",
        headers=csrf_headers,
        json={"messages": [{"role": "user", "content": "流式测试"}]},
    ) as resp:
        body = "".join(resp.iter_text())
    assert '"delta": "你"' in body
    assert '"delta": "好"' in body
    assert '"done": true' in body
    assert adapter.stream_calls == 1

    assert db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"] == 1
    row = db.query(pid, "SELECT tokens_in, tokens_out FROM llm_call")[0]
    assert row["tokens_in"] == 10 and row["tokens_out"] == 2
    turns = db.list_turns(pid)
    assert [t["content"] for t in turns] == ["流式测试", "你好"]


def test_chat_stream_fallback_without_stream_support(client, csrf_headers, monkeypatch):
    _project(client, csrf_headers)
    _patch(monkeypatch, _FakeAdapter("不支持流式的回答"))
    with client.stream(
        "POST",
        "/api/v1/providers/deepseek/chat/stream",
        headers=csrf_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    ) as resp:
        body = "".join(resp.iter_text())
    assert "不支持流式的回答" in body
    assert '"done": true' in body


def test_zero_ddl_after_provider_chat(client, csrf_headers, monkeypatch):
    pid = _project(client, csrf_headers)
    _patch(monkeypatch, _FakeAdapter())
    client.post(
        "/api/v1/providers/deepseek/chat",
        headers=csrf_headers,
        json={"messages": [{"role": "user", "content": "x"}]},
    )
    assert db.count_user_objects(pid) == 16


# ---------------------------------------------------------------------------
# 适配器层：httpx.stream 必须收到 method + url（真实联调暴露）
# ---------------------------------------------------------------------------
class _FakeStreamCtx:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"he"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"llo"}}]}'
        yield 'data: {"usage":{"prompt_tokens":3,"completion_tokens":2}}'
        yield "data: [DONE]"


def test_openai_stream_complete_passes_method_and_url(monkeypatch):
    from hpsl.models import openai_compat
    from hpsl.models.base import Msg

    seen = {}

    def fake_stream(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _FakeStreamCtx()

    adapter = openai_compat.OpenAICompatAdapter(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        model_id="deepseek-flash",
        key_resolver=lambda: "sk-unit",
        _stream=fake_stream,
    )
    events = list(adapter.stream_complete([Msg("user", "hi")]))
    assert events[0] == {"delta": "he"}
    assert events[1] == {"delta": "llo"}
    assert events[2] == {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    assert seen["args"][0] == "POST"
    assert seen["args"][1] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["kwargs"]["json"]["stream"] is True
