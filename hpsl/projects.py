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
from .common.enums import Role
from .common.errors import BizError

# 校验边界（规格 §9.2 / §9.3）
NAME_MIN, NAME_MAX = 1, 100
GOAL_MIN, GOAL_MAX = 1, 500
BELIEF_MAX = 2000

# M39：可显式指定模型的角色（user 是学习者本人，不对应模型）
_CONFIGURABLE_ROLES = frozenset(r.value for r in Role if r is not Role.USER)

_DIR_MODE = 0o700
_SUBDIRS = ("backups", "exports", "logs")

_active_pid: Optional[str] = None
# 顶栏「当前项目」提示用的展示名缓存（随 set_active 更新；不参与业务判定）。
_active_label: Optional[str] = None


# ---------------------------------------------------------------------------
# 内部助手
# ---------------------------------------------------------------------------
def _read_project_row(pid: str) -> dict:
    """读取 project 行（config 解析为 dict）。不存在 → A010001。"""
    # 先判库文件是否在：目录不存在时 sqlite3.connect 只会抛 OperationalError
    # （"unable to open database file"），冒到上层就是 500。这里统一收敛成 A010001（404）。
    if not _config.db_path(pid).exists():
        raise BizError("A010001")
    rows = _db.query(pid, "SELECT * FROM project WHERE id=?", (pid,))
    if not rows:
        raise BizError("A010001")
    row = rows[0]
    cfg_raw = row.get("config")
    config_dict = json.loads(cfg_raw) if isinstance(cfg_raw, str) else (cfg_raw or {})
    archived = config_dict.get("archived_at")
    return {
        "id": row["id"],
        "name": config_dict.get("name", ""),
        "goal": row["goal"],
        "initial_belief": row.get("initial_belief"),
        "created_at": row["created_at"],
        "config": config_dict,
        # 只承认非空字符串；历史数据里若被写成 null/空串，一律视为未归档。
        "archived_at": archived if isinstance(archived, str) and archived else None,
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
        archived_at=row.get("archived_at"),
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


def list_projects(include_archived: bool = True) -> list[ProjectVO]:
    """扫描项目根目录，返回项目 VO（按创建时间升序）。

    `include_archived` 默认 True：保持既有调用方（REST、备份/轮转 CLI）语义不变——
    备份必须覆盖归档项目，否则归档等于丢数据。**页面**才显式传 False 把归档项藏起来。
    """
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
        if row["archived_at"] and not include_archived:
            continue
        vos.append(_row_to_vo(row))
    vos.sort(key=lambda v: v.created_at)
    return vos


def get_project(pid: str) -> ProjectVO:
    return _row_to_vo(_read_project_row(pid))


def set_active(pid: str) -> None:
    """切换 active 项目句柄（跨项目零共享唯一入口）。"""
    row = _read_project_row(pid)  # 校验存在（不存在 → A010001）
    global _active_pid, _active_label
    _active_pid = pid
    # 仅用于界面常驻提示，避免每个请求为顶栏再查一次库；不参与任何业务判定。
    _active_label = row["name"] or pid


def active_project() -> Optional[ProjectVO]:
    """返回当前 active 项目 VO；未设置则返回 None。"""
    if _active_pid is None:
        return None
    try:
        return _row_to_vo(_read_project_row(_active_pid))
    except BizError:
        return None


def active_label() -> Optional[str]:
    """当前 active 项目的展示名（顶栏提示用）；未设置则 None。"""
    return _active_label if _active_pid is not None else None


def _clear_active() -> None:
    """清空 active 句柄（项目归档/删除后调用）。

    不换成「自动跳到下一个项目」：隐式换项目会让顶栏显示的项目与用户认知脱节，
    宁可退回「未选择项目」空态，由用户显式切换。
    """
    global _active_pid, _active_label
    _active_pid = None
    _active_label = None


def is_archived(pid: str) -> bool:
    """项目是否已归档（读 config.archived_at，零 DDL）。"""
    return _read_project_row(pid)["archived_at"] is not None


def set_archived(pid: str, archived: bool, actor: str = "user") -> ProjectVO:
    """归档 / 取消归档（经 db.write 审计，零 DDL）。

    - 归档：写 `config.archived_at`（UTC ISO-8601）；**幂等**——已归档则保留原时间戳，
      重复点击不会把「归档时间」刷新成今天，避免历史被悄悄改写。
    - 取消归档：置 null（`_read_project_row` 与 `ConfigVO` 都把它当"未归档"）。
    - 归档当前 active 项目 → 清空 active 句柄（归档语义是"先收起来"，不该还占着当前位）。
    - 归档不是删除：项目目录、库、快照全部保留，备份/轮转 CLI 仍然覆盖它。
    """
    row = _read_project_row(pid)
    current = row["archived_at"]
    if archived:
        if not current:
            set_config(pid, {"archived_at": _db._now_utc()}, actor=actor)
        if _active_pid == pid:
            _clear_active()
    elif current:
        set_config(pid, {"archived_at": None}, actor=actor)
    return get_project(pid)


def delete_project(pid: str, confirm_name: str, actor: str = "user") -> str:
    """删除项目：整目录移入 `HPSL_HOME/trash/`，返回回收目录路径。

    三个设计取舍（都不是随手写的）：

    1. **要求确认项目名**：删除不可撤销，只按 pid 或一次点击就删太危险。调用方必须传
       与项目名逐字一致的 `confirm_name`，否则 C400001（参数错误，不是"项目不存在"）。
    2. **不 rmtree，改为 rename 到 trash/**：同分区 rename 是原子的，误删可直接从
       文件系统取回；真删（清空 trash）交给用户自己决定，程序不替用户做不可逆操作。
    3. **先关连接再 rename**：Windows 上库文件被 sqlite 占用时 rename 会失败；
       `db.close_project` 释放写连接（含 WAL checkpoint）后再动目录。

    审计说明：project.db 随目录进 trash，删除前的全部审计记录仍在库内可查；
    本操作本身不额外写审计行（库已不在此路径，写了也读不到）。
    """
    row = _read_project_row(pid)
    expect = row["name"] or pid
    if not isinstance(confirm_name, str) or confirm_name.strip() != expect:
        raise BizError("C400001", msg=f"确认的项目名不匹配，未删除。请输入「{expect}」再试")

    src = _db.safe_path(pid)  # 防目录穿越（C400002）
    ts = _db._now_utc().replace(":", "-")
    trash = _config.trash_root()
    dest = trash / f"{pid}-{ts}"
    try:
        trash.mkdir(parents=True, mode=_DIR_MODE, exist_ok=True)
        _db.close_project(pid)
        src.rename(dest)
    except OSError as exc:
        raise BizError("B999001", msg=f"删除失败（目录未移动，项目仍在）：{exc}") from exc

    if _active_pid == pid:
        _clear_active()
    return str(dest)


def restore_last_active() -> Optional[ProjectVO]:
    """启动时恢复「上次所在项目」（显式调用；**不得**在模块导入期执行）。

    恢复源是 `routes/projects.py` 切换端点为 AC7「切换须留痕」写入的
    `config.last_active_at`，因此本函数**不引入新的可写状态源**：唯一写入点仍是切换，
    这里只读不写、零 DDL、零新增库对象。

    规则：
    - 只读：不写 config、不追加审计；
    - 一个时间戳都没有 → 返回 None（不猜测，保持原有「未选项目」空态）；
    - 时间戳统一由 `_switch_ts()` 生成（UTC 定长 ISO-8601），可直接字典序比较；
    - 单个项目读取失败 → 跳过，不影响其它项目；
    - **归档项目不参与恢复**：归档语义就是"收起来"，启动时还跳回去等于没归档；
    - 选中项目已不可读 → set_active 抛 A010001，由调用方捕获（不影响启动）。
    """
    best_pid: Optional[str] = None
    best_ts = ""
    for vo in list_projects(include_archived=False):
        try:
            ts = get_raw_config(vo.id).get("last_active_at")
        except BizError:
            continue
        if not isinstance(ts, str) or not ts or ts <= best_ts:
            continue
        best_pid, best_ts = vo.id, ts
    if best_pid is None:
        return None
    set_active(best_pid)
    return get_project(best_pid)


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
        archived_at=row.get("archived_at"),
    )


