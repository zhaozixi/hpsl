"""M10B 验收（规格 §3 AC1–AC4）：/settings 配置页 + 软提示关闭持久化。

- AC1：GET /settings（激活项目）→ 200；表单含 §1.3 全部字段且预填当前生效值。
- AC2：提交配置 → PUT /api/v1/projects/{pid}/config → 200；刷新 /settings 新值生效。
- AC3：dismiss 后，独立请求（非进程内存/cookie）仍过滤该提示，且落盘 config。
- AC4：GET /soft-prompts 与 /ledger 均按 config.dismissed_prompts 过滤。
"""

import pytest

from hpsl import projects as proj


def _create_and_switch(client, csrf_headers, name="M10B项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


# ---------------------------------------------------------------------------
# AC1：设置页渲染（字段齐全 + 预填）
# ---------------------------------------------------------------------------
def test_settings_no_active_project(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


def test_settings_renders_all_fields_prefilled(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    r = client.get("/settings")
    assert r.status_code == 200
    # 项目名
    assert d["name"] in r.text
    # §1.3 全部字段的 input id 均存在
    for fid in (
        "pass_threshold", "pass_hi", "pass_lo", "l1_gap_limit", "session_minutes",
        "zero_gap_max", "forced_close_max", "skipped_produce_max",
        "budget_cap_usd", "review_intervals",
    ):
        assert f'id="{fid}"' in r.text
    # 预填当前生效值（默认 pass_threshold=0.7 -> 渲染 0.7）
    assert 'value="0.7"' in r.text
    # review_intervals 默认 (2,7,21) 逗号分隔预填
    assert "2,7,21" in r.text


# ---------------------------------------------------------------------------
# AC2：配置提交经 PUT config 落盘并生效
# ---------------------------------------------------------------------------
def test_settings_put_config_updates_effective(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    patch = {
        "pass_threshold": 0.9,
        "session_minutes": 40,
        "review_intervals": [3, 14, 30],
    }
    r = client.put(
        f"/api/v1/projects/{pid}/config",
        headers={**csrf_headers, "Content-Type": "application/json"},
        json=patch,
    )
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "0"
    # 生效配置反映新值
    cfg = proj.get_effective_config(pid)
    assert cfg.pass_threshold == 0.9
    assert cfg.session_minutes == 40
    assert cfg.review_intervals == (3, 14, 30)
    # 刷新 /settings 页面预填新值
    page = client.get("/settings")
    assert 'value="0.9"' in page.text
    assert 'value="40"' in page.text
    assert "3,14,30" in page.text


# ---------------------------------------------------------------------------
# AC3：dismiss 持久化（跨请求，非进程内存）
# ---------------------------------------------------------------------------
def test_dismiss_persists_across_requests(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    prompts = client.get("/api/v1/soft-prompts").json()["data"]
    target = prompts[0]["id"] if prompts else "risk-forced-close"
    r = client.post(f"/api/v1/soft-prompts/{target}/dismiss", headers=csrf_headers)
    assert r.status_code == 200
    # 第二次独立请求（不依赖 cookie / 进程内存）仍过滤
    again = client.get("/api/v1/soft-prompts").json()["data"]
    assert all(s["id"] != target for s in again)
    # 落盘校验：已写入 project.config.dismissed_prompts
    assert target in proj.get_raw_config(pid)["dismissed_prompts"]


# ---------------------------------------------------------------------------
# AC4：/soft-prompts 与 /ledger 同口径按 config 过滤
# ---------------------------------------------------------------------------
def test_both_endpoints_filter_by_persisted_dismissal(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    prompts = client.get("/api/v1/soft-prompts").json()["data"]
    target = prompts[0]["id"] if prompts else "risk-forced-close"
    client.post(f"/api/v1/soft-prompts/{target}/dismiss", headers=csrf_headers)

    # /soft-prompts 过滤
    sp_ids = [s["id"] for s in client.get("/api/v1/soft-prompts").json()["data"]]
    assert target not in sp_ids

    # /ledger 页面同样过滤（signal div 的 id 形如 prompt-<id>）
    ledger = client.get("/ledger")
    assert ledger.status_code == 200
    assert f"prompt-{target}" not in ledger.text


# ---------------------------------------------------------------------------
# 零 DDL 守卫生成：get_raw_config 仅读，绝不新增库对象
# ---------------------------------------------------------------------------
def test_get_raw_config_includes_extra_keys(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    raw = proj.get_raw_config(pid)
    assert isinstance(raw, dict)
    # dismissed_prompts 为 extra 键（非 ConfigVO 已知字段），get_raw_config 应原样保留
    assert "name" in raw  # 创建时落库的 name 键
    # 关闭后再读应包含 dismissed_prompts
    client.post(f"/api/v1/soft-prompts/risk-forced-close/dismiss", headers=csrf_headers)
    assert "dismissed_prompts" in proj.get_raw_config(pid)
