"""M25 安全红线对齐测试（P0-05 ~ P0-08）。

- P0-05 密钥：口令 + 每文件随机 salt + fail-fast（无口令即拒绝，不再明文降级）
- P0-06 限流：llm_freq（60s/10 次）与 sse_concurrency（并发闸）
- P0-07 Host/Origin 精确匹配（不再 `127.` 前缀放行）
- P0-08 关闭 /docs /redoc /openapi.json
"""

import os

import pytest

from hpsl import config, db
from hpsl.common.errors import BizError
from hpsl.models.base import Reply
from hpsl.routes import roles as roles_routes
from hpsl.routes import security
from hpsl.secrets import keystore
from hpsl.secrets.keystore import FileBackend


# ---------------------------------------------------------------------------
# P0-08：文档端点关闭
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_endpoints_disabled(client, path):
    r = client.get(path)
    assert r.status_code == 404, (path, r.status_code)


# ---------------------------------------------------------------------------
# P0-07：Host/Origin 精确匹配
# ---------------------------------------------------------------------------
def test_empty_host_whitelist_denies_all(monkeypatch):
    monkeypatch.setattr(config, "allowed_hosts", lambda: [])
    assert security.check_host("127.0.0.1:8765") is False
    assert security.check_host("") is False


def test_host_requires_exact_port(monkeypatch):
    assert security.check_host("127.0.0.1:8765") is True
    assert security.check_host("127.0.0.1:8766") is False
    assert security.check_host("127.0.0.1.attacker.com:8765") is False


def test_origin_requires_exact_origin():
    assert security.check_origin("http://127.0.0.1:8765") is True
    assert security.check_origin("http://localhost:8765") is True
    assert security.check_origin("http://127.0.0.1:9999") is False
    assert security.check_origin("http://0.0.0.1:8765") is False
    assert security.check_origin("https://127.0.0.1:8765") is True  # https 同一主机/端口
    assert security.check_origin("http://127.0.0.1.evil.com:8765") is False


# ---------------------------------------------------------------------------
# P0-05：密钥库（口令 + 随机 salt + fail-fast）
# ---------------------------------------------------------------------------
def test_file_backend_roundtrip_with_random_salt(tmp_path, monkeypatch):
    monkeypatch.setenv("HPSL_MASTER_PASSPHRASE", "unit-test-passphrase")
    b = FileBackend(tmp_path)
    b.set("deepseek", "sk-secret-1234")
    first = (tmp_path / "deepseek.fernet").read_bytes()
    assert first.startswith(b"HPSL2")
    assert b"sk-secret-1234" not in first
    assert b.get("deepseek") == "sk-secret-1234"

    b.set("deepseek", "sk-secret-1234")  # 同一明文再写一次 → 新随机 salt
    second = (tmp_path / "deepseek.fernet").read_bytes()
    assert first[5:21] != second[5:21], "每次写入应使用新的随机 salt"
    assert b.get("deepseek") == "sk-secret-1234"


def test_file_backend_locked_without_passphrase(tmp_path, monkeypatch):
    monkeypatch.delenv("HPSL_MASTER_PASSPHRASE", raising=False)
    b = FileBackend(tmp_path)
    with pytest.raises(BizError) as exc:
        b.set("deepseek", "sk-x")
    assert exc.value.code == "A040003"
    assert "HPSL_MASTER_PASSPHRASE" in exc.value.msg


def test_lock_state_reflects_passphrase(monkeypatch):
    try:
        import keyring  # noqa: F401
        pytest.skip("环境安装了 keyring，文件保险库锁定语义不适用")
    except ImportError:
        pass
    monkeypatch.delenv("HPSL_MASTER_PASSPHRASE", raising=False)
    assert keystore.lock_state()["locked"] is True
    monkeypatch.setenv("HPSL_MASTER_PASSPHRASE", "x")
    state = keystore.lock_state()
    assert state["locked"] is False
    assert state["stored_in"] == "fernet_file"


