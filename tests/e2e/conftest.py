"""浏览器 E2E 套件的夹具（默认不跑，见下）。

为什么默认关闭
--------------
E2E 要真起服务 + 真开 Chromium，单次十几秒，且强依赖本机 node / playwright 安装。
把它塞进默认全量测试会让 `pytest -q` 从 90 秒变成几分钟，还会在没装浏览器的机器上
一片红。因此用**环境变量**显式开关（不用 pytest addoption：子目录 conftest 里
addoption 在 pytest 8 会晚注册报错）：

    HPSL_E2E=1 pytest tests/e2e -q

为什么用子进程起服务
------------------
根 conftest 的 autouse `isolated_home` 会在每个测试函数前改**本进程**的 HPSL_HOME。
若 uvicorn 跑在同进程线程里，`config.home()` 每次都读环境变量，服务会跟着漂到
各个临时目录去。子进程拿到自己那份 env 副本，与 pytest 进程完全隔离——顺带还
验证了真实的 `python -m hpsl serve` 启动路径。
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
NODE_MODULES = Path(
    os.environ.get(
        "HPSL_NODE_PATH",
        r"C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules",
    )
)


def e2e_enabled() -> bool:
    return os.environ.get("HPSL_E2E", "").strip() not in ("", "0", "false", "False")


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_http(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310（本机回环）
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """起一个真实 uvicorn 子进程（独立 HPSL_HOME），返回 base_url。"""
    if not e2e_enabled():
        pytest.skip("浏览器 E2E 默认关闭；设 HPSL_E2E=1 开启")

    home = tmp_path_factory.mktemp("e2e-home")
    port = _free_port()
    env = {
        **os.environ,
        "HPSL_HOME": str(home),
        "HPSL_MASTER_PASSPHRASE": "e2e-test-passphrase",
        "HPSL_ALLOWED_HOSTS": f"127.0.0.1,localhost",
        "HPSL_RATE_LIMITS": (
            "process=100000,route=100000,llm_freq=100000,"
            "sse_concurrency=100000,write_freq=100000"
        ),
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "hpsl", "serve", "--port", str(port), "--no-restore"],
        cwd=str(ROOT),
        env=env,
        stdout=open(home / "server.log", "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_http(base + "/"):
            proc.terminate()
            log = (home / "server.log").read_text(encoding="utf-8", errors="replace")
            pytest.fail(f"E2E 服务未能启动（{base}）：\n{log[-2000:]}")
        yield {"base": base, "home": home}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def node_playwright():
    """返回 (node 可执行文件, env)；缺 node 或缺 playwright 时 skip。"""
    node = os.environ.get("HPSL_NODE") or shutil.which("node")
    if not node:
        pytest.skip("未找到 node，跳过浏览器 E2E")

    env = {**os.environ, "NODE_PATH": str(NODE_MODULES)}
    probe = subprocess.run(
        [node, "-e", "require('playwright'); process.stdout.write('ok')"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if probe.returncode != 0 or "ok" not in probe.stdout:
        pytest.skip(f"node 无法加载 playwright（NODE_PATH={NODE_MODULES}）：{probe.stderr[-400:]}")
    return node, env


@pytest.fixture(scope="session")
def flows_dir() -> Path:
    return Path(__file__).resolve().parent / "flows"
