"""pytest 根配置：将工程根加入 sys.path 并用临时 HPSL_HOME 隔离，避免污染真实 ~/.hpsl。

- 本文件位于工程根，pytest 自动将其目录加入 sys.path，使 `import hpsl` 可用。
- autouse fixture 为每个测试分配独立临时 HPSL_HOME，并重置进程级连接/active 缓存。
"""

import os

import pytest
from pathlib import Path

import hpsl.db as db
import hpsl.projects as projects


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hpsl_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HPSL_HOME", str(home))
    # 重置进程级缓存，避免测试间串扰
    db._write_conns.clear()
    projects._active_pid = None
    yield
    db._write_conns.clear()
    projects._active_pid = None
