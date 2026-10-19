"""M39：密钥清除 / 快照删除 / 角色→模型映射 / 交叉验证密钥预检。

验收口径：
- 密钥可以清除（幂等），且**锁定态也能清**——删除只需 unlink，不需要口令解密；
- 单份快照可以手动删除，文件名走与恢复相同的 basename + 白名单校验；
- 角色可以显式绑定模型；指定了没密钥的厂商时**报错而不是静默换模型**；
- 自动/显式交叉验证在创建会话之前预检密钥，失败时既不留会话也不产生费用。
"""

from __future__ import annotations

import pytest

from hpsl import config as cfg
from hpsl import db, projects as proj
from hpsl.common.dto import ConfigVO
from hpsl.common.errors import BizError
from hpsl.models import registry


def _project(client, csrf, name="M39 项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _unit(client, csrf, title="M39 单元"):
    r = client.post("/api/v1/units", headers=csrf, json={"title": title})
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _make_backup(client, csrf, name="M39 备份"):
    """建项目 + 切为激活 + 真导出一份快照（删除接口要求快照真实存在）。"""
    pid = _project(client, csrf, name)
    client.post(f"/api/v1/projects/{pid}/switch", headers=csrf, json={})
    db.backup_project(pid)
    names = db.list_backups(pid)
    assert names, "快照未生成"
    return pid, names


# ---------------------------------------------------------------------------
# 1. 密钥清除
# ---------------------------------------------------------------------------
def test_delete_key_removes_file_and_flips_status(client, csrf_headers):
    secret = "sk-m39-abcdef123456"
    assert client.put(
        "/api/v1/settings/keys/deepseek", headers=csrf_headers, json={"key": secret}
    ).json()["code"] == "0"
    assert (cfg.home() / "secrets" / "deepseek.fernet").exists()

    r = client.delete("/api/v1/settings/keys/deepseek", headers=csrf_headers)
    body = r.json()
    assert body["code"] == "0", r.text
    assert body["data"]["removed"] is True
    assert body["data"]["status"]["has_key"] is False
    assert not (cfg.home() / "secrets" / "deepseek.fernet").exists()
    # 页面上回到「未配置」，且不再显示尾号
    page = client.get("/keys").text
    assert "还没有填过密钥" in page


def test_delete_key_is_idempotent(client, csrf_headers):
    """本来就没有 → 仍 200，removed=False；不报错、不抛异常。"""
    r = client.delete("/api/v1/settings/keys/qwen", headers=csrf_headers)
    assert r.json()["code"] == "0"
    assert r.json()["data"]["removed"] is False
    assert r.json()["data"]["status"]["has_key"] is False


def test_delete_key_rejects_unknown_provider(client, csrf_headers):
    r = client.delete("/api/v1/settings/keys/nope", headers=csrf_headers)
    assert r.json()["code"] == "C400001"


def test_delete_key_works_while_vault_locked(client, csrf_headers, monkeypatch):
    """锁定态也能撤销密钥：删除不解密，否则填错一把就再也删不掉了。"""
    assert client.put(
        "/api/v1/settings/keys/zhipu", headers=csrf_headers, json={"key": "sk-locked-9999"}
    ).json()["code"] == "0"

    monkeypatch.delenv("HPSL_MASTER_PASSPHRASE", raising=False)
    assert "密钥库当前不可用" in client.get("/keys").text  # 确实处于锁定态

    r = client.delete("/api/v1/settings/keys/zhipu", headers=csrf_headers)
    assert r.json()["code"] == "0", r.text
    assert r.json()["data"]["removed"] is True
    assert not (cfg.home() / "secrets" / "zhipu.fernet").exists()


def test_keys_page_has_delete_button(client, csrf_headers):
    client.put("/api/v1/settings/keys/deepseek", headers=csrf_headers, json={"key": "sk-x-1234"})
    page = client.get("/keys").text
    assert 'data-action="delete-key"' in page
    assert "清除密钥" in page
    assert "确定清除这家厂商的密钥吗" in page


# ---------------------------------------------------------------------------
# 2. 快照删除
# ---------------------------------------------------------------------------
def test_delete_backup_removes_file_and_returns_oob(client, csrf_headers):
    pid, names = _make_backup(client, csrf_headers)
    first = cfg.project_dir(pid) / "backups"
    target = names[0]
    headers = {**csrf_headers, "HX-Request": "true"}
    r = client.delete(f"/api/v1/backup/{target}", headers=headers)
    assert r.status_code == 200, r.text
    assert "已删除快照" in r.text
    assert 'id="backup-list"' in r.text and 'hx-swap-oob="true"' in r.text
    assert 'id="backup-count"' in r.text
    assert not (first / target).exists()
    assert r.headers.get("HX-Refresh") is None
    # 删文件不是 DDL
    assert db.count_user_objects(pid) == 16


def test_delete_backup_json_returns_ok(client, csrf_headers):
    pid, names = _make_backup(client, csrf_headers)
    target = names[0]
    body = client.delete(f"/api/v1/backup/{target}", headers=csrf_headers).json()
    assert body["code"] == "0", body
    assert body["data"]["deleted"] == target


@pytest.mark.parametrize("bad", ["nope.db", "%2E%2E", "project.db", "a%5Cb.db"])
def test_delete_backup_rejects_non_whitelisted(client, csrf_headers, bad):
    _make_backup(client, csrf_headers)
    r = client.delete(f"/api/v1/backup/{bad}", headers=csrf_headers)
    assert r.json()["code"] == "C400001", bad


def test_delete_backup_never_touches_project_db(client, csrf_headers):
    """白名单来自 backups/ 扫描，项目主库不可能被列进来。"""
    pid, _ = _make_backup(client, csrf_headers)
    db_file = cfg.project_dir(pid) / "project.db"
    r = client.delete("/api/v1/backup/project.db", headers=csrf_headers)
    assert r.json()["code"] == "C400001"
    assert db_file.exists()


# ---------------------------------------------------------------------------
# 3. 角色 → 模型映射
# ---------------------------------------------------------------------------
def test_role_models_persist_and_resolve(client, csrf_headers):
    pid = _project(client, csrf_headers, "角色模型")
    client.put("/api/v1/settings/keys/deepseek", headers=csrf_headers, json={"key": "sk-ds-1111"})
    r = client.put(
        f"/api/v1/projects/{pid}/config",
        headers=csrf_headers,
        json={"role_models": {"critic": "deepseek-v4-pro"}},
    )
    assert r.json()["code"] == "0", r.text
    assert r.json()["data"]["role_models"] == {"critic": "deepseek-v4-pro"}

    # 写入后由项目配置解析出生效模型（而不是自动选择）
    effective = registry.resolve_role_model("critic", {"critic": "deepseek-v4-pro"})
    assert effective == "deepseek-v4-pro"
    # 未配置的角色仍走自动
    assert registry.resolve_role_model("clerk", {"critic": "deepseek-v4-pro"}) == "deepseek-flash"


def test_role_models_rejects_unregistered_model(client, csrf_headers):
    pid = _project(client, csrf_headers, "未注册模型")
    r = client.put(
        f"/api/v1/projects/{pid}/config",
        headers=csrf_headers,
        json={"role_models": {"critic": "no-such-model"}},
    )
    assert r.json()["code"] == "C400001", r.text
    assert "未注册" in r.json()["msg"]


def test_role_models_rejects_unknown_role(client, csrf_headers):
    pid = _project(client, csrf_headers, "未知角色")
    r = client.put(
        f"/api/v1/projects/{pid}/config",
        headers=csrf_headers,
        json={"role_models": {"principal": "deepseek-chat"}},
    )
    assert r.json()["code"] == "C400001"
    assert "未知角色" in r.json()["msg"]


def test_role_models_rejects_non_object(client, csrf_headers):
    pid = _project(client, csrf_headers, "类型错误")
    r = client.put(
        f"/api/v1/projects/{pid}/config",
        headers=csrf_headers,
        json={"role_models": ["deepseek-chat"]},
    )
    assert r.json()["code"] == "C400001"


def test_role_model_without_key_raises_instead_of_silent_fallback(client, csrf_headers):
    """指定了没密钥的厂商 → A040003，绝不悄悄换成别家模型（否则用户不知道钱花在哪）。"""
    client.put("/api/v1/settings/keys/deepseek", headers=csrf_headers, json={"key": "sk-ds-2222"})
    with pytest.raises(BizError) as exc:
        registry.resolve_role_model("critic", {"critic": "qwen-plus"})
    assert exc.value.code == "A040003"
    assert "密钥" in exc.value.msg and "自动选择" in exc.value.msg


def test_role_models_empty_value_means_auto(client, csrf_headers):
    pid = _project(client, csrf_headers, "清空映射")
    client.put("/api/v1/settings/keys/deepseek", headers=csrf_headers, json={"key": "sk-ds-3333"})
    client.put(
        f"/api/v1/projects/{pid}/config",
        headers=csrf_headers,
        json={"role_models": {"critic": "deepseek-v4-pro", "clerk": ""}},
    )
    r = client.put(
        f"/api/v1/projects/{pid}/config",
        headers=csrf_headers,
        json={"role_models": {}},
    )
    assert r.json()["data"]["role_models"] == {}


def test_describe_role_models_reports_error_without_raising():
    rows = registry.describe_role_models({"critic": "qwen-plus"})  # 无密钥
    critic = next(r for r in rows if r["role"] == "critic")
    assert critic["source"] == "explicit"
    assert critic["effective"] == ""
    assert "密钥" in critic["error"]
    assert len(rows) == len(registry.CONFIGURABLE_ROLES)


def test_config_vo_roundtrip_role_models():
    cfg_vo = ConfigVO.from_dict({"role_models": {"critic": "deepseek-v4-pro"}})
    assert cfg_vo.role_models == {"critic": "deepseek-v4-pro"}
    assert ConfigVO().role_models == {}


def test_settings_page_renders_role_selects(client, csrf_headers):
    pid = _project(client, csrf_headers, "设置页角色")
    client.put("/api/v1/settings/keys/deepseek", headers=csrf_headers, json={"key": "sk-ds-4444"})
    client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers, json={})
    page = client.get("/settings").text
    assert "角色用哪个模型" in page
    for role in registry.CONFIGURABLE_ROLES:
        assert f'data-role-model="{role}"' in page
    assert "deepseek-v4-pro" in page
    assert "未配置密钥" in page  # 没配密钥的厂商在下拉里标注出来


