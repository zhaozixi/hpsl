"""M30 平台收尾验收：P2-08 启动口令 / P2-09 CSRF 签名 / P2-10 Anthropic 真流式。

- P2-08：`_ensure_passphrase_interactive()`（TTY 才询问；空/非交互打印提示；
  `HPSL_NO_PASSPHRASE_PROMPT=1` 静默跳过）；keyring 后端实测（fake 模块）。
- P2-09：CSRF token 为 `nonce.sig` HMAC 签名；篡改/缺失一律 False；
  cookie/header 自洽但签名伪造 → 403；重启后无效 cookie 由 GET 重新签发。
- P2-10：AnthropicAdapter.stream_complete() 解析 Messages SSE：
  content_block_delta → delta；message_start/message_delta usage → 归一化 usage；
  4xx/5xx → A040001；坏 JSON → A040004。
- 零 DDL：全程 count_user_objects == 16。
"""

import os
import sys
import types

import pytest

from hpsl import app as app_module
from hpsl import db
from hpsl.common.errors import BizError
from hpsl.models.anthropic import AnthropicAdapter
from hpsl.models.base import Msg
from hpsl.routes import security
from hpsl.secrets import keystore

ORIGIN = "http://127.0.0.1:8765"


# ---------------------------------------------------------------------------
# P2-09：CSRF 签名 token（单元层）
# ---------------------------------------------------------------------------
def test_csrf_token_signed_format_and_tamper():
    tok = security.new_csrf_token()
    nonce, sep, sig = tok.partition(".")
    assert sep == "."
    assert len(nonce) == 32 and len(sig) == 64  # 16B / 32B hex
    int(nonce, 16)  # 合法 hex
    int(sig, 16)
    assert security.verify_csrf_token(tok) is True

    # 每次签发 nonce 不同（不可预测、不可重放）
    assert security.new_csrf_token() != tok

    # 缺失 / 结构错误 / 篡改签名 / 篡改 nonce → False
    assert security.verify_csrf_token("") is False
    assert security.verify_csrf_token(None) is False
    assert security.verify_csrf_token(nonce) is False
    assert security.verify_csrf_token(f"{nonce}.") is False
    assert security.verify_csrf_token(f".{sig}") is False
    assert security.verify_csrf_token(f"{nonce}.{'0' * 64}") is False
    bad_sig = sig[:-1] + ("1" if sig[-1] != "1" else "0")
    assert security.verify_csrf_token(f"{nonce}.{bad_sig}") is False
    assert security.verify_csrf_token(f"{'0' * 32}.{sig}") is False


