"""pytest 根配置：将工程根加入 sys.path 并用临时 HPSL_HOME 隔离，避免污染真实 ~/.hpsl。

- 本文件位于工程根，pytest 自动将其目录加入 sys.path，使 `import hpsl` 可用。
- autouse fixture 为每个测试分配独立临时 HPSL_HOME，并重置进程级连接/active/安全单例缓存，
  防止全局状态在用例之间泄漏（B9）。
"""

import pytest

import hpsl.config as config
import hpsl.db as db
import hpsl.projects as projects


def _reset_security_state() -> None:
    """重置进程级安全单例（尤其 SSE 并发闸），恢复 config 当前默认值。

    安全模块导入时会按当时环境变量构造单例，故在 fixture 内惰性导入，确保
    tests/conftest.py 已设置测试限流环境。重新绑定全新的 ConcurrencyLimiter
    既恢复默认上限，也清空任何泄漏的占用槽（仅 configure 无法把 active 归零）。
    """
    from hpsl.routes import security

    default = int(config.rate_limits().get("sse_concurrency", 2))
    security.sse_limiter = security.ConcurrencyLimiter(default)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hpsl_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HPSL_HOME", str(home))
    # 重置进程级缓存，避免测试间串扰
    db._write_conns.clear()
    db._write_locks.clear()
    projects._active_pid = None
    projects._active_label = None
    _reset_security_state()
    yield
    db._write_conns.clear()
    db._write_locks.clear()
    projects._active_pid = None
    projects._active_label = None
    _reset_security_state()
