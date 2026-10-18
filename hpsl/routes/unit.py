"""M10B 第三批：`/unit` 单元详情 + 工作流页（消费 M2 state.move / M5 claim_check / M7 ledger）。

服务端渲染（Jinja2 + HTMX），提供：
- GET /unit：当前激活项目单元索引（只读查询）。
- GET /unit/{uid}：单元详情 + 工作流（M2）+ 缺口（M7）+ 挑错（M5）。
- GET /api/v1/units：单元列表 REST。
- GET /api/v1/units/{uid}：单元详情 REST（含 allowed_next / gaps / claim_checks）。
- POST /api/v1/units/{uid}/move：驱动状态机（write_security）。
- POST /api/v1/units/{uid}/claim-check/{cid}/arbitrate：挑错仲裁（write_security）。

纪律约束（严守锁定基线）：
- 零 DDL：仅经 engine -> db.write 写 unit/claim_check 既有表（M0 锁定 16 库对象）。
- 写接口强制 write_security；读接口 read_security。
- 无新错误码：复用 A010001 / A020001 / A020003 / C400001。
- engine 层保持 headless，不依赖 routes/app（本批次不改 engine）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import db, projects as proj
from ..engine import state as st
from ..engine import claim_check as cc
from ..engine import ledger as lg
from ..common.enums import UnitState
from ..common.errors import BizError
from ..common.result import ok
from .security import write_security, read_security

api_router = APIRouter(prefix="/api/v1")
page_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# 通用助手
# ---------------------------------------------------------------------------
def _require_pid() -> str:
    """校验存在激活项目；否则 A010001（REST 返回错误码）。"""
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目")
    return active.id


def _allowed_next(status_str: str) -> list[str]:
    """由当前 status 经 state.TRANSITIONS 推算可用下一状态（只读，不调用 move）。"""
    try:
        s = UnitState(status_str)
    except ValueError:
        return []
    return [x.value for x in st.TRANSITIONS.get(s, ())]


async def _parse_body(req: Request) -> dict:
    """兼容 JSON 与表单（HTMX 默认 form 提交），统一成 dict。"""
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


# ---------------------------------------------------------------------------
# 页面：GET /unit（单元索引）
# ---------------------------------------------------------------------------
@page_router.get("/unit", dependencies=[Depends(read_security)])
def unit_index_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="unit_index.html",
            context={"error": "尚未选择项目，请先在首页选择或新建项目"},
        )
    pid = active.id
    units = db.query(
        pid,
        "SELECT id,title,status,forced_close,closed_at FROM unit ORDER BY id ASC",
    )
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="unit_index.html",
        context={"project": active.to_dict(), "units": units},
    )


# ---------------------------------------------------------------------------
# 页面：GET /unit/{uid}（单元详情 + 工作流）
# ---------------------------------------------------------------------------
@page_router.get("/unit/{uid}", dependencies=[Depends(read_security)])
def unit_detail_page(req: Request, uid: str):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="unit_detail.html",
            context={"error": "尚未选择项目，请先在首页选择或新建项目"},
        )
    pid = active.id
    rows = db.query(pid, "SELECT * FROM unit WHERE id=?", (uid,))
    if not rows:
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="unit_detail.html",
            context={"project": active.to_dict(), "error": f"单元 {uid} 不存在"},
        )
    unit = rows[0]
    status = unit["status"]
    allowed_next = _allowed_next(status)
    gaps = [g.to_dict() for g in lg.list_gaps(pid, unit_id=uid)]
    claim_checks = db.query(pid, "SELECT * FROM claim_check WHERE unit_id=?", (uid,))
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="unit_detail.html",
        context={
            "project": active.to_dict(),
            "unit": unit,
            "allowed_next": allowed_next,
            "gaps": gaps,
            "claim_checks": claim_checks,
        },
    )


# ---------------------------------------------------------------------------
# API：GET /api/v1/units
# ---------------------------------------------------------------------------
@api_router.get("/units", dependencies=[Depends(read_security)])
def units_api():
    pid = _require_pid()
    units = db.query(
        pid,
        "SELECT id,title,status,forced_close,closed_at FROM unit ORDER BY id ASC",
    )
    return ok(units)


# ---------------------------------------------------------------------------
# API：GET /api/v1/units/{uid}
# ---------------------------------------------------------------------------
@api_router.get("/units/{uid}", dependencies=[Depends(read_security)])
def unit_detail_api(uid: str):
    pid = _require_pid()
    rows = db.query(pid, "SELECT * FROM unit WHERE id=?", (uid,))
    if not rows:
        raise BizError("A020001", msg=f"单元 {uid} 不存在")
    unit = rows[0]
    status = unit["status"]
    allowed_next = _allowed_next(status)
    gaps = [g.to_dict() for g in lg.list_gaps(pid, unit_id=uid)]
    claim_checks = db.query(pid, "SELECT * FROM claim_check WHERE unit_id=?", (uid,))
    return ok(
        {
            "unit": unit,
            "allowed_next": allowed_next,
            "gaps": gaps,
            "claim_checks": claim_checks,
        }
    )


# ---------------------------------------------------------------------------
# API：POST /api/v1/units/{uid}/move（驱动状态机）
# ---------------------------------------------------------------------------
@api_router.post("/units/{uid}/move", dependencies=[Depends(write_security)])
async def move_unit_api(req: Request, uid: str):
    pid = _require_pid()
    data = await _parse_body(req)
    cur = data.get("cur")
    nxt = data.get("nxt")
    if not isinstance(cur, str) or not cur:
        raise BizError("C400001", msg="缺少 cur（当前状态）")
    if not isinstance(nxt, str) or not nxt:
        raise BizError("C400001", msg="缺少 nxt（目标状态）")
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    expected_version = (
        data.get("expected_version") if isinstance(data.get("expected_version"), str) else None
    )
    vo = st.move(
        pid,
        uid,
        cur,
        nxt,
        reason=reason,
        actor="user",
        expected_version=expected_version,
    )
    result = vo.to_dict()
    if _is_htmx(req):
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="_unit_wf_panel.html",
            context={"unit": {"id": uid, "status": result["status"]}, "allowed_next": result["allowed_next"]},
        )
    return ok(result)


# ---------------------------------------------------------------------------
# API：POST /api/v1/units/{uid}/claim-check/{cid}/arbitrate（挑错仲裁）
# ---------------------------------------------------------------------------
@api_router.post("/units/{uid}/claim-check/{cid}/arbitrate", dependencies=[Depends(write_security)])
async def arbitrate_cc_api(req: Request, uid: str, cid: str):
    pid = _require_pid()
    data = await _parse_body(req)
    ruling = data.get("ruling")
    if not isinstance(ruling, str) or not ruling:
        raise BizError("C400001", msg="缺少 ruling（仲裁结论）")
    cc.arbitrate(pid, claim_id=cid, ruling=ruling)
    if _is_htmx(req):
        rows = db.query(pid, "SELECT * FROM claim_check WHERE id=?", (cid,))
        if not rows:
            return HTMLResponse(f'<div id="cc-{cid}"><p class="notice">挑错 {cid} 不存在</p></div>')
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="_unit_cc_row.html",
            context={"unit": {"id": uid}, "cc": rows[0]},
        )
    return ok({"claim_id": cid, "ruling": ruling})


# ---------------------------------------------------------------------------
# HTMX 片段模板（面板替换，保持 #wf-panel / #cc-{cid} 锚点）
# - _unit_wf_panel.html：工作流面板（供页面 include 与 move 端点 outerHTML 替换共用）
# - _unit_cc_row.html：单条挑错行（供页面 include 与 arbitrate 端点 outerHTML 替换共用）
# ---------------------------------------------------------------------------