def test_http_forged_self_consistent_token_rejected(client):
    """cookie 与 header 相同但签名是伪造的 → 不能通过（旧随机双提交会放行）。"""
    client.get("/")  # 先获得合法 cookie，随后覆盖
    forged = f"{'ab' * 16}.{'cd' * 32}"
    client.cookies.set(security.CSRF_COOKIE, forged)
    r = client.post(
        "/api/v1/projects/x/switch",
        headers={"Origin": ORIGIN, "X-CSRF-Token": forged},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "C403001"


def test_http_valid_signed_token_write_passes(client, csrf_headers):
    """正常 GET → 签名 cookie → 写请求通过（既有 csrf_headers 行为不破）。"""
    tok = client.cookies.get(security.CSRF_COOKIE)
    assert security.verify_csrf_token(tok) is True
    r = client.post(
        "/api/v1/projects",
        headers=csrf_headers,
        json={"name": "M30 签名 CSRF", "goal": "g"},
    )
    assert r.status_code == 200
    assert r.json()["code"] == "0", r.text


def test_http_valid_but_mismatched_tokens_rejected(client, csrf_headers):
    """两个各自签名合法的 token 不同 → 双提交不匹配，仍 403。"""
    other = security.new_csrf_token()
    r = client.post(
        "/api/v1/projects/x/switch",
        headers={**csrf_headers, "X-CSRF-Token": other},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "C403001"


def test_get_reseeds_invalid_cookie_after_restart(client):
    """进程重启后 secret 变化：旧 cookie 无效，GET 必须重新签发有效 token。"""
    stale = f"{'0' * 32}.{'0' * 64}"
    r = client.get("/", headers={"Cookie": f"{security.CSRF_COOKIE}={stale}"})
    set_cookie = r.headers.get("set-cookie", "")
    assert f"{security.CSRF_COOKIE}=" in set_cookie
    fresh = set_cookie.split(f"{security.CSRF_COOKIE}=", 1)[1].split(";", 1)[0]
    assert fresh != stale
    assert security.verify_csrf_token(fresh) is True


# ---------------------------------------------------------------------------
# P2-08：keyring 后端实测（fake 模块）
# ---------------------------------------------------------------------------
def test_keyring_backend_roundtrip_with_fake_module(monkeypatch):
    fake = types.ModuleType("keyring")
    store: dict[tuple[str, str], str] = {}

    def set_password(service, username, password):
        store[(service, username)] = password

    def get_password(service, username):
        return store.get((service, username))

    fake.set_password = set_password
    fake.get_password = get_password
    monkeypatch.setitem(sys.modules, "keyring", fake)

    backend = keystore._try_keyring_backend()
    assert backend is not None
    assert backend.location() == "keyring"

    backend.set("anthropic", "sk-ant-1234")
    assert backend.get("anthropic") == "sk-ant-1234"
    assert store[("hpsl", "anthropic")] == "sk-ant-1234"
    assert backend.get("openai") is None  # 未写入 → None（不抛）

    # lock_state 认定已解锁；默认链路 set/get 走 keyring（不落 fernet 文件）
    assert keystore.lock_state()["locked"] is False
    keystore.set_api_key("anthropic", "sk-ant-5678")
    assert keystore.get_api_key("anthropic") == "sk-ant-5678"
    status = keystore.key_status("anthropic")
    assert status["has_key"] is True
    assert status["stored_in"] == "keyring"
    assert status["key_tail"] == "5678"


# ---------------------------------------------------------------------------
# P2-08：启动时交互式口令
# ---------------------------------------------------------------------------
def _locked():
    return {"locked": True, "stored_in": None, "reason": "unit-test"}


def test_passphrase_prompt_sets_env(monkeypatch):
    # 用 monkeypatch 记录基线，确保本用例直接写入的 env 在 teardown 被还原
    monkeypatch.setenv("HPSL_MASTER_PASSPHRASE", "before-test")
    monkeypatch.delenv("HPSL_NO_PASSPHRASE_PROMPT", raising=False)
    prompts: list[str] = []

    def fake_getpass(prompt):
        prompts.append(prompt)
        return "s3cret-pass"

    ok = app_module._ensure_passphrase_interactive(
        isatty=lambda: True, getpass_fn=fake_getpass, lock_state_fn=_locked
    )
    assert ok is True
    assert os.environ["HPSL_MASTER_PASSPHRASE"] == "s3cret-pass"
    assert prompts and "HPSL_MASTER_PASSPHRASE" in prompts[0]


def test_passphrase_empty_input_skips_and_prints(monkeypatch, capsys):
    monkeypatch.setenv("HPSL_MASTER_PASSPHRASE", "before-test")
    monkeypatch.delenv("HPSL_NO_PASSPHRASE_PROMPT", raising=False)
    ok = app_module._ensure_passphrase_interactive(
        isatty=lambda: True, getpass_fn=lambda p: "", lock_state_fn=_locked
    )
    assert ok is False
    assert os.environ["HPSL_MASTER_PASSPHRASE"] == "before-test"  # 空口令不覆盖
    assert "HPSL_MASTER_PASSPHRASE" in capsys.readouterr().err


def test_passphrase_non_interactive_prints_hint(monkeypatch, capsys):
    monkeypatch.delenv("HPSL_MASTER_PASSPHRASE", raising=False)
    monkeypatch.delenv("HPSL_NO_PASSPHRASE_PROMPT", raising=False)
    called: list[str] = []

    def fake_getpass(prompt):
        called.append(prompt)
        return "never-used"

    ok = app_module._ensure_passphrase_interactive(
        isatty=lambda: False, getpass_fn=fake_getpass, lock_state_fn=_locked
    )
    assert ok is False
    assert called == []  # 非交互绝不阻塞在 getpass
    assert "HPSL_MASTER_PASSPHRASE" not in os.environ
    assert "HPSL_MASTER_PASSPHRASE" in capsys.readouterr().err


def test_passphrase_prompt_disabled_by_env(monkeypatch):
    monkeypatch.delenv("HPSL_MASTER_PASSPHRASE", raising=False)
    monkeypatch.setenv("HPSL_NO_PASSPHRASE_PROMPT", "1")
    called: list[str] = []

    def fake_getpass(prompt):
        called.append(prompt)
        return "never-used"

    ok = app_module._ensure_passphrase_interactive(
        isatty=lambda: True, getpass_fn=fake_getpass, lock_state_fn=_locked
    )
    assert ok is False
    assert called == []
    assert "HPSL_MASTER_PASSPHRASE" not in os.environ


def test_passphrase_skipped_when_unlocked(monkeypatch):
    monkeypatch.delenv("HPSL_MASTER_PASSPHRASE", raising=False)
    called: list[str] = []
    ok = app_module._ensure_passphrase_interactive(
        isatty=lambda: True,
        getpass_fn=lambda p: called.append(p) or "never-used",
        lock_state_fn=lambda: {"locked": False, "stored_in": "keyring", "reason": None},
    )
    assert ok is False
    assert called == []


# ---------------------------------------------------------------------------
# P2-10：Anthropic 真流式
# ---------------------------------------------------------------------------
class _FakeStreamCtx:
    """最小化的 httpx.stream 上下文管理器替身。"""

    def __init__(self, lines, status: int = 200):
        self._lines = list(lines)
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        yield from self._lines


def _stream_adapter(fake_stream) -> AnthropicAdapter:
    return AnthropicAdapter(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        model_id="claude-x",
        key_resolver=lambda: "ak-unit",
        _stream=fake_stream,
    )


def test_anthropic_stream_complete_deltas_and_usage():
    seen = {}

    def fake_stream(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _FakeStreamCtx(
            [
                "event: message_start",
                'data: {"type":"message_start","message":{"id":"m1","usage":{"input_tokens":11,"output_tokens":1}}}',
                "event: content_block_start",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你"}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"好"}}',
                "event: message_delta",
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}',
                "event: message_stop",
                'data: {"type":"message_stop"}',
                "data: [DONE]",
            ]
        )

    events = list(_stream_adapter(fake_stream).stream_complete([Msg("user", "hi")]))

    assert [e["delta"] for e in events if "delta" in e] == ["你", "好"]
    usages = [e["usage"] for e in events if "usage" in e]
    assert usages[0] == {"prompt_tokens": 11, "completion_tokens": 1}
    assert usages[-1] == {"prompt_tokens": 11, "completion_tokens": 7}  # 合并后完整
    assert seen["args"][0] == "POST"
    assert seen["args"][1].endswith("/messages")
    assert seen["kwargs"]["json"]["stream"] is True


def test_anthropic_stream_http_error_maps_a040001():
    adapter = _stream_adapter(lambda *a, **k: _FakeStreamCtx([], status=429))
    with pytest.raises(BizError) as exc:
        list(adapter.stream_complete([Msg("user", "hi")]))
    assert exc.value.code == "A040001"


def test_anthropic_stream_bad_json_maps_a040004():
    adapter = _stream_adapter(lambda *a, **k: _FakeStreamCtx(["data: {not-json"]))
    with pytest.raises(BizError) as exc:
        list(adapter.stream_complete([Msg("user", "hi")]))
    assert exc.value.code == "A040004"


# ---------------------------------------------------------------------------
# 零 DDL
# ---------------------------------------------------------------------------
def test_zero_ddl_after_m30_paths(client, csrf_headers):
    r = client.post(
        "/api/v1/projects",
        headers=csrf_headers,
        json={"name": "M30 零 DDL", "goal": "g"},
    )
    assert r.json()["code"] == "0", r.text
    pid = r.json()["data"]["id"]
    client.get("/")  # 触发 CSRF cookie 重签路径
    assert db.count_user_objects(pid) == 16
