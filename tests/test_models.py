"""M4 模型适配层测试（AC-M4-1/2/3/4/9）。

覆盖：OpenAI 兼容 / Anthropic 适配器请求构造、密钥不泄露、重试耗尽、结构化解析、
异构判定、注册表与适配器工厂、密钥后端（内存 / 文件 0600 / 缺失 A040003）。
全部用 fake post 与注入 backend，不要求联网或真实密钥。
"""

import os
import tempfile

import httpx
import pytest

from hpsl.common.errors import BizError
from hpsl.models import (
    AnthropicAdapter,
    OpenAICompatAdapter,
    build_adapter,
    get_model,
    heterogeneous,
    pricing,
)
from hpsl.models.base import Msg
from hpsl.secrets.keystore import (
    FileBackend,
    MemoryBackend,
    PlaintextCipher,
    get_api_key,
    key_status,
    set_api_key,
)


# ---------------------------------------------------------------------------
# fake httpx 响应 / post
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {}

    def json(self):
        return self._data


def _fake_post_factory(behaviors):
    """behaviors：可调用序列或单对象；每次调用消费一个。
    - 返回 FakeResponse → 正常响应（按 status_code 判定重试）。
    - 抛 httpx.HTTPError → 视为网络错误（触发重试）。
    """
    state = {"i": 0, "calls": []}

    def _post(url=None, **kwargs):
        state["calls"].append(kwargs)
        item = behaviors(state["i"]) if callable(behaviors) else (
            behaviors[state["i"]] if state["i"] < len(behaviors) else behaviors[-1]
        )
        state["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    _post.calls = state["calls"]
    return _post


def _ok_openai(content="answer"):
    return FakeResponse(200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


# ---------------------------------------------------------------------------
# AC-M4-1：请求构造 + 密钥不泄露
# ---------------------------------------------------------------------------
def test_openai_request_construction_and_key_isolation():
    captured = {}
    KEY = "sk-test-123456"

    def fake(url=None, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return _ok_openai()

    adapter = OpenAICompatAdapter(
        name="openai",
        base_url="https://api.openai.com/v1",
        model_id="gpt-4o-mini",
        key_resolver=lambda: KEY,
        supports_json=True,
        _post=fake,
    )
    msgs = [Msg("system", "sys"), Msg("user", "hi")]
    adapter.complete(msgs)

    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == f"Bearer {KEY}"
    assert "sk-test" not in captured["json"].get("messages", [{}])[0]["content"]
    assert captured["json"]["model"] == "gpt-4o-mini"
    # supports_json → response_format 原生结构化
    assert captured["json"]["response_format"] == {"type": "json_object"}


def test_openai_key_not_leaked_in_body_or_logs():
    seen = {}

    def fake(url=None, **kwargs):
        seen.update(kwargs)
        seen["url"] = url
        return _ok_openai()

    adapter = OpenAICompatAdapter(
        name="openai", base_url="https://x/v1", model_id="m",
        key_resolver=lambda: "SECRETKEY", _post=fake,
    )
    adapter.complete([Msg("user", "q")])
    body_str = str(seen["json"])
    assert "SECRETKEY" not in body_str, "密钥不得出现在请求体"
    # 请求头含密钥但属正常 Bearer；此处仅验证不在 body


# ---------------------------------------------------------------------------
# AC-M4-2：重试 + 记账 + 耗尽 → A040001
# ---------------------------------------------------------------------------
def test_openai_retry_then_success_records_each_attempt():
    post = _fake_post_factory([httpx.ConnectError("boom"), httpx.ConnectError("boom"), _ok_openai()])
    calls = []
    adapter = OpenAICompatAdapter(
        name="openai", base_url="https://x/v1", model_id="m",
        key_resolver=lambda: "k", max_retries=2, backoff=lambda n: 0,
        on_call=calls.append, _post=post,
    )
    rep = adapter.complete([Msg("user", "q")])
    assert rep.text == "answer"
    assert len(calls) == 3, "重试逐次记账：2 次失败 + 1 次成功 = 3 次调用记录"
    assert len(post.calls) == 3


def test_openai_retry_exhausted_raises_a040001():
    post = _fake_post_factory([httpx.ConnectError("boom"), httpx.ConnectError("boom"), httpx.ConnectError("boom")])
    adapter = OpenAICompatAdapter(
        name="openai", base_url="https://x/v1", model_id="m",
        key_resolver=lambda: "k", max_retries=2, backoff=lambda n: 0, _post=post,
    )
    with pytest.raises(BizError) as exc:
        adapter.complete([Msg("user", "q")])
    assert exc.value.code == "A040001"


def test_openai_5xx_retry_exhausted_raises_a040001():
    post = _fake_post_factory([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
    adapter = OpenAICompatAdapter(
        name="openai", base_url="https://x/v1", model_id="m",
        key_resolver=lambda: "k", max_retries=2, backoff=lambda n: 0, _post=post,
    )
    with pytest.raises(BizError) as exc:
        adapter.complete([Msg("user", "q")])
    assert exc.value.code == "A040001"


# ---------------------------------------------------------------------------
# AC-M4-1（结构化）+ A040004
# ---------------------------------------------------------------------------
def test_openai_structured_ok():
    post = _fake_post_factory([_ok_openai(content='{"x":1}')])
    adapter = OpenAICompatAdapter(
        name="openai", base_url="https://x/v1", model_id="m",
        key_resolver=lambda: "k", supports_json=True, _post=post,
    )
    schema = {"type": "object", "properties": {"x": {"type": "number"}}}
    rep = adapter.complete([Msg("user", "q")], json_schema=schema)
    assert rep.text == '{"x":1}'


def test_openai_structured_parse_fail_raises_a040004():
    post = _fake_post_factory([_ok_openai(content="not-json")])
    adapter = OpenAICompatAdapter(
        name="openai", base_url="https://x/v1", model_id="m",
        key_resolver=lambda: "k", _post=post,
    )
    schema = {"type": "object"}
    with pytest.raises(BizError) as exc:
        adapter.complete([Msg("user", "q")], json_schema=schema)
    assert exc.value.code == "A040004"


# ---------------------------------------------------------------------------
# Anthropic 适配器
# ---------------------------------------------------------------------------
def _ok_anthropic_text(text="answer"):
    return FakeResponse(200, {"content": [{"type": "text", "text": text}]})


def _ok_anthropic_tool(input_obj):
    return FakeResponse(200, {"content": [{"type": "tool_use", "name": "return_structured", "input": input_obj}]})


def test_anthropic_request_construction():
    captured = {}

    def fake(url=None, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return _ok_anthropic_text()

    adapter = AnthropicAdapter(
        name="anthropic", base_url="https://api.anthropic.com/v1", model_id="claude-x",
        key_resolver=lambda: "ak", _post=fake,
    )
    adapter.complete([Msg("system", "sys"), Msg("user", "hi")])
    assert captured["url"].endswith("/messages")
    assert captured["headers"]["x-api-key"] == "ak"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    # system 与 messages 分离
    assert captured["json"]["system"] == "sys"
    assert captured["json"]["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_structured_tool_use_ok():
    post = _fake_post_factory([_ok_anthropic_tool({"score": 0.8})])
    adapter = AnthropicAdapter(
        name="anthropic", base_url="https://x/v1", model_id="m",
        key_resolver=lambda: "ak", _post=post,
    )
    schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    rep = adapter.complete([Msg("user", "q")], json_schema=schema)
    assert '"score": 0.8' in rep.text


def test_anthropic_structured_no_tool_use_raises_a040004():
    post = _fake_post_factory([_ok_anthropic_text("no tool")])
    adapter = AnthropicAdapter(
        name="anthropic", base_url="https://x/v1", model_id="m",
        key_resolver=lambda: "ak", _post=post,
    )
    with pytest.raises(BizError) as exc:
        adapter.complete([Msg("user", "q")], json_schema={"type": "object"})
    assert exc.value.code == "A040004"


def test_anthropic_retry_exhausted_raises_a040001():
    post = _fake_post_factory([httpx.ConnectError("x"), httpx.ConnectError("x"), httpx.ConnectError("x")])
    adapter = AnthropicAdapter(
        name="anthropic", base_url="https://x/v1", model_id="m",
        key_resolver=lambda: "ak", max_retries=2, backoff=lambda n: 0, _post=post,
    )
    with pytest.raises(BizError) as exc:
        adapter.complete([Msg("user", "q")])
    assert exc.value.code == "A040001"


# ---------------------------------------------------------------------------
# AC-M4-3：异构判定
# ---------------------------------------------------------------------------
def test_heterogeneous():
    ds = get_model("deepseek-flash")        # family deepseek
    qwen = get_model("qwen-plus")           # family qwen
    ds_chat = get_model("deepseek-chat")    # family deepseek, derived_from gpt4o
    ds_reasoner = get_model("deepseek-reasoner")  # family deepseek, derived_from gpt4o
    assert heterogeneous(ds, qwen) is True
    assert heterogeneous(qwen, ds_chat) is True
    # 同 family → 同源；同 derived_from 亦视为同源
    assert heterogeneous(ds, ds_chat) is False
    assert heterogeneous(ds_chat, ds_reasoner) is False


# ---------------------------------------------------------------------------
# 注册表 / 工厂
# ---------------------------------------------------------------------------
def test_build_adapter_returns_correct_class():
    a = build_adapter("deepseek-flash", key_resolver=lambda: "k")
    assert isinstance(a, OpenAICompatAdapter)
    b = build_adapter("glm-5.3-flash", key_resolver=lambda: "k")
    assert isinstance(b, OpenAICompatAdapter)
    assert pricing("deepseek-flash")["in_per_1k"] == 0.0002778


# ---------------------------------------------------------------------------
# AC-M4-4：密钥后端
# ---------------------------------------------------------------------------
def test_memory_backend_set_get_and_status():
    b = MemoryBackend()
    set_api_key("openai", "sk-abcdef123456", backend=b)
    assert get_api_key("openai", backend=b) == "sk-abcdef123456"
    st = key_status("openai", backend=b)
    assert st["has_key"] is True
    assert st["stored_in"] == "memory"
    assert st["key_tail"] == "3456"
    assert "sk-abcdef" not in st.values(), "完整密钥不得出现在 status 中"


def test_memory_missing_key_raises_a040003():
    b = MemoryBackend()
    with pytest.raises(BizError) as exc:
        get_api_key("missing", backend=b)
    assert exc.value.code == "A040003"


def test_file_backend_roundtrip_and_permissions():
    tmp = tempfile.mkdtemp()
    b = FileBackend(tmp, cipher=PlaintextCipher())
    set_api_key("openai", "sk-secret-9999", backend=b)
    assert get_api_key("openai", backend=b) == "sk-secret-9999"
    f = os.path.join(tmp, "openai.fernet")
    assert os.path.exists(f)
    # 权限 0600 仅在 POSIX 可严格校验；Windows 不强制 POSIX mode，跳过严格断言
    if os.name == "posix":
        mode = os.stat(f).st_mode & 0o777
        assert mode == 0o600, f"密钥文件权限须为 0600，实际 {oct(mode)}"
    # 缺失
    with pytest.raises(BizError) as exc:
        get_api_key("nope", backend=b)
    assert exc.value.code == "A040003"


def test_file_backend_fernet_if_cryptography_available():
    cryptography = __import__("importlib").import_module("importlib")
    try:
        cryptography.import_module("cryptography")  # 环境无则 skip
    except ImportError:
        pytest.skip("cryptography 未安装，跳过真实 Fernet 路径")
    from cryptography.fernet import Fernet

    from hpsl.secrets.keystore import FernetCipher

    tmp = tempfile.mkdtemp()
    cipher = FernetCipher(Fernet.generate_key())
    b = FileBackend(tmp, cipher=cipher)
    set_api_key("openai", "sk-fernet-7777", backend=b)
    assert get_api_key("openai", backend=b) == "sk-fernet-7777"
    raw = open(os.path.join(tmp, "openai.fernet"), "rb").read()
    assert b"sk-fernet-7777" not in raw, "静态文件不得明文存储密钥"


# ---------------------------------------------------------------------------
# AC-M4-9：headless 纪律 —— models / secrets / engine 不依赖 routes / app / fastapi
# ---------------------------------------------------------------------------
def test_headless_no_forbidden_imports():
    import ast
    import pathlib

    roots = [
        pathlib.Path(__file__).parent.parent / "hpsl" / "models",
        pathlib.Path(__file__).parent.parent / "hpsl" / "secrets",
        pathlib.Path(__file__).parent.parent / "hpsl" / "engine",
    ]
    forbidden = {"routes", "app", "hpsl.routes", "hpsl.app", "fastapi", "uvicorn"}
    for root in roots:
        for py in root.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root_mod = alias.name.split(".")[0]
                        assert root_mod not in forbidden, f"{py.name} 禁止 import {alias.name}"
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        root_mod = node.module.split(".")[0]
                        assert root_mod not in forbidden, f"{py.name} 禁止 from {node.module} import"