def _normalize_role_models(raw) -> dict:
    """归一化 `role_models`：只保留六角色的非空映射，空值视为「走自动选择」。

    结构性错误一律 C400001（参数问题），不静默丢弃——写错角色名却毫无提示，
    用户会以为配置生效了。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BizError("C400001", msg='role_models 须为对象，形如 {"critic": "deepseek-v4-pro"}')
    out: dict = {}
    for role, model_id in raw.items():
        if role not in _CONFIGURABLE_ROLES:
            raise BizError(
                "C400001",
                msg=f"未知角色：{role}（可用：{', '.join(sorted(_CONFIGURABLE_ROLES))}）",
            )
        if model_id is None or (isinstance(model_id, str) and not model_id.strip()):
            continue  # 空值 = 删除该键 = 该角色走自动选择
        if not isinstance(model_id, str):
            raise BizError("C400001", msg=f"角色 {role} 的模型 ID 须为字符串")
        out[role] = model_id.strip()
    return out


def set_config(pid: str, patch: dict, actor: str = "user") -> ConfigVO:
    """项目级阈值覆盖写入（经 db.write 审计）。返回合并后的 ConfigVO。"""
    row = _read_project_row(pid)
    patch = dict(patch or {})
    if "role_models" in patch:
        # 整体替换语义：前端总是提交完整的六角色快照（空值被剔除），
        # 这样「把某角色改回自动」只需提交时不含该键。
        patch["role_models"] = _normalize_role_models(patch["role_models"])
    merged = {**row["config"], **patch}
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
