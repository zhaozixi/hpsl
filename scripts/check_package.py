"""E-04 工程债收口：打包产物自检（离线可跑，替代 pipx install 实测）。

为什么不用 `pipx install`：pipx 会把包装进独立 venv 并联网解析依赖；在无外网/受限环境
下无法执行，且它验证的核心只有两件事——
  ① wheel 里到底装进了哪些文件（templates / static / prompts / yaml 有没有漏）；
  ② 入口点 `hpsl = hpsl.app:main` 是否可用。
本脚本用**本地离线构建**覆盖 ①，并静态校验 ②，无需网络。

用法：
    python scripts/check_package.py          # 构建到临时目录并校验，结束后清理
    python scripts/check_package.py --keep   # 保留 .pkgcheck/ 便于人工翻看

退出码：0 全通过；1 有缺失项或构建失败。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# wheel 中必须存在的条目（缺任何一项 → 安装包在用户机器上都跑不起来）
REQUIRED = [
    "hpsl/app.py",
    "hpsl/db.py",
    "hpsl/config.py",
    "hpsl/projects.py",
    "hpsl/common/errors.py",
    "hpsl/engine/state.py",
    "hpsl/engine/grading.py",
    "hpsl/engine/cost.py",
    "hpsl/models/registry.py",
    "hpsl/models/models.yaml",
    "hpsl/models/pricing.yaml",
    "hpsl/roles/contracts.py",
    "hpsl/secrets/keystore.py",
    "hpsl/routes/unit.py",
    "hpsl/routes/security.py",
    "hpsl/templates/base.html",
    "hpsl/templates/macros.html",
    "hpsl/static/app.css",
    "hpsl/static/app.js",
    "hpsl/static/htmx.min.js",
]

# 分类下限（防"文件在但只打进一半"）
MIN_COUNTS = {"templates": 20, "prompts": 6, "static": 4}


def _pick_python(explicit: str | None) -> str:
    """选一个带 setuptools 的解释器（venv 里通常没有，构建需要它）。"""
    candidates = [
        explicit,
        os.environ.get("HPSL_BUILD_PYTHON"),
        sys.executable,
        r"D:\soft\anaconda3\python.exe",
    ]
    for p in candidates:
        if not p:
            continue
        if not Path(p).exists():
            continue
        probe = subprocess.run(
            [p, "-c", "import setuptools"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return p
    print(
        "找不到带 setuptools 的解释器。\n"
        "用法：python scripts/check_package.py --python <带 setuptools 的 python>\n"
        "或先装：pip install setuptools wheel"
    )
    raise SystemExit(1)


def _build(outdir: Path, python: str) -> Path:
    cmd = [
        python,
        "-m",
        "pip",
        "wheel",
        ".",
        "--no-deps",
        "--no-build-isolation",
        "-w",
        str(outdir),
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        print("构建失败：\n" + (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:])
        raise SystemExit(1)
    wheels = sorted(outdir.glob("*.whl"))
    if not wheels:
        print("构建未产出 wheel")
        raise SystemExit(1)
    return wheels[-1]


def _check(wheel: Path) -> int:
    names = set(zipfile.ZipFile(wheel).namelist())
    missing = [n for n in REQUIRED if n not in names]
    counts = {
        "templates": sum(1 for n in names if "/templates/" in n),
        "prompts": sum(1 for n in names if "/roles/prompts/" in n),
        "static": sum(1 for n in names if "/static/" in n),
    }
    print(f"wheel: {wheel.name}  条目 {len(names)}")
    print(f"分类: {counts}")
    ok = True
    for name, floor in MIN_COUNTS.items():
        if counts[name] < floor:
            print(f"  ✗ {name} 仅 {counts[name]} 个（期望 ≥ {floor}）")
            ok = False
    for n in missing:
        print(f"  ✗ 缺少 {n}")
        ok = False

    # 入口点：pyproject 声明 hpsl = hpsl.app:main，且 main 必须存在
    entry = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if 'hpsl = "hpsl.app:main"' not in entry:
        print("  ✗ pyproject 入口点缺失或改名")
        ok = False
    app_src = (ROOT / "hpsl" / "app.py").read_text(encoding="utf-8")
    if "def main(" not in app_src:
        print("  ✗ hpsl/app.py 缺 main()")
        ok = False

    print("结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留 .pkgcheck/ 目录")
    ap.add_argument("--python", default=None, help="用于构建的解释器（需带 setuptools）")
    args = ap.parse_args()

    python = _pick_python(args.python)
    outdir = ROOT / ".pkgcheck"
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir()
    try:
        wheel = _build(outdir, python)
        return _check(wheel)
    finally:
        build = ROOT / "build"
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(outdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
