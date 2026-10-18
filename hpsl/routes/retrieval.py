"""P2-01 知识库检索 Web 接线（消费 engine.retrieval，零 DDL）。

路由（`/api/v1`）：
- `POST /sources`（write_security）：body `{path}` → `retrieval.index_source`。
- `GET /sources`（read_security）：已挂载来源列表（含 chunk 数）。
- `DELETE /sources/{sid}`（write_security）：删除来源及其 FTS 索引。
- `GET /search?q=&limit=`（read_security）：检索；返回 `{hit, items}`，无命中 `hit=false`、
  `items=[]`（页面文案「未检索到」）。

纪律：零 DDL（仅 source 表 + chunk_fts 虚表）；写接口强制 write_security；
不新增错误码（复用 A010001 / C400001）。engine 保持 headless，不依赖本模块。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates

from .. import projects as proj
from ..common.errors import BizError
from ..common.result import ok
from ..engine import retrieval
from ._htmx import refresh_with_toast
from .security import read_security, write_security

api_router = APIRouter(prefix="/api/v1")
page_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _is_htmx(req: Request) -> bool:
    return req.headers.get("hx-request") is not None


def _require_pid() -> str:
    """当前激活项目 pid；未激活 → A010001（与其它路由同语义）。"""
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目")
    return active.id


async def _parse_body(req: Request) -> dict:
    """兼容 JSON 与表单（与 roles.py / unit.py 同构）。"""
    import json as _json

    ct = req.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            return await req.json()
        except Exception:
            raise BizError("C400001", msg="请求体需为 JSON")
    try:
        form = await req.form()
        return {k: v for k, v in form.items()}
    except Exception:
        return {}


@api_router.post("/sources", dependencies=[Depends(write_security)])
async def create_source_api(req: Request):
    """挂载并索引本地知识库目录。请求体：`{path}`（绝对路径）。"""
    body = await _parse_body(req)
    path = body.get("path")
    if not isinstance(path, str) or not path.strip():
        raise BizError("C400001", msg="path 不能为空")
    force = str(body.get("force", "")).lower() in ("1", "true", "yes", "on")
    result = retrieval.index_source(_require_pid(), path, force=force)
    if _is_htmx(req):
        return refresh_with_toast(
            f"已索引 {result.get('files_indexed', 0)} 个变更文件、"
            f"删除 {result.get('files_removed', 0)} 个、共 {result.get('chunks', 0)} 个片段"
        )
    return ok(result)


@page_router.get("/knowledge", dependencies=[Depends(read_security)])
def knowledge_page(req: Request, q: str = ""):
    """知识库页：挂载目录、来源列表、检索结果（无命中显示「未检索到」）。"""
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="knowledge.html",
            context={"error": "尚未选择项目，请先在首页选择或新建项目"},
        )
    pid = active.id
    sources = retrieval.list_sources(pid)
    query = (q or "").strip()
    results = retrieval.search(pid, query, limit=10) if query else []
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="knowledge.html",
        context={
            "project": active.to_dict(),
            "sources": sources,
            "q": query,
            "results": results,
        },
    )


@api_router.get("/sources", dependencies=[Depends(read_security)])
def list_sources_api():
    """列出当前项目已挂载的知识库来源。"""
    return ok(retrieval.list_sources(_require_pid()))


@api_router.delete("/sources/{sid}", dependencies=[Depends(write_security)])
def delete_source_api(sid: str, req: Request):
    """删除知识库来源及其 FTS 索引（source 行 + chunk_fts 行，同事务留痕）。"""
    if not sid or not sid.strip():
        raise BizError("C400001", msg="source_id 不能为空")
    result = retrieval.delete_source(_require_pid(), sid)
    if _is_htmx(req):
        return refresh_with_toast("已删除该来源")
    return ok(result)


@api_router.get("/search", dependencies=[Depends(read_security)])
def search_api(q: str = "", limit: int = retrieval.DEFAULT_LIMIT):
    """知识库检索。无命中返回 `{hit: false, items: []}`（页面显示「未检索到」）。"""
    items = retrieval.search(_require_pid(), q, limit=limit)
    return ok({"hit": bool(items), "items": items})
