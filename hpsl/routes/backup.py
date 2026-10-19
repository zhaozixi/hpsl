"""M10 Phase B/C 第四批：备份导出 F1（消费 M0 WAL + VACUUM INTO，经 db.backup_project）。

- GET /backup：当前激活项目备份管理页（只读查询；列出既有备份 + 触发导出）。
- GET /api/v1/projects/{pid}/backups：列出该项目 backups/ 目录下的快照文件（REST）。
- POST /api/v1/backup/{name}/restore：从当前项目快照恢复 project.db（write_security；
  仅接受 basename 且必须命中 db.list_backups 白名单，委托 db.restore_project）。
- 实际导出由 M1 既有 `POST /api/v1/projects/{pid}/backup`（projects.py）委托 db.backup_project 完成。

纪律约束：零 DDL（仅向项目目录外部写文件，不触库内对象）；读接口 read_security；
导出/恢复写接口 write_security（导出在 projects.py 已落实）；本批次不改 engine；无新错误码。
"""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from ..templating import make_templates

from .. import db, projects as proj, config as cfg
from ..common.errors import BizError
from ..common.result import ok
from .security import read_security, write_security

page_router = APIRouter()
api_router = APIRouter(prefix="/api/v1")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = make_templates(str(_TEMPLATES_DIR))


def _list_backups(pid: str) -> list[str]:
    """返回项目 backups/ 目录下快照文件名（最新在前）。委托 db.list_backups（唯一实现）。"""
    return db.list_backups(pid)


def _active_pid() -> str:
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目，请先在首页选择或新建项目")
    return active.id


def _validate_backup_name(pid: str, name: str) -> str:
    """校验恢复目标：仅接受纯文件名（basename）且必须在本项目快照清单内。

    - 拒绝空串、路径分隔符、`.` / `..`、绝对路径等一切目录穿越形态（复用 C400001）。
    - 必须命中 `db.list_backups(pid)`（白名单），不接受任意文件名/不存在文件；
      `db.restore_project` 内部还会再做一次 safe_path + backups 目录归属校验。
    """
    raw = (name or "").strip()
    if (
        not raw
        or raw in (".", "..")
        or "/" in raw
        or "\\" in raw
        or "\x00" in raw
        or Path(raw).name != raw
    ):
        raise BizError("C400001", msg="备份文件名非法（只接受 backups/ 内的快照文件名）")
    if raw not in _list_backups(pid):
        raise BizError("C400001", msg=f"备份文件不存在：{raw}")
    return raw


@page_router.get("/backup", dependencies=[Depends(read_security)])
def backup_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req, name="backup.html", context={"error": "尚未选择项目，请先在首页选择或新建项目"}
        )
    pid = active.id
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="backup.html",
        context={"project": active.to_dict(), "pid": pid, "backups": _list_backups(pid)},
    )


@api_router.get("/projects/{pid}/backups", dependencies=[Depends(read_security)])
def list_backups_api(pid: str):
    db.safe_path(pid)  # 防目录穿越
    if not (cfg.project_dir(pid) / "project.db").exists():
        raise BizError("A010001", msg=f"项目 {pid} 不存在")
    return ok(_list_backups(pid))


@api_router.post("/backup/{name}/restore", dependencies=[Depends(write_security)])
def restore_backup_api(req: Request, name: str):
    """从当前项目的某份快照恢复（危险写操作：CSRF/Origin + 二次确认由页面 hx-confirm 承担）。

    - 校验 name 为 basename 且命中 `db.list_backups(pid)`；
    - 委托 `db.restore_project`（完整性校验 → pre-restore 留底 → 覆盖 → 复检）；
    - HTMX：返回结果片段 + HX-Refresh 整页刷新（恢复后页面数据整体变化）；
      非 HTMX：返回统一 Result（data 含恢复来源与留底路径）。
    """
    pid = _active_pid()
    safe_name = _validate_backup_name(pid, name)
    info = db.restore_project(pid, safe_name)
    pre_restore = Path(info["pre_restore"]).name
    if req.headers.get("hx-request") is not None:
        # 文件名来自受控白名单，仍做 HTML 转义（文件名可能含特殊字符）
        msg = html.escape(f"已从快照恢复：{safe_name}（恢复前数据已留底：{pre_restore}）")
        return HTMLResponse(f"<p class='ok'>{msg}</p>", headers={"HX-Refresh": "true"})
    return ok(
        {
            "pid": pid,
            "backup_file": safe_name,
            "restored_from": info["restored_from"],
            "pre_restore": info["pre_restore"],
            "objects": info["objects"],
        }
    )
