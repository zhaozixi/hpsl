"""HTTP 路由层断言（规格 AC2 / AC3 / AC7 / AC9 前端页面）。

- AC2：7 个 HTTP 接口可用，委托 projects.py，响应走统一封装 ok()/fail()。
- AC3：创建后首页列表出现、详情可查、配置读写合并生效。
- AC7：创建 / 切换 / 配置更新 经 db.write 审计，audit_log 必有条目。
"""

import pytest

from hpsl import db, projects
from hpsl.common.errors import BizError


def _create(client, csrf_headers, name="项目A", goal="目标g", belief=None):
    body = {"name": name, "goal": goal}
    if belief is not None:
        body["initial_belief"] = belief
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ---------------------------------------------------------------------------
# AC2：七接口 + backup 占位
# ---------------------------------------------------------------------------
def test_create_project_endpoint(client, csrf_headers):
    d = _create(client, csrf_headers)
    assert d["id"] and d["name"] == "项目A"
    # 重名 → A010002（HTTP 200，业务失败）
    r = client.post(
        "/api/v1/projects",
        headers=csrf_headers,
        json={"name": "项目A", "goal": "x"},
    )
    assert r.status_code == 200
    assert r.json()["code"] == "A010002"


def test_list_endpoint(client, csrf_headers):
    _create(client, csrf_headers)
    r = client.get("/api/v1/projects")
    assert r.status_code == 200
    assert isinstance(r.json()["data"], list)


def test_switch_endpoint(client, csrf_headers):
    d = _create(client, csrf_headers)
    r = client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    assert r.status_code == 200


def test_detail_endpoint(client, csrf_headers):
    d = _create(client, csrf_headers)
    r = client.get(f"/api/v1/projects/{d['id']}")
    assert r.status_code == 200
    assert r.json()["data"]["id"] == d["id"]


def test_initial_belief_endpoint(client, csrf_headers):
    d = _create(client, csrf_headers)
    r = client.put(
        f"/api/v1/projects/{d['id']}/initial-belief",
        headers=csrf_headers,
        json={"initial_belief": "我的初版认识"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["initial_belief"] == "我的初版认识"


def test_config_get_put_endpoint(client, csrf_headers):
    d = _create(client, csrf_headers)
    cfg0 = client.get(f"/api/v1/projects/{d['id']}/config")
    assert cfg0.status_code == 200
    assert cfg0.json()["data"]["pass_threshold"] == 0.7  # 全局默认
    cfg1 = client.put(
        f"/api/v1/projects/{d['id']}/config",
        headers=csrf_headers,
        json={"pass_threshold": 0.9, "budget_cap_usd": 20.0},
    )
    assert cfg1.status_code == 200
    cfg2 = client.get(f"/api/v1/projects/{d['id']}/config")
    assert cfg2.json()["data"]["pass_threshold"] == 0.9       # 项目覆盖生效
    assert cfg2.json()["data"]["budget_cap_usd"] == 20.0


def test_backup_implemented_200(client, csrf_headers):
    d = _create(client, csrf_headers)
    r = client.post(f"/api/v1/projects/{d['id']}/backup", headers=csrf_headers)
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    from pathlib import Path
    assert Path(r.json()["data"]["backup_path"]).exists()


# ---------------------------------------------------------------------------
# AC3：列表出现 / 详情可查 / 配置合并
# ---------------------------------------------------------------------------
def test_created_project_appears_in_list(client, csrf_headers):
    d = _create(client, csrf_headers, name="列表项")
    lst = client.get("/api/v1/projects").json()["data"]
    assert any(p["id"] == d["id"] and p["name"] == "列表项" for p in lst)


def test_config_merge_global_plus_override(client, csrf_headers):
    d = _create(client, csrf_headers)
    client.put(
        f"/api/v1/projects/{d['id']}/config",
        headers=csrf_headers,
        json={"pass_threshold": 0.88},
    )
    cfg = client.get(f"/api/v1/projects/{d['id']}/config").json()["data"]
    assert cfg["pass_threshold"] == 0.88          # 覆盖值
    assert cfg["budget_cap_usd"] == 10.0          # 仍取全局默认


# ---------------------------------------------------------------------------
# AC7：审计留痕
# ---------------------------------------------------------------------------
def test_audit_trail_on_write_endpoints(client, csrf_headers):
    d = _create(client, csrf_headers)
    pid = d["id"]
    client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers)
    client.put(f"/api/v1/projects/{pid}/config", headers=csrf_headers,
               json={"pass_threshold": 0.95})
    client.put(f"/api/v1/projects/{pid}/initial-belief", headers=csrf_headers,
               json={"initial_belief": "更新认识"})
    # 至少：create(1) + switch 记 last_active_at(1) + set_config(1) + update_belief(1) = 4
    n = db.query(pid, "SELECT count(*) c FROM audit_log WHERE table_name='project'")[0]["c"]
    assert n >= 4


# ---------------------------------------------------------------------------
# AC9 辅助：最小前端页面可渲染
# ---------------------------------------------------------------------------
def test_pages_render(client, csrf_headers):
    assert client.get("/").status_code == 200
    assert client.get("/projects/new").status_code == 200
    d = _create(client, csrf_headers)
    r = client.get(f"/projects/{d['id']}")
    assert r.status_code == 200
    assert "初版认识" in r.text
