"""M1 七个 HTTP 接口 + 页面路由（委托 projects.py 内部 API，响应走 common/result）。

接口契约（规格 §4）：
- 写接口（POST/PUT）强制 write_security（Origin + CSRF + 限流）；读接口仅 read_security（限流）。
- 所有写经 db.write 审计；业务代码不得直写 audit_log。
- backup 为占位（完整快照轮转留 F1），返回 501。
- 页面（Jinja2 + HTMX）从简，仅提供首页/建项向导/项目详情。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .. import db, projects as proj, config as _config
from ..common.dto import ProjectCreateRequest, ProjectVO, ConfigVO
from ..common.errors import BizError
from ..common.result import ok, fail
from .security import write_security, read_security

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

api_router = APIRouter(prefix="/api/v1")
page_router = APIRouter()


def _require_project(pid: str) -> ProjectVO:
    """校验 pid 合法且项目存在；pid 经 safe_path 防穿越，缺失 → A010001。"""
    db.safe_path(pid)  # 防目录穿越（C400002）
    return proj.get_project(pid)


async def _parse_body(req: Request) -> dict:
    """兼容 JSON 与表单（HTMX 默认 form 提交），统一成 dict。"""
    ct = req.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            return await req.json()  # type: ignore[unreachable]
        except Exception:
            raise BizError("C400001", msg="请求体需为 JSON")
    try:
        form = await req.form()
        return {k: (v if not hasattr(v, "file") else v) for k, v in form.items()}
    except Exception:
        return {}


def _is_htmx(req: Request) -> bool:
    return req.headers.get("hx-request") is not None


# ---------------------------------------------------------------------------
# API：创建项目
# ---------------------------------------------------------------------------
@api_router.post("/projects", dependencies=[Depends(write_security)])
async def create_project_api(req: Request):
    data = await _parse_body(req)
    try:
        pcr = ProjectCreateRequest(
            name=data.get("name"), goal=data.get("goal"), initial_belief=data.get("initial_belief")
        )
        pcr.validate()
    except ValueError as exc:
        raise BizError("C400001", msg=str(exc))
    vo = proj.create_project(pcr.name, pcr.goal, pcr.initial_belief, actor="user")
    if _is_htmx(req):
        return HTMLResponse(f"<p class='ok'>已创建项目：{vo.name}</p>")
    return ok(vo.to_dict())


@api_router.get("/projects", dependencies=[Depends(read_security)])
def list_projects_api():
    return ok([p.to_dict() for p in proj.list_projects()])


@api_router.post("/projects/{pid}/switch", dependencies=[Depends(write_security)])
def switch_api(pid: str):
    vo = _require_project(pid)
    proj.set_active(pid)  # 进程内 active 句柄（M0 设计，不写库）
    # AC7：切换须留痕。M0 set_active 为内存句柄无 db 写入，且 M1 不改 projects.py 逻辑，
    # 故经 db.write 唯一入口（set_config 合并 last_active_at）留下审计条目；ConfigVO 忽略该键。
    proj.set_config(pid, {"last_active_at": _switch_ts()}, actor="user")
    return ok(vo.to_dict())


def _switch_ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


@api_router.get("/projects/{pid}", dependencies=[Depends(read_security)])
def detail_api(pid: str):
    vo = _require_project(pid)
    return ok(vo.to_dict())


@api_router.put("/projects/{pid}/initial-belief", dependencies=[Depends(write_security)])
async def update_belief_api(pid: str, req: Request):
    vo = _require_project(pid)
    data = await _parse_body(req)
    vo2 = proj.update_initial_belief(pid, data.get("initial_belief"), actor="user")
    if _is_htmx(req):
        return HTMLResponse(f"<p class='ok'>已更新初版认识（{len(vo2.initial_belief or '')} 字）</p>")
    return ok(vo2.to_dict())


@api_router.get("/projects/{pid}/config", dependencies=[Depends(read_security)])
def get_config_api(pid: str):
    _require_project(pid)
    cfg = proj.get_effective_config(pid)
    return ok(cfg.to_dict())


@api_router.put("/projects/{pid}/config", dependencies=[Depends(write_security)])
async def set_config_api(pid: str, req: Request):
    vo = _require_project(pid)
    data = await _parse_body(req)
    cfg = proj.set_config(pid, data or {}, actor="user")
    return ok(cfg.to_dict())


@api_router.post("/projects/{pid}/backup", dependencies=[Depends(write_security)])
def backup_api(pid: str, req: Request):
    # F1：完整快照轮转（VACUUM INTO）— M1 占位在此落实；委托 db.backup_project。
    db.safe_path(pid)  # 防目录穿越（C400002）
    if not (_config.project_dir(pid) / "project.db").exists():
        raise BizError("A010001", msg=f"项目 {pid} 不存在")
    try:
        dest = db.backup_project(pid)
    except Exception as exc:  # 备份属系统操作，意外失败归 B999001（已注册，非新码）
        raise BizError("B999001", msg=f"备份失败：{exc}") from exc
    name = Path(dest).name
    if _is_htmx(req):
        return HTMLResponse(
            f"<p class='ok'>已生成备份：{name}</p>", headers={"HX-Refresh": "true"}
        )
    return ok({"backup_file": name, "backup_path": dest, "pid": pid})


# ---------------------------------------------------------------------------
# 页面（Jinja2 + HTMX）
# ---------------------------------------------------------------------------
@page_router.get("/")
def index_page(req: Request):
    projects = proj.list_projects()
    return _TEMPLATES.TemplateResponse(
        request=req, name="index.html", context={"projects": [p.to_dict() for p in projects]}
    )


@page_router.get("/projects/new")
def new_page(req: Request):
    return _TEMPLATES.TemplateResponse(request=req, name="create.html", context={})


@page_router.get("/projects/{pid}")
def detail_page(req: Request, pid: str):
    vo = _require_project(pid)
    cfg = proj.get_effective_config(pid)
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="project_detail.html",
        context={"project": vo.to_dict(), "config": cfg.to_dict()},
    )


@page_router.get("/settings", dependencies=[Depends(read_security)])
def settings_page(req: Request):
    """项目设置页（M10B）：复用 M1 config REST，仅前端表单 + 页面路由，无新增后端 API。"""
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="settings.html",
            context={"error": "尚未选择项目，请先在首页选择或新建项目"},
        )
    cfg = proj.get_effective_config(active.id)
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="settings.html",
        context={"project": active.to_dict(), "config": cfg.to_dict()},
    )
