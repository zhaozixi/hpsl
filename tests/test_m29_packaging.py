"""M29 / P1-07 验收：依赖与打包元数据（pyproject.toml）。

- AC1：`pyproject.toml` 可被 `tomllib` 解析，构建后端仍是 setuptools。
- AC2：`tool.setuptools.packages.find.include` 含 `hpsl*`（包发现覆盖全部子包）。
- AC3：`tool.setuptools.package-data.hpsl` 覆盖模板 / 静态资源 / 角色提示词 / 模型 yaml，
  且对应资源目录在源码树中确有文件。
- AC4：`hpsl/__main__.py` 存在并暴露 `main`，使 `python -m hpsl` 与 `hpsl` 命令等价
  （`hpsl` 命令由 `pip install -e .` 生成，venv 重建后未重装时 `python -m hpsl` 是唯一入口）。

纪律：只读文件、不执行 pip 安装或构建（离线环境）。
"""

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def test_pyproject_is_parseable_by_tomllib():
    data = _load_pyproject()
    assert data["project"]["name"] == "hpsl"
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"
    assert data["build-system"]["requires"]


def test_packages_find_include_covers_hpsl():
    data = _load_pyproject()
    include = data["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "hpsl*" in include


def test_package_data_covers_templates_static_prompts_and_models():
    data = _load_pyproject()
    patterns = data["tool"]["setuptools"]["package-data"]["hpsl"]

    assert any(p.startswith("templates/") for p in patterns), patterns
    assert any(p.startswith("static/") for p in patterns), patterns
    assert any(p.startswith("roles/prompts/") and p.endswith(".md") for p in patterns), patterns
    assert any(p.startswith("models/") and p.endswith(".yaml") for p in patterns), patterns

    # 源码树中确有对应资源（避免只写空模式）
    templates = list((ROOT / "hpsl" / "templates").glob("*.html"))
    static = [p for p in (ROOT / "hpsl" / "static").glob("*") if p.is_file()]
    prompts = list((ROOT / "hpsl" / "roles" / "prompts").glob("*.md"))
    models = list((ROOT / "hpsl" / "models").glob("*.yaml"))
    assert templates and static and prompts and models


def test_python_dash_m_entrypoint_is_wired():
    """AC4：`python -m hpsl` 依赖 `hpsl/__main__.py`，缺了会让未安装环境的用户无入口可用。"""
    import importlib

    entry = ROOT / "hpsl" / "__main__.py"
    assert entry.is_file(), "缺少 hpsl/__main__.py，`python -m hpsl` 不可用"

    mod = importlib.import_module("hpsl.__main__")
    assert callable(mod.main), "hpsl/__main__.py 必须暴露 main"
