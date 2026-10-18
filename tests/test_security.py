"""安全基线断言（规格 AC4 / AC5 / AC6）。

- AC4：Host 非白名单 → 400；写请求缺 Origin/Origin 非回环 → 403；CSRF 缺失/不符 → 403。
- AC5：safe_path 越界 → C400002（复用 M0 断言思路，并新增 pid 穿越防护）。
- AC6：五维限流越限 → 429（用 tight_ratelimit 收敛阈值验证机制）。
"""

import pytest

from hpsl import db, projects
from hpsl.common.errors import BizError
from hpsl.routes import security


# ---------------------------------------------------------------------------
# AC4：Host / Origin / CSRF
# ---------------------------------------------------------------------------
def test_host_not_whitelisted_returns_400(client):
    r = client.get("/", headers={"Host": "evil.example.com"})
    assert r.status_code == 400
    assert r.json()["code"] == "C400003"


def test_write_missing_origin_returns_403(client, csrf_headers):
    # 有合法 CSRF 但无 Origin → 403
    h = {k: v for k, v in csrf_headers.items() if k.lower() != "origin"}
    r = client.post("/api/v1/projects/x/switch", headers=h)
    assert r.status_code == 403
    assert r.json()["code"] == "C403001"


def test_write_bad_origin_returns_403(client, csrf_headers):
    h = {**csrf_headers, "Origin": "http://attacker.com"}
    r = client.post("/api/v1/projects/x/switch", headers=h)
    assert r.status_code == 403
    assert r.json()["code"] == "C403001"


def test_write_missing_csrf_returns_403(client):
    # 未发起 GET → 无 CSRF cookie，也未带 header → 403
    r = client.post(
        "/api/v1/projects/x/switch",
        headers={"Origin": "http://127.0.0.1:8765"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "C403001"


def test_write_csrf_mismatch_returns_403(client, csrf_headers):
    h = {**csrf_headers, "X-CSRF-Token": "deadbeefdeadbeef"}
    r = client.post("/api/v1/projects/x/switch", headers=h)
    assert r.status_code == 403
    assert r.json()["code"] == "C403001"


# ---------------------------------------------------------------------------
# AC5：safe_path 越界 → C400002
# ---------------------------------------------------------------------------
def test_safe_path_rel_traversal_rejected():
    vo = projects.create_project("safe_path_rel", "越界检测")
    # 相对穿越：../../escape 应被拒绝
    with pytest.raises(BizError) as exc:
        db.safe_path(vo.id, "..", "..", "escape.txt")
    assert exc.value.code == "C400002"


def test_safe_path_pid_traversal_rejected():
    # pid 含 ../ 穿越：解析后不在 projects_root 内 → 拒绝
    with pytest.raises(BizError) as exc:
        db.safe_path("../../etc", "project.db")
    assert exc.value.code == "C400002"


# ---------------------------------------------------------------------------
# AC6：限流越限 → 429
# ---------------------------------------------------------------------------
def test_rate_limit_returns_429_read_route(client, tight_ratelimit):
    # 读接口亦受限流；同一路由连续超出 route 阈值(=1) → 429 C429001
    r1 = client.get("/api/v1/projects")
    assert r1.status_code != 429
    r2 = client.get("/api/v1/projects")
    assert r2.status_code == 429
    assert r2.json()["code"] == "C429001"


def test_rate_limit_returns_429_write_route(client, csrf_headers, tight_ratelimit):
    # 写接口：创建真实项目后，对同一 switch 路由连续两次 → 第 2 次越限 429
    body = {"name": "限流写", "goal": "g"}
    c = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    pid = c.json()["data"]["id"]
    h = dict(csrf_headers)
    r1 = client.post(f"/api/v1/projects/{pid}/switch", headers=h)
    assert r1.status_code != 429
    r2 = client.post(f"/api/v1/projects/{pid}/switch", headers=h)
    assert r2.status_code == 429
    assert r2.json()["code"] == "C429001"


def test_rate_limiter_unit():
    # 单元层直接验证滑动窗口限流器：阈值 2，第 3 次越限
    lim = security.SlidingWindowLimiter({"x": 2}, window=1.0)
    assert lim.allow("x", "k") is True
    assert lim.allow("x", "k") is True
    assert lim.allow("x", "k") is False


def test_security_helpers_unit():
    # 单元层直接验证各判定函数（不依赖 HTTP）
    assert security.check_host("127.0.0.1:8765") is True
    assert security.check_host("localhost:8765") is True
    assert security.check_host("evil.com") is False
    assert security.check_origin("http://127.0.0.1:8765") is True
    assert security.check_origin("http://attacker.com") is False
    assert security.check_origin("") is False
    tok = security.new_csrf_token()
    assert len(tok) == 64  # 32B hex
