"""M10 Phase B/C 第四批：备份导出 F1（消费 M0 WAL + VACUUM INTO，经 db.backup_project）。

- GET /backup：当前激活项目备份管理页（只读查询；列出既有备份 + 触发导出）。
- GET /api/v1/projects/{pid}/backups：列出该项目 backups/ 目录下的快照文件（REST）。
- 实际导出由 M1 既有 `POST /api/v1/projects/{pid}/backup`（projects.py）委托 db.backup_project 完成。

纪律约束：零 DDL（仅向项目目录外部写文件，不触库内对象）；读接口 read_security；
导出写接口 write_security（在 projects.py 已落实）；本批次不改 engine；无新错误码。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import db, projects as proj, config as cfg
from ..common.errors import BizError
from ..common.result import ok
from .security import read_security

page_router = APIRouter()
api_router = APIRouter(prefix="/api/v1")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _list_backups(pid: str) -> list[str]:
    """返回项目 backups/ 目录下快照文件名（按修改时间倒序，最新在前）。"""
    d = cfg.project_dir(pid) / "backups"
    if not d.is_dir():
        return []
    files = [p.name for p in d.glob("*.db") if p.is_file()]
    files.sort(reverse=True)
    return files


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
