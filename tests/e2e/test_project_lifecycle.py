"""浏览器 E2E：项目生命周期（新建 → 归档 → 取消归档 → 删除）。

跑法：

    HPSL_E2E=1 pytest tests/e2e -q

默认（不设 HPSL_E2E）整目录 skip，所以 `pytest -q` 不受影响。
缺 node / 缺 playwright 时同样 skip，不会在别人机器上制造红灯。
"""

import random
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def test_project_lifecycle_in_browser(live_server, node_playwright, flows_dir):
    node, env = node_playwright
    name = f"E2E项目{random.randint(1000, 9999)}"

    proc = subprocess.run(
        [node, str(flows_dir / "project_lifecycle.js"), live_server["base"], name],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"E2E 剧本未通过（退出码 {proc.returncode}）：\n{out[-3000:]}"

    # 断言剧本自己确实跑了核心步骤，避免"空跑也 exit 0"的假绿
    for must in (
        "PASS | 新项目出现在活跃列表",
        "PASS | 归档后离开活跃列表",
        "PASS | 取消归档后回到活跃列表",
        "PASS | 名称不符时给出错误提示",
        "PASS | 删除后项目从列表与归档区消失",
        "PASS | 零 console error",
        "PASS | 375px 视口无横向溢出",
    ):
        assert must in out, f"缺少预期步骤：{must}\n{out[-2000:]}"
