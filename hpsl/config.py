"""路径、服务端口与阈值默认值（规格 §4 / §9.3）。

- HPSL_HOME：默认 ~/.hpsl，可被环境变量覆盖。
- 服务端口：默认 8765（hpsl serve 监听 127.0.0.1，端口可配）。
- ConfigVO：全局默认阈值；项目级覆盖经 projects.get_effective_config 合并。
- 项目目录约定（规格 §4）：
    HPSL_HOME/projects/{pid}/project.db
    HPSL_HOME/projects/{pid}/{backups,exports,logs}/
"""

from __future__ import annotations

import os
from pathlib import Path

from .common.dto import ConfigVO

# 环境变量名
ENV_HOME = "HPSL_HOME"
ENV_PORT = "HPSL_PORT"
ENV_ALLOWED_HOSTS = "HPSL_ALLOWED_HOSTS"

DEFAULT_PORT = 8765

# 默认 Host 白名单（冻结项 #5 本地形态）：仅回环
DEFAULT_ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

# 五维限流默认阈值（冻结项 #5）：进程 / 路由 / LLM 频次 / SSE 并发 / 写频次
DEFAULT_RATE_LIMITS = {
    "process": 50,
    "route": 20,
    "llm_freq": 10,
    "sse_concurrency": 2,
    "write_freq": 5,
}
RATE_LIMIT_WINDOW = 1.0  # 滑动窗口秒数


def home() -> Path:
    """全局根目录：环境变量 HPSL_HOME 优先，否则 ~/.hpsl。"""
    raw = os.environ.get(ENV_HOME)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".hpsl"


def projects_root() -> Path:
    return home() / "projects"


def project_dir(pid: str) -> Path:
    """项目根目录：HPSL_HOME/projects/{pid}/（权限 0700）。"""
    return projects_root() / pid


def db_path(pid: str) -> Path:
    """每项目独立库：HPSL_HOME/projects/{pid}/project.db。"""
    return project_dir(pid) / "project.db"


def service_port() -> int:
    """服务端口，默认 8765，可被 HPSL_PORT 覆盖。"""
    try:
        return int(os.environ.get(ENV_PORT, str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT


def allowed_hosts() -> list[str]:
    """Host 白名单（冻结项 #5）：默认回环，可被 HPSL_ALLOWED_HOSTS（逗号分隔）覆盖。"""
    raw = os.environ.get(ENV_ALLOWED_HOSTS)
    if raw:
        return [h.strip() for h in raw.split(",") if h.strip()]
    return list(DEFAULT_ALLOWED_HOSTS)


def rate_limits() -> dict[str, int]:
    """五维限流阈值（冻结项 #5），可被 HPSL_RATE_LIMITS（逗号 k=v）覆盖，用于测试调小阈值。"""
    raw = os.environ.get("HPSL_RATE_LIMITS")
    limits = dict(DEFAULT_RATE_LIMITS)
    if raw:
        for item in raw.split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                try:
                    limits[k.strip()] = int(v.strip())
                except ValueError:
                    pass
    return limits


def default_config() -> ConfigVO:
    """全局默认阈值（规格 §9.3 逐字取值）。"""
    return ConfigVO()
