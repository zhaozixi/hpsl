"""E2E：密钥保存/测试/清除 + 单元七态推进（浏览器真点击）。"""

import random
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def test_keys_and_unit_seven_states(live_server, node_playwright, flows_dir):
    node, env = node_playwright
    name = f"E2E七态项目{random.randint(1000, 9999)}"

    proc = subprocess.run(
        [node, str(flows_dir / "keys_and_unit.js"), live_server["base"], name],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"E2E 剧本未通过（退出码 {proc.returncode}）：\n{out[-4000:]}"

    for must in (
        "PASS | 七态推进",
        "PASS | 密钥保存",
        "PASS | 密钥测试",
        "PASS | 密钥清除",
        "PASS | 零 console error",
        "PASS | 375px 视口无横向溢出",
    ):
        assert must in out, f"缺少预期步骤：{must}\n{out[-3000:]}"