def test_settings_keys_reports_locked(client, csrf_headers, monkeypatch):
    monkeypatch.delenv("HPSL_MASTER_PASSPHRASE", raising=False)
    client.post("/api/v1/projects", headers=csrf_headers, json={"name": "锁项目", "goal": "g"})
    r = client.get("/api/v1/settings/keys")
    assert r.json()["data"]["locked"] is True
    assert "HPSL_MASTER_PASSPHRASE" in r.json()["data"]["lock_reason"]

    w = client.put(
        "/api/v1/settings/keys/deepseek", headers=csrf_headers, json={"key": "sk-should-fail"}
    )
    assert w.json()["code"] == "A040003"


# ---------------------------------------------------------------------------
# P0-06：限流（窗口按维度；并发闸）
# ---------------------------------------------------------------------------
def test_limiter_per_dimension_window():
    lim = security.SlidingWindowLimiter(
        {"llm_freq": 2, "route": 2}, window=1.0, windows={"llm_freq": 60.0}
    )
    assert lim.allow("llm_freq", "s", now=0.0) is True
    assert lim.allow("llm_freq", "s", now=1.0) is True
    assert lim.allow("llm_freq", "s", now=2.0) is False  # 60s 窗口内第 3 次
    assert lim.allow("llm_freq", "s", now=61.0) is True  # 0/1 已滑出窗口
    # route 用默认 1s 窗口（阈值 2 → 1s 内第 3 次才越限）
    assert lim.allow("route", "p", now=100.0) is True
    assert lim.allow("route", "p", now=100.5) is True
    assert lim.allow("route", "p", now=100.6) is False
    assert lim.allow("route", "p", now=102.0) is True


def test_concurrency_limiter():
    lim = security.ConcurrencyLimiter(2)
    assert lim.try_acquire() is True
    assert lim.try_acquire() is True
    assert lim.try_acquire() is False
    assert lim.active() == 2
    lim.release()
    assert lim.try_acquire() is True
    lim.release()
    lim.release()
    assert lim.active() == 0


class _FakeAdapter:
    name = "fake"
    supports_json = False

    def complete(self, messages, *, json_schema=None, stream=False):
        return Reply(
            text="ok",
            finish_reason="stop",
            model="deepseek-chat",
            raw={"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )


def test_role_call_hits_llm_freq_limit(client, csrf_headers, monkeypatch):
    client.post("/api/v1/projects", headers=csrf_headers, json={"name": "限流项目", "goal": "g"})
    adapter = _FakeAdapter()
    monkeypatch.setattr(roles_routes.registry, "pick_default_model", lambda role=None: "deepseek-chat")
    monkeypatch.setattr(roles_routes.registry, "build_adapter", lambda model_id, **kw: adapter)

    limits = dict(config.rate_limits())
    limits["llm_freq"] = 1
    security.rate_limiter.configure(limits, window=1.0, windows={"llm_freq": 60.0})
    try:
        r1 = client.post(
            "/api/v1/roles/clerk/call", headers=csrf_headers, json={"learner_input": "a"}
        )
        assert r1.json()["code"] == "0", r1.text
        sid = r1.json()["data"]["session_id"]
        r2 = client.post(
            "/api/v1/roles/clerk/call",
            headers=csrf_headers,
            json={"learner_input": "b", "session_id": sid},
        )
        assert r2.json()["code"] == "C429001", r2.text
    finally:
        security.rate_limiter.configure(
            config.rate_limits(), config.RATE_LIMIT_WINDOW, config.rate_windows()
        )


def test_sse_slot_release_on_exit():
    security.sse_limiter.configure(1)
    gen = security.hold_sse_slot()
    next(gen)
    assert security.sse_limiter.active() == 1
    with pytest.raises(BizError) as exc:
        next(security.hold_sse_slot())
    assert exc.value.code == "C429001"
    gen.close()
    assert security.sse_limiter.active() == 0
