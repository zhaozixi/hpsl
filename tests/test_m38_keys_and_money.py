"""M38：独立密钥入口（/keys）+ 费用美元/人民币双币展示。

验收口径：
- 密钥页不依赖激活项目就能打开（密钥是全局的），且只暴露 has_key / key_tail；
- 保存后立即回显「已配置 + 尾号」，页面任何位置都不出现完整密钥；
- 费用展示是「展示层折算」：记账与预算判断仍按美元，人民币只读配置的 usd_cny_rate；
- 汇率可在设置页调整，改完预算页/详情页立刻按新汇率折算。
"""

from __future__ import annotations

import pytest

from hpsl import projects as proj
from hpsl.common import money
from hpsl.common.dto import ConfigVO


# ---------------------------------------------------------------------------
# 1. 货币格式化（纯函数）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "usd, expected",
    [(0.0123, "$0.0123"), (0.002345, "$0.002345"), (10.0, "$10.00"), (0, "$0.00")],
)
def test_fmt_usd_digits(usd, expected):
    assert money.fmt_usd(usd) == expected


@pytest.mark.parametrize(
    "usd, rate, expected",
    [
        (0.0123, 7.2, "¥0.09"),
        (10.0, 7.2, "¥72.00"),
        (0.0001, 7.2, "¥0.0007"),
        (0.000001, 7.2, "¥0.000007"),
        (10.0, 7.0, "¥70.00"),
    ],
)
def test_fmt_cny(usd, rate, expected):
    assert money.fmt_cny(usd, rate) == expected


def test_fmt_both_and_rate_text():
    assert money.fmt_both(0.0123, 7.2) == "$0.0123（约 ¥0.09）"
    assert money.fmt_both(10.0, 7.2) == "$10.00（约 ¥72.00）"
    assert money.fmt_both(None) == "—"
    assert money.fmt_rate(7.2) == "1 美元 ≈ ¥7.20"


@pytest.mark.parametrize("bad", [None, 0, -1, "abc", "  "])
def test_rate_falls_back_to_default(bad):
    """脏汇率回落默认值，绝不让页面报错或显示 ¥0.00。"""
    assert money.fmt_cny(1.0, bad) == money.fmt_cny(1.0, money.DEFAULT_USD_CNY)


# ---------------------------------------------------------------------------
# 2. 汇率是项目配置项
# ---------------------------------------------------------------------------
def test_config_default_rate_and_roundtrip(client, csrf_headers):
    r = client.post("/api/v1/projects", headers=csrf_headers, json={"name": "汇率项目", "goal": "目标"})
    pid = r.json()["data"]["id"]
    assert ConfigVO().usd_cny_rate == 7.2

    cfg = proj.set_config(pid, {"usd_cny_rate": 7.35}, "user")
    assert cfg.usd_cny_rate == 7.35
    assert proj.get_effective_config(pid).usd_cny_rate == 7.35
    # JSON 往返后仍能读回（KNOWN_FIELDS 已包含新字段）
    assert ConfigVO.from_dict({"usd_cny_rate": 7.35}).usd_cny_rate == 7.35


# ---------------------------------------------------------------------------
# 3. 密钥页
# ---------------------------------------------------------------------------
def test_keys_page_needs_no_active_project(client):
    """没有激活项目也能进——这正是「独立入口」的意义。"""
    proj._active_pid = None
    r = client.get("/keys")
    assert r.status_code == 200
    for token in ["模型密钥", "DeepSeek", "千问 Qwen", "智谱 GLM", "未配置"]:
        assert token in r.text, f"缺少: {token}"


def test_nav_has_keys_entry(client):
    r = client.get("/keys")
    assert '<a href="/keys"' in r.text
    assert ">密钥</a>" in r.text


def test_keys_page_save_shows_tail_never_full_key(client, csrf_headers):
    secret = "sk-m38-test-1234567890abcd"
    r = client.put("/api/v1/settings/keys/deepseek", headers=csrf_headers, json={"key": secret})
    assert r.json()["code"] == "0"
    assert r.json()["data"]["key_tail"] == "abcd"

    page = client.get("/keys")
    assert page.status_code == 200
    assert "已配置" in page.text
    assert "abcd" in page.text
    assert secret not in page.text  # 完整密钥绝不出现（D1 §6.5）


def test_keys_page_locked_banner_when_vault_unavailable(client, monkeypatch):
    monkeypatch.delenv("HPSL_MASTER_PASSPHRASE", raising=False)
    r = client.get("/keys")
    assert r.status_code == 200
    assert "密钥库当前不可用" in r.text
    assert "HPSL_MASTER_PASSPHRASE" in r.text
    assert "disabled" in r.text  # 锁定状态下不给填，避免用户白提交一次


# ---------------------------------------------------------------------------
# 4. 费用双币展示
# ---------------------------------------------------------------------------
def test_budget_page_shows_cny_footnote(client, csrf_headers):
    d = client.post("/api/v1/projects", headers=csrf_headers, json={"name": "预算项目", "goal": "目标"}).json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers, json={})
    r = client.get("/budget")
    assert r.status_code == 200
    assert "1 美元 ≈ ¥7.20" in r.text
    assert "人民币" in r.text


def test_project_detail_shows_both_currencies(client, csrf_headers):
    d = client.post("/api/v1/projects", headers=csrf_headers, json={"name": "双币项目", "goal": "目标"}).json()["data"]
    pid = d["id"]
    proj.set_config(pid, {"budget_cap_usd": 10.0, "usd_cny_rate": 7.2}, "user")
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "$10.00（约 ¥72.00）" in r.text


def test_rate_change_moves_both_display(client, csrf_headers):
    d = client.post("/api/v1/projects", headers=csrf_headers, json={"name": "改汇率", "goal": "目标"}).json()["data"]
    pid = d["id"]
    proj.set_config(pid, {"budget_cap_usd": 10.0, "usd_cny_rate": 7.35}, "user")
    r = client.get(f"/projects/{pid}")
    assert "¥73.50" in r.text
    assert "1 美元 ≈ ¥7.35" in r.text


def test_settings_page_has_rate_field(client, csrf_headers):
    d = client.post("/api/v1/projects", headers=csrf_headers, json={"name": "设置汇率", "goal": "目标"}).json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers, json={})
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'id="usd_cny_rate"' in r.text
    assert "美元兑人民币汇率" in r.text
