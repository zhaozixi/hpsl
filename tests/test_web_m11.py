"""M11 批次1 验收：首次启动向导 /wizard + 密钥 REST 暴露 + test_connection 落地 + v0 封版。

- AC1：GET /wizard（激活项目）→ 200；含四步引导结构；无项目时显示「尚未选择项目」。
- AC2：PUT /settings/keys/{provider} → 200；密钥写入 HPSL_HOME/secrets/ 且 GET /settings/keys 返回 has_key=true + key_tail。
- AC3：GET /settings/keys 仅暴露 has_key/stored_in/key_tail，无完整密钥。
- AC4：POST /providers/{name}/test → 200；离线/异常 → ok=false 非崩溃；密钥缺失 → A040003。
- AC5：零 DDL（count_user_objects == 16）；密钥落文件、不触库。
- AC6：v0 封版——全量 pytest 零回归（本文件随全量运行）。
"""

from pathlib import Path

import httpx
import pytest

from hpsl import config as cfg
from hpsl import db
from hpsl import projects
from hpsl.models import connectivity


def _create_and_switch(client, csrf_headers, name="M11项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


# ---------------------------------------------------------------------------
# AC1：向导页结构 + 无项目提示
# ---------------------------------------------------------------------------
def test_wizard_page_has_four_steps(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/wizard")
    assert r.status_code == 200
    # 四步引导结构
    for sid in ("step-1", "step-2", "step-3", "step-4"):
        assert f'id="{sid}"' in r.text, f"缺少 {sid}"
    # Provider 下拉含已知 Provider
    assert 'value="openai"' in r.text
    assert 'value="anthropic"' in r.text
    # M22：国内大模型厂商应出现在下拉（覆盖 DeepSeek / 通义 / 智谱 / Kimi / 豆包 等）
    assert 'value="deepseek"' in r.text


def test_wizard_page_no_project_shows_note(client):
    # 不创建项目直接访问
    r = client.get("/wizard")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


# ---------------------------------------------------------------------------
# AC2 / AC3：密钥写入 + 状态脱敏
# ---------------------------------------------------------------------------
def test_set_key_writes_file_and_status(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    provider = "openai"
    secret_key = "sk-test-1234"
    r = client.put(
        f"/api/v1/settings/keys/{provider}",
        headers=csrf_headers,
        json={"key": secret_key},
    )
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["has_key"] is True
    assert d["key_tail"] == "1234"
    # 文件落地于 HPSL_HOME/secrets/
    fpath = cfg.home() / "secrets" / f"{provider}.fernet"
    assert fpath.exists(), f"密钥文件未落盘：{fpath}"


def test_get_keys_only_redacted(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    provider = "openai"
    client.put(
        f"/api/v1/settings/keys/{provider}",
        headers=csrf_headers,
        json={"key": "sk-secret-9999"},
    )
    r = client.get("/api/v1/settings/keys")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # AC3：仅脱敏字段，无完整密钥
    assert "sk-secret-9999" not in r.text
    prov = next(p for p in data["providers"] if p["provider"] == provider)
    assert prov["has_key"] is True
    assert prov["key_tail"] == "9999"
    assert set(prov.keys()) == {"provider", "has_key", "stored_in", "key_tail"}


def test_set_key_unknown_provider_rejected(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.put(
        "/api/v1/settings/keys/not_a_provider",
        headers=csrf_headers,
        json={"key": "x"},
    )
    # C400001：未知 Provider（业务错误码，HTTP 体 code 字段）
    assert r.json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# AC4：test_connection 端点（离线安全 + 密钥缺失）
# ---------------------------------------------------------------------------
def test_test_connection_offline_returns_false(client, csrf_headers, monkeypatch):
    _create_and_switch(client, csrf_headers)
    provider = "openai"
    client.put(
        f"/api/v1/settings/keys/{provider}",
        headers=csrf_headers,
        json={"key": "sk-test-1234"},
    )

    def fake_get(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", fake_get)
    r = client.post(f"/api/v1/providers/{provider}/test", headers=csrf_headers)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["ok"] is False
    assert "网络不可达" in d["detail"]


def test_test_connection_missing_key_returns_a040003(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.post("/api/v1/providers/openai/test", headers=csrf_headers)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["ok"] is False
    assert d["code"] == "A040003"


def test_test_connection_unknown_provider_rejected(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.post("/api/v1/providers/bogus/test", headers=csrf_headers)
    assert r.json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# AC5：零 DDL 守护（密钥落文件，不触 16 库对象）
# ---------------------------------------------------------------------------
def test_zero_ddl_after_key_ops(client, csrf_headers, monkeypatch):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]

    def fake_get(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", fake_get)
    client.put("/api/v1/settings/keys/openai", headers=csrf_headers, json={"key": "sk-x-1"})
    client.get("/api/v1/settings/keys")
    client.post("/api/v1/providers/openai/test", headers=csrf_headers)

    # 密钥落文件，库对象数不变
    assert (cfg.home() / "secrets" / "openai.fernet").exists()
    assert db.count_user_objects(pid) == 16


# ---------------------------------------------------------------------------
# 单元级：connectivity.test_connection 直接行为（离线安全 / 密钥缺失 / 拒绝未知）
# ---------------------------------------------------------------------------
def test_connectivity_missing_key(monkeypatch):
    monkeypatch.setattr(connectivity, "httpx", None)  # 防止触网
    res = connectivity.test_connection("openai", secrets_dir=cfg.home() / "secrets")
    assert res["ok"] is False
    assert res["code"] == "A040003"


def test_connectivity_offline(monkeypatch):
    def fake_get(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", fake_get)
    # 先写密钥（落临时目录）
    from hpsl.secrets.keystore import FileBackend
    from hpsl import secrets as secrets_mod

    backend = FileBackend(cfg.home() / "secrets")
    secrets_mod.set_api_key("openai", "sk-x", backend=backend)
    res = connectivity.test_connection("openai", secrets_dir=cfg.home() / "secrets")
    assert res["ok"] is False
    assert "网络不可达" in res["detail"]
