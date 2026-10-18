"""M10 Web 接入 — REST 端点 / 账本总览 / 健康检查 / traceId（规格 §3.2.M10）。

覆盖：
- /healthz 返回 phase "M10"；/readyz 在临时 HPSL_HOME 下返回 200。
- /ledger 无激活项目 → 显示提示；有项目 → 四本账 + 四平面 + 软提示渲染。
- /api/v1/counts、/api/v1/counts/planes 返回 M9 结构。
- /api/v1/soft-prompts 返回列表并下发 sid cookie；dismiss 经写安全过滤该 id。
- 响应头携带 X-Trace-Id（traceId 中间件）。
"""

import pytest

from hpsl import projects as proj
from hpsl.routes import governance as governance_routes


def _create_and_switch(client, csrf_headers, name="M10项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


# ---------------------------------------------------------------------------
# 健康检查与 traceId
# ---------------------------------------------------------------------------
def test_healthz_phase_m10(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["phase"] == "M10"


def test_readyz_ok_under_temp_home(client, csrf_headers):
    # 项目库目录在创建项目后存在且可写；readiness 探针（纯探测，不自动建目录）
    _create_and_switch(client, csrf_headers)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_trace_id_header_present(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/api/v1/counts")
    assert r.status_code == 200
    assert "X-Trace-Id" in r.headers
    assert len(r.headers["X-Trace-Id"]) == 32


# ---------------------------------------------------------------------------
# 账本总览页面
# ---------------------------------------------------------------------------
def test_ledger_no_active_project(client):
    r = client.get("/ledger")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


def test_ledger_with_project_renders_four_planes(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/ledger")
    assert r.status_code == 200
    assert "账本总览" in r.text
    assert "四本账" in r.text
    assert "四平面度量" in r.text
    assert "治理软提示" in r.text
    assert "缺口" in r.text and "单元" in r.text
    assert "质量" in r.text and "风险" in r.text


# ---------------------------------------------------------------------------
# 治理度量 REST
# ---------------------------------------------------------------------------
def test_counts_endpoint(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/api/v1/counts")
    assert r.status_code == 200
    d = r.json()["data"]
    for k in (
        "pass_rate", "review_pass_rate", "calibration_gap",
        "l1_gap_sessions", "zero_gap_ratio", "unit_completion",
        "gap_clear_ratio", "l2_gap_backlog", "forced_close_ratio",
        "skipped_produce_ratio", "unresolved_disagreements", "isolated_node_ratio",
    ):
        assert k in d


def test_counts_planes_endpoint(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/api/v1/counts/planes")
    assert r.status_code == 200
    d = r.json()["data"]
    assert "counts" in d and "signals_by_plane" in d
    assert set(d["signals_by_plane"].keys()).issubset({"风险", "质量", "节奏", "进度"})


def test_soft_prompts_returns_list(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/api/v1/soft-prompts")
    assert r.status_code == 200
    # v1 不再下发 hpsl_sid cookie（持久化改落项目 config）；返回应为列表
    assert r.json()["code"] == "0"
    assert isinstance(r.json()["data"], list)


def test_dismiss_persists_in_config(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    first = client.get("/api/v1/soft-prompts")
    assert first.status_code == 200
    first_data = first.json()["data"]
    target = first_data[0]["id"] if first_data else "risk-forced-close"
    r = client.post(f"/api/v1/soft-prompts/{target}/dismiss", headers=csrf_headers)
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    again = client.get("/api/v1/soft-prompts")
    ids = [s["id"] for s in again.json()["data"]]
    assert target not in ids
    # 持久化于 project.config.dismissed_prompts（非进程内存）
    assert target in proj.get_raw_config(pid)["dismissed_prompts"]


def test_counts_requires_active_project(client):
    # 未激活项目 → A010001（HTTP 200，业务失败）
    r = client.get("/api/v1/counts")
    assert r.status_code == 200
    assert r.json()["code"] == "A010001"
