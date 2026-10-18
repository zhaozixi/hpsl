"""项目管理与配置（规格 §9 / 锁定基线 §3.2.M1）。

- create_project：建目录(0700) → migrate 建表 → 写 project 行（经 db.write 审计）→ 切换 active。
- list_projects：扫描 HPSL_HOME/projects/ 目录（每项目独立库，无跨库索引）。
- active_project / set_active：进程内 active 句柄（跨项目零共享唯一入口）。
- get_effective_config：全局默认 ConfigVO 与项目 config JSON 覆盖合并。
- set_config：经 db.write 审计写入 project.config。

设计说明（M0 冻结约束下的实现选择）：
  锁定基线 §7.1 的 project 表 DDL 不含 name 列（已冻结、不得修改），而 ProjectVO/唯一性约束
  依赖 name。因此项目名持久化于 project.config JSON 的 "name" 键；唯一性在创建时扫描各项目
  目录读取 config.name 实现（规格 §4 明确「项目名唯一性由 projects.py 创建时扫描项目根目录实现」）。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from . import config as _config
from . import db as _db
from .common.dto import ProjectCreateRequest, ProjectVO, ConfigVO
from .common.errors import BizError

# 校验边界（规格 §9.2 / §9.3）
NAME_MIN, NAME_MAX = 1, 100
GOAL_MIN, GOAL_MAX = 1, 500
BELIEF_MAX = 2000

_DIR_MODE = 0o700
_SUBDIRS = ("backups", "exports", "logs")

_active_pid: Optional[str] = None


# ---------------------------------------------------------------------------
# 内部助手
# ---------------------------------------------------------------------------
def _read_project_row(pid: str) -> dict:
    """读取 project 行（config 解析为 dict）。不存在 → A010001。"""
    rows = _db.query(pid, "SELECT * FROM project WHERE id=?", (pid,))
    if not rows:
        raise BizError("A010001")
    row = rows[0]
    cfg_raw = row.get("config")
    config_dict = json.loads(cfg_raw) if isinstance(cfg_raw, str) else (cfg_raw or {})
    return {
        "id": row["id"],
        "name": config_dict.get("name", ""),
        "goal": row["goal"],
        "initial_belief": row.get("initial_belief"),
        "created_at": row["created_at"],
        "config": config_dict,
    }


def _name_exists(name: str) -> bool:
    """扫描项目根目录，按 config.name 判重（规格 §4 / §9.2）。"""
    root = _config.projects_root()
    if not root.exists():
        return False
    for child in root.iterdir():
        if not child.is_dir():
            continue
        db_file = child / "project.db"
        if not db_file.exists():
            continue
        try:
            rows = _db.query(child.name, "SELECT config FROM project WHERE id=?", (child.name,))
        except Exception:
            continue
        if not rows:
            continue
        cfg_raw = rows[0].get("config")
        cfg = json.loads(cfg_raw) if isinstance(cfg_raw, str) else (cfg_raw or {})
        if cfg.get("name") == name:
            return True
    return False


def _row_to_vo(row: dict) -> ProjectVO:
    return ProjectVO(
        id=row["id"],
        name=row["name"],
        goal=row["goal"],
        initial_belief=row.get("initial_belief"),
        db_path=str(_config.db_path(row["id"])),
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def create_project(
    name: str,
    goal: str,
    initial_belief: Optional[str] = None,
    actor: str = "user",
) -> ProjectVO:
    """创建项目（规格 §9.2 Step1–5）。

    重名 → A010002；参数非法 → C400001；磁盘/权限异常 → B999001 并清理半成品目录。
    """
    req = ProjectCreateRequest(name=name, goal=goal, initial_belief=initial_belief)
    try:
        req.validate()
    except ValueError as exc:
        raise BizError("C400001", msg=str(exc))

    if _name_exists(name):
        raise BizError("A010002")

    pid = uuid.uuid4().hex
    pdir = _config.project_dir(pid)
    try:
        pdir.mkdir(parents=True, mode=_DIR_MODE)
        os.chmod(pdir, _DIR_MODE)
        for sub in _SUBDIRS:
            (pdir / sub).mkdir(mode=_DIR_MODE, exist_ok=True)
    except OSError as exc:
        shutil.rmtree(pdir, ignore_errors=True)
        _db.close_project(pid)
        raise BizError("B999001", msg=f"建目录失败：{exc}") from exc

    # 写 project 行（config 内持久化 name，因冻结 DDL 无 name 列）
    try:
        cfg = _config.default_config().to_dict()
        cfg["name"] = name
        now = _db._now_utc()
        row = {
            "id": pid,
            "goal": goal,
            "initial_belief": initial_belief,
            "created_at": now,
            "config": cfg,
        }
        _db.write(pid, "project", row, actor)
    except Exception:
        shutil.rmtree(pdir, ignore_errors=True)
        _db.close_project(pid)
        raise

    vo = ProjectVO(
        id=pid,
        name=name,
        goal=goal,
        initial_belief=initial_belief,
        db_path=str(_config.db_path(pid)),
        created_at=now,
    )
    set_active(pid)
    return vo


def list_projects() -> list[ProjectVO]:
    """扫描项目根目录，返回全部项目 VO（按创建时间升序）。"""
    root = _config.projects_root()
    if not root.exists():
        return []
    vos: list[ProjectVO] = []
    for child in root.iterdir():
        if not child.is_dir() or not (child / "project.db").exists():
            continue
        try:
            row = _read_project_row(child.name)
        except BizError:
            continue
        vos.append(_row_to_vo(row))
    vos.sort(key=lambda v: v.created_at)
    return vos


def get_project(pid: str) -> ProjectVO:
    return _row_to_vo(_read_project_row(pid))


def set_active(pid: str) -> None:
    """切换 active 项目句柄（跨项目零共享唯一入口）。"""
    _read_project_row(pid)  # 校验存在（不存在 → A010001）
    global _active_pid
    _active_pid = pid


def active_project() -> Optional[ProjectVO]:
    """返回当前 active 项目 VO；未设置则返回 None。"""
    if _active_pid is None:
        return None
    try:
        return _row_to_vo(_read_project_row(_active_pid))
    except BizError:
        return None


def get_effective_config(pid: str) -> ConfigVO:
    """全局默认 + 项目 config JSON 覆盖合并（规格 §9.1 / 基线 §3.4 配置解析）。"""
    row = _read_project_row(pid)
    project_cfg = row.get("config") or {}
    merged = {**_config.default_config().to_dict(), **project_cfg}
    return ConfigVO.from_dict(merged)


def get_raw_config(pid: str) -> dict:
    """返回 project.config 原始 dict（含 extra 键，如 dismissed_prompts、last_active_at）。

    与 get_effective_config 不同，本函数不过滤 ConfigVO 已知键，直接返回落库的原始
    JSON（M10B 软提示持久化据此读写 dismissed_prompts，零 DDL）。
    """
    return _read_project_row(pid).get("config") or {}


def update_initial_belief(pid: str, belief: Optional[str], actor: str = "user") -> ProjectVO:
    """录入/更新初版认识（project.initial_belief 列，经 db.write 审计）。"""
    if belief is not None:
        if not isinstance(belief, str) or len(belief) > BELIEF_MAX:
            raise BizError("C400001", msg=f"initial_belief 须为 ≤{BELIEF_MAX} 字符的字符串")
    row = _read_project_row(pid)
    _db.write(
        pid,
        "project",
        {
            "id": pid,
            "goal": row["goal"],
            "initial_belief": belief,
            "created_at": row["created_at"],
            "config": row["config"],
        },
        actor,
    )
    return ProjectVO(
        id=pid,
        name=row["name"],
        goal=row["goal"],
        initial_belief=belief,
        db_path=str(_config.db_path(pid)),
        created_at=row["created_at"],
    )


def set_config(pid: str, patch: dict, actor: str = "user") -> ConfigVO:
    """项目级阈值覆盖写入（经 db.write 审计）。返回合并后的 ConfigVO。"""
    row = _read_project_row(pid)
    merged = {**row["config"], **(patch or {})}
    _db.write(
        pid,
        "project",
        {
            "id": pid,
            "goal": row["goal"],
            "initial_belief": row.get("initial_belief"),
            "created_at": row["created_at"],
            "config": merged,
        },
        actor,
    )
    return ConfigVO.from_dict(merged)