# ---------------------------------------------------------------------------
# 4. 交叉验证密钥预检
# ---------------------------------------------------------------------------
def test_auto_cross_check_without_keys_creates_nothing(client, csrf_headers, monkeypatch):
    pid = _project(client, csrf_headers, "无密钥交叉")
    uid = _unit(client, csrf_headers)
    client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers, json={})
    monkeypatch.setattr(registry, "provider_has_key", lambda provider: False)

    r = client.post(
        f"/api/v1/units/{uid}/answers/cross-check/auto",
        headers=csrf_headers,
        json={"question": "问题"},
    )
    assert r.json()["code"] == "C400001", r.text
    assert "密钥" in r.json()["msg"]
    assert "未创建会话" in r.json()["msg"]
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0
    assert db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"] == 0


def test_auto_cross_check_with_one_key_says_how_many(client, csrf_headers, monkeypatch):
    pid = _project(client, csrf_headers, "只有一家")
    uid = _unit(client, csrf_headers)
    client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers, json={})
    monkeypatch.setattr(registry, "provider_has_key", lambda provider: provider == "deepseek")

    r = client.post(
        f"/api/v1/units/{uid}/answers/cross-check/auto",
        headers=csrf_headers,
        json={"question": "问题"},
    )
    assert r.json()["code"] == "C400001"
    assert "只有 1 家" in r.json()["msg"]
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0


def test_explicit_cross_check_missing_key_blocks_before_session(client, csrf_headers, monkeypatch):
    """显式给了一家没密钥的模型：在建会话之前就失败，不留半截账。"""
    pid = _project(client, csrf_headers, "显式缺密钥")
    uid = _unit(client, csrf_headers)
    client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers, json={})
    monkeypatch.setattr(registry, "provider_has_key", lambda provider: provider == "deepseek")

    r = client.post(
        f"/api/v1/units/{uid}/answers/cross-check",
        headers=csrf_headers,
        json={"question": "问题", "model_ids": ["deepseek-chat", "qwen-plus"]},
    )
    assert r.json()["code"] == "A040003", r.text
    assert "qwen-plus" in r.json()["msg"]
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0
    assert db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"] == 0
