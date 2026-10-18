"""M32：接入智谱 GLM-5.3-Flash 与千问 Qwen3.8-Flash（含多模态透传）。"""

from hpsl import db
from hpsl.models import registry
from hpsl.models.base import Msg
from hpsl.routes import providers as providers_routes


class _CaptureAdapter:
    name = "fake"

    def __init__(self):
        self.seen = None

    def complete(self, messages, *, json_schema=None, stream=False):
        from hpsl.models.base import Reply

        self.seen = messages
        return Reply(text="ok", finish_reason="stop", model="glm-5.3-flash", raw={"usage": {}})


def _project(client, csrf, name="双模型项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    return r.json()["data"]["id"]


def _patch(monkeypatch, adapter):
    monkeypatch.setattr(providers_routes.registry, "build_adapter", lambda model_id, **kw: adapter)


def test_glm_flash_registered_and_priced():
    m = registry.get_model("glm-5.3-flash")
    assert m.provider == "zhipu"
    assert m.family == "glm"
    p = registry.pricing("glm-5.3-flash")
    assert p["in_per_1k"] > 0 and p["out_per_1k"] > 0
    assert 0.0001 < p["in_per_1k"] < 0.0002
    assert 0.0003 < p["out_per_1k"] < 0.0005


def test_qwen_flash_registered_and_priced():
    m = registry.get_model("qwen3.8-flash")
    assert m.provider == "qwen"
    assert m.family == "qwen"
    assert registry.default_base_url("qwen") == "https://maas.qianwenaiapi.com/compatible-mode/v1"
    p = registry.pricing("qwen3.8-flash")
    assert p["in_per_1k"] > 0 and p["out_per_1k"] > 0
    assert 0.0003 < p["out_per_1k"] < 0.0005


def test_new_models_are_same_source_as_their_vendor():
    assert registry.heterogeneous(
        registry.get_model("glm-5.3-flash"), registry.get_model("glm-4-plus")
    ) is False
    assert registry.heterogeneous(
        registry.get_model("qwen3.8-flash"), registry.get_model("qwen-plus")
    ) is False


def test_multimodal_content_passthrough(client, csrf_headers, monkeypatch):
    pid = _project(client, csrf_headers)
    adapter = _CaptureAdapter()
    _patch(monkeypatch, adapter)
    content = [
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        {"type": "text", "text": "图里有什么？"},
    ]
    r = client.post(
        "/api/v1/providers/zhipu/chat",
        headers=csrf_headers,
        json={"model": "glm-5.3-flash", "messages": [{"role": "user", "content": content}]},
    )
    assert r.json()["code"] == "0", r.text
    assert adapter.seen and isinstance(adapter.seen[0].content, list)
    assert adapter.seen[0].content[0]["type"] == "image_url"
    assert db.count_user_objects(pid) == 16


def test_invalid_content_rejected(client, csrf_headers):
    _project(client, csrf_headers)
    for bad in (123, None, [], [{"no_type": 1}], ["text"]):
        r = client.post(
            "/api/v1/providers/zhipu/chat",
            headers=csrf_headers,
            json={"messages": [{"role": "user", "content": bad}]},
        )
        assert r.json()["code"] == "C400001", (bad, r.text)


def test_multimodal_message_type_is_generic():
    msg = Msg("user", [{"type": "text", "text": "hi"}])
    assert isinstance(msg.content, list)


def test_4xx_maps_to_provider_error_with_detail():
    """4xx 不再误报 A040004（结构异常），而是 A040001 + 上游 error 文本。"""
    import pytest

    from hpsl.common.errors import BizError
    from hpsl.models.openai_compat import OpenAICompatAdapter

    class _Resp:
        status_code = 400
        text = ""

        def json(self):
            return {"error": {"code": "1210", "message": "图片输入格式/解析错误"}}

    adapter = OpenAICompatAdapter(
        name="zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model_id="glm-5.3-flash",
        key_resolver=lambda: "sk-x",
        _post=lambda *a, **k: _Resp(),
        max_retries=0,
    )
    with pytest.raises(BizError) as exc:
        adapter.complete([Msg("user", "描述图片")])
    assert exc.value.code == "A040001"
    assert "图片输入格式" in exc.value.msg
    assert "400" in exc.value.msg
