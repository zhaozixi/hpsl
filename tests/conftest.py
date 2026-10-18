"""M1 测试公共设施（规格 §8：全部用临时 HPSL_HOME）。

- 强制临时 HPSL_HOME，避免污染真实 ~/.hpsl。
- 放行 TestClient 默认 Host=testserver（生产仅回环）；放大限流阈值，避免集成测试误伤 429。
- 提供 client（TestClient）、csrf_headers（GET 触发 CSRF cookie + Origin/Token 头）、tight_ratelimit。
"""

import os
import tempfile

# 临时 HPSL_HOME（全量测试隔离）
os.environ["HPSL_HOME"] = tempfile.mkdtemp(prefix="hpsl-test-home-")
# 测试用 Host 白名单（TestClient 默认 Host=testserver）；仅测试放宽，生产配置仍为回环
os.environ["HPSL_ALLOWED_HOSTS"] = "127.0.0.1,localhost,testserver"
# 放大限流阈值，避免集成测试因内置限流被 429；AC6 通过 tight_ratelimit 单独收敛验证
os.environ["HPSL_RATE_LIMITS"] = (
    "process=100000,route=100000,llm_freq=100000,sse_concurrency=100000,write_freq=100000"
)
# M25：文件保险库需要口令（默认链路不再允许明文降级）。测试用固定口令；
# 验证"锁定"行为的用例会 monkeypatch.delenv 临时移除。
os.environ.setdefault("HPSL_MASTER_PASSPHRASE", "hpsl-test-passphrase")

import pytest
from fastapi.testclient import TestClient

from hpsl.app import app
from hpsl.routes import security

_HUGE_LIMITS = {
    "process": 100000, "route": 100000, "llm_freq": 100000,
    "sse_concurrency": 100000, "write_freq": 100000,
}


@pytest.fixture(autouse=True)
def _reset_ratelimit():
    security.rate_limiter.reset()
    yield
    security.rate_limiter.reset()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def csrf_headers(client):
    """先 GET 触发中间件下发 CSRF cookie，再返回写请求必需的 Origin + X-CSRF-Token 头。

    cookie 由 TestClient 自动随后续请求回传；header 提供 X-CSRF-Token 与 cookie 匹配。
    """
    client.get("/")
    token = client.cookies.get("hpsl_csrf")
    return {"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": token}


@pytest.fixture
def tight_ratelimit():
    """收敛到极小阈值用于验证越限 → 429；仅收紧 route 维度（其余保持大值），teardown 还原。

    仅收紧 route 可避免「创建项目已消耗 process/write_freq 额度」导致后续断言失真，
    使「同一路由连续越限 → 429」的判定稳定可复现。
    """
    security.rate_limiter.configure(
        {"process": 100000, "route": 1, "llm_freq": 100000, "sse_concurrency": 100000, "write_freq": 100000},
        window=1.0,
    )
    yield
    security.rate_limiter.configure(dict(_HUGE_LIMITS), window=1.0)
