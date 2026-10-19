"""M10 Phase B/C 第六批：超限确认弹层 A080001（消费 M8 cost.budget_status + cost.confirm_continue）。

- GET /budget：预算总览页（列出本项目会话及其预算三态；blocked 会话渲染超限确认弹层）。
- GET /api/v1/budget/status?session_id=X：返回会话预算估算（status / cumulative_usd / budget_cap_usd / confirmations）。
- POST /api/v1/budget/confirm（write_security）：超限确认放行（cost.confirm_continue），返回更新后的估算；htmx 返回弹层片段。

纪律约束：读接口 read_security；确认写接口 write_security（委托 cost.confirm_continue 经 db.append_audit_event）；
A080001 已在 M8 注册并复用（弹层文案与其 user_msg 一致）；零 DDL；本批次不改 engine（仅消费）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Depends
from ..templating import make_templates

from .. import db, projects as proj
from ..common.errors import BizError
from ..common.money import DEFAULT_USD_CNY
from ..common.result import ok
from ..engine import cost
from .security import write_security, read_security

page_router = APIRouter()
api_router = APIRouter(prefix="/api/v1")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = make_templates(str(_TEMPLATES_DIR))


async def _parse_body(req: Request) -> dict:
    ct = req.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            import json as _json
            return await req.json()
        except Exception:
            raise BizError("C400001", msg="请求体需为 JSON")
    try:
        form = await req.form()
        return {k: v for k, v in form.items()}
    except Exception:
        return {}


def _is_htmx(req: Request) -> bool:
    return req.headers.get("hx-request") is not None


def _rate() -> float:
    """当前项目的美元→人民币展示汇率（无激活项目时用默认值）。"""
    active = proj.active_project()
    if active is None:
        return DEFAULT_USD_CNY
    return proj.get_effective_config(active.id).usd_cny_rate


# ---------------------------------------------------------------------------
# 页面：预算总览 + 超限弹层
# ---------------------------------------------------------------------------
@page_router.get("/budget", dependencies=[Depends(read_security)])
def budget_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req, name="budget.html", context={"error": "尚未选择项目，请先在首页选择或新建项目"}
        )
    pid = active.id
    rate = proj.get_effective_config(pid).usd_cny_rate  # M38：双币展示汇率
    sessions = db.query(pid, "SELECT * FROM session ORDER BY started_at DESC")
    rows = []
    for s in sessions:
        sc = cost.session_cost(s["id"]).to_dict()
        rows.append({"session": dict(s), "cost": sc})
    blocked = [r for r in rows if r["cost"]["status"] == "blocked"]
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="budget.html",
        context={
            "project": active.to_dict(),
            "rows": rows,
            "blocked": blocked,
            "cap": (rows[0]["cost"]["budget_cap_usd"] if rows else None),
            "usd_rate": rate,
            "unpriced": cost.unpriced_models(pid),
        },
    )


# ---------------------------------------------------------------------------
# REST：状态查询
# ---------------------------------------------------------------------------
@api_router.get("/budget/status", dependencies=[Depends(read_security)])
def budget_status_api(session_id: str = ""):
    if not session_id:
        raise BizError("C400001", msg="缺少 session_id")
    try:
        sc = cost.session_cost(session_id)
    except BizError as exc:
        # 会话不存在等 → 透传既有错误码（如 A010001 经 _active_pid，或会话无记录）
        raise exc
    return ok(sc.to_dict())


# ---------------------------------------------------------------------------
# REST：超限确认放行
# ---------------------------------------------------------------------------
@api_router.post("/budget/confirm", dependencies=[Depends(write_security)])
async def budget_confirm_api(req: Request):
    data = await _parse_body(req)
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise BizError("C400001", msg="缺少 session_id（确认放行的会话）")
    try:
        sc = cost.confirm_continue(session_id, actor="user")
    except BizError as exc:
        # M36/P1-B4：失败仍用带 #quota-modal-{session_id} 锚点的弹层片段，
        # 避免 outerHTML 把锚点整个顶掉后用户无处重试。
        if not _is_htmx(req):
            raise
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="_quota_modal.html",
            context={
                "session_id": session_id,
                "cost": None,
                "error": exc.msg,
                "error_code": exc.code,
                "usd_rate": _rate(),
            },
        )
    result = sc.to_dict()
    if _is_htmx(req):
        # 确认后弹层收起：返回该会话更新后的状态块（status 应已 blocked→warn）
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="_quota_modal.html",
            context={
                "session_id": session_id,
                "cost": result,
                "confirmed": True,
                "usd_rate": _rate(),
            },
        )
    return ok(result)
