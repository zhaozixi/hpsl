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

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import db, projects as proj
from ..engine import state as st
from ..engine import claim_check as cc
from ..engine import grading as gr
from ..engine import ledger as lg
from ..common.enums import AttemptKind, UnitState
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
# P0-A 主链写入（建单元 / 产出 / 尝试 / 闭环）
# 纪律：全部经 db.write；零 DDL；不新增错误码；状态推进仍只经 state.move。
# ---------------------------------------------------------------------------
def _get_unit(pid: str, uid: str) -> dict:
    rows = db.query(pid, "SELECT * FROM unit WHERE id=?", (uid,))
    if not rows:
        raise BizError("A020001", msg=f"单元 {uid} 不存在")
    return rows[0]


def _as_score(value, field: str) -> Optional[float]:
    """把 0~1 的分数（JSON 数字或表单字符串）归一为 float；空 → None。"""
    if value is None or value == "":
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise BizError("C400001", msg=f"{field} 须为 0~1 的数字")
    if not (0.0 <= score <= 1.0):
        raise BizError("C400001", msg=f"{field} 须在 [0,1] 区间")
    return score


@api_router.post("/units", dependencies=[Depends(write_security)])
async def create_unit_api(req: Request):
    """建单元（INIT）。P0-01：此前没有任何创建入口。"""
    pid = _require_pid()
    data = await _parse_body(req)
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise BizError("C400001", msg="title 必填（1~200 字符）")
    title = title.strip()
    if len(title) > 200:
        raise BizError("C400001", msg="title 过长（≤200 字符）")
    band = data.get("difficulty_band")
    if band in (None, ""):
        band = None
    if band is not None and band not in ("稳妥", "拉伸", "回补"):
        raise BizError("C400001", msg="difficulty_band 仅支持 稳妥/拉伸/回补")
    uid = uuid.uuid4().hex
    db.write(
        pid,
        "unit",
        {
            "id": uid,
            "title": title,
            "status": UnitState.INIT.value,
            "difficulty_band": band,
        },
        "user",
    )
    if _is_htmx(req):
        return HTMLResponse(
            "<p class='ok'>已创建单元，正在刷新…</p>", headers={"HX-Refresh": "true"}
        )
    return ok(
        {
            "id": uid,
            "title": title,
            "status": UnitState.INIT.value,
            "difficulty_band": band,
            "allowed_next": _allowed_next(UnitState.INIT.value),
        }
    )


@api_router.post("/units/{uid}/production", dependencies=[Depends(write_security)])
async def unit_production_api(req: Request, uid: str):
    """提交单元产出（回归主线）。produced_at 由进入 VERIFY 时的 move 写入。"""
    pid = _require_pid()
    _get_unit(pid, uid)
    data = await _parse_body(req)
    production = data.get("production")
    if not isinstance(production, str) or not production.strip():
        raise BizError("C400001", msg="production 不能为空（写一句产出内容）")
    if len(production) > 20000:
        raise BizError("C400001", msg="production 过长（≤20000 字符）")
    db.write(pid, "unit", {"id": uid, "production": production}, "user")
    if _is_htmx(req):
        return HTMLResponse("<p class='ok'>产出已保存。下一步：推进到「验证」，再记录重述/边界题得分。</p>")
    return ok({"unit_id": uid, "length": len(production)})


@api_router.post("/units/{uid}/attempts", dependencies=[Depends(write_security)])
async def create_attempt_api(req: Request, uid: str):
    """记录一次尝试（重述 / 边界 / 追问 / 复测）及其自评、确认得分。"""
    pid = _require_pid()
    _get_unit(pid, uid)
    data = await _parse_body(req)
    kind = data.get("kind")
    valid_kinds = [k.value for k in AttemptKind]
    if kind not in valid_kinds:
        raise BizError("C400001", msg=f"kind 仅支持 {'/'.join(valid_kinds)}")
    self_score = _as_score(data.get("self_score"), "self_score")
    confirmed_score = _as_score(data.get("confirmed_score"), "confirmed_score")
    concept_id = data.get("concept_id") or None
    if concept_id:
        if not db.query(pid, "SELECT id FROM concept WHERE id=?", (concept_id,)):
            raise BizError("C400001", msg="concept_id 不存在")
    rubric = data.get("rubric")
    if rubric is not None and not isinstance(rubric, dict):
        raise BizError("C400001", msg="rubric 需为对象（评分要点）")
    seconds = data.get("seconds")
    if seconds is not None:
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            raise BizError("C400001", msg="seconds 须为整数")
    attempt_id = uuid.uuid4().hex
    db.write(
        pid,
        "attempt",
        {
            "id": attempt_id,
            "unit_id": uid,
            "concept_id": concept_id,
            "kind": kind,
            "rubric": rubric,
            "self_score": self_score,
            "confirmed_score": confirmed_score,
            "seconds": seconds,
            "ts": db._now_utc(),
        },
        "user",
    )
    if _is_htmx(req):
        return HTMLResponse(f"<p class='ok'>已记录一次「{kind}」。</p>")
    return ok({"attempt_id": attempt_id, "kind": kind})


@api_router.post("/units/{uid}/close", dependencies=[Depends(write_security)])
async def close_unit_api(req: Request, uid: str):
    """闭环（JUDGE→INTEGRATE）。成功后自动入队延迟复测。"""
    pid = _require_pid()
    data = await _parse_body(req)
    score = _as_score(data.get("confirmed_score"), "confirmed_score")
    if score is None:
        raise BizError("A050004", msg="确认得分缺失，不可闭环")
    reason = data.get("close_reason")
    if not isinstance(reason, str):
        reason = None
    same_source = data.get("same_source")
    if same_source not in (True, False, None):
        same_source = None
    vo = gr.close_unit(
        pid,
        uid,
        score,
        force_close=bool(data.get("force_close")),
        close_reason=reason,
        is_keypoint=bool(data.get("is_keypoint")),
        same_source=same_source,
        explicit_confirm=bool(data.get("explicit_confirm")),
    )
    enqueued = gr.enqueue_unit_reviews(pid, uid)
    payload = vo.to_dict()
    payload["reviews_enqueued"] = len(enqueued)
    payload["review_ids"] = enqueued
    if _is_htmx(req):
        panel = _TEMPLATES.TemplateResponse(
            request=req,
            name="_unit_wf_panel.html",
            context={
                "unit": {"id": uid, "status": payload["status"]},
                "allowed_next": payload["allowed_next"],
            },
        )
        msg = (
            f"已闭环，复测入队 {len(enqueued)} 条（2 天后到期）。"
            if enqueued
            else "已闭环；本单元尚未关联概念，暂无复测。"
        )
        oob = f'<div id="close-result" hx-swap-oob="true"><p class="ok">{msg}</p></div>'
        return HTMLResponse(panel.body.decode("utf-8") + oob)
    return ok(payload)


# ---------------------------------------------------------------------------
# HTMX 片段模板（面板替换，保持 #wf-panel / #cc-{cid} 锚点）
# - _unit_wf_panel.html：工作流面板（供页面 include 与 move 端点 outerHTML 替换共用）
# - _unit_cc_row.html：单条挑错行（供页面 include 与 arbitrate 端点 outerHTML 替换共用）
# ---------------------------------------------------------------------------
