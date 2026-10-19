"""M10B 第三批：`/unit` 单元详情 + 工作流页（消费 M2 state.move / M5 claim_check / M7 ledger）。

服务端渲染（Jinja2 + HTMX），提供：
- GET /unit：当前激活项目单元索引（只读查询）。
- GET /unit/{uid}：单元详情 + 工作流（M2）+ 缺口（M7）+ 挑错（M5）。
- GET /api/v1/units：单元列表 REST。
- GET /api/v1/units/{uid}：单元详情 REST（含 allowed_next / gaps / claim_checks）。
- POST /api/v1/units/{uid}/move：驱动状态机（write_security）。
- POST /api/v1/units/{uid}/claim-check/{cid}/arbitrate：挑错仲裁（write_security）。
- POST /api/v1/units/{uid}/three-questions：记录论文口径的固定三问（write_security）。

纪律约束（严守锁定基线）：
- 零 DDL：仅经 engine -> db.write 写 unit/claim_check 既有表（M0 锁定 16 库对象）。
- 写接口强制 write_security；读接口 read_security。
- 无新错误码：复用 A010001 / A020001 / A020003 / C400001。
- engine 层保持 headless，不依赖 routes/app（本批次不改 engine）。
"""

from __future__ import annotations

import html as _html
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from ..templating import make_templates

from .. import db, projects as proj
from ..engine import state as st
from ..engine import claim_check as cc
from ..engine import grading as gr
from ..engine import ledger as lg
from ..common.enums import AttemptKind, UnitState
from ..common.errors import BizError
from ..common.result import ok
from ._htmx import refresh_with_toast
from .security import write_security, read_security

api_router = APIRouter(prefix="/api/v1")
page_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = make_templates(str(_TEMPLATES_DIR))


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


# 固定三问（论文 4.2.1 口径）：字段名 → 人读文案。判据固定，跨单元才可比。
_THREE_QUESTION_FIELDS = (
    ("what_problem", "它解决什么问题"),
    ("how_it_works", "它怎么工作"),
    ("when_not_applicable", "什么条件不适用"),
)
_THREE_QUESTION_MAX = 1000


def _three_questions_payload(data: dict) -> dict:
    """校验固定三问：三项均为 1~1000 字符（缺失 / 空 / 超长 → C400001）。"""
    out: dict[str, str] = {}
    for key, label in _THREE_QUESTION_FIELDS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise BizError("C400001", msg=f"{label}（{key}）必填，1~1000 字符")
        value = value.strip()
        if len(value) > _THREE_QUESTION_MAX:
            raise BizError(
                "C400001", msg=f"{label}（{key}）过长（≤{_THREE_QUESTION_MAX} 字符）"
            )
        out[key] = value
    return out


def _latest_three_questions(pid: str, uid: str) -> Optional[dict]:
    """取该单元最近一条 kind='三问'，解析 rubric['three_questions']（只读）。

    `db.query` 不自动解码 JSON 列，这里按需解析；历史 data 若损坏则视为无记录，
    页面降级为空态而不是 500。
    """
    rows = db.query(
        pid,
        "SELECT rubric, ts FROM attempt WHERE unit_id=? AND kind='三问' "
        "ORDER BY ts DESC, rowid DESC LIMIT 1",
        (uid,),
    )
    if not rows:
        return None
    raw = rows[0].get("rubric")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    tq = raw.get("three_questions")
    if not isinstance(tq, dict):
        return None
    out = {key: tq.get(key, "") for key, _label in _THREE_QUESTION_FIELDS}
    out["recorded_at"] = _local_ts(rows[0].get("ts"))
    return out


def _as_bool(value) -> bool:
    """表单/JSON 布尔归一：'false'/'0'/'no'/'off' 为 False（避免 bool('false')==True）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _local_ts(value):
    """UTC ISO 串 → 本地时间 `YYYY-MM-DD HH:MM`；无法解析时原样返回。"""
    if not value or not isinstance(value, str):
        return value
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return value


def _close_panel_oob(uid: str, status: str) -> str:
    """渲染「判定闭环」区并标记 OOB 替换。

    该区的**可操作态**由状态派生（仅 JUDGE 可提交），而工作流面板走局部替换；
    若不随之刷新，推进到 JUDGE 后闭环按钮会一直停留在禁用态，单元无法闭环。
    """
    body = _TEMPLATES.env.get_template("_unit_close_panel.html").render(
        unit={"id": uid, "status": status}
    )
    return body.replace('<div id="close-panel">', '<div id="close-panel" hx-swap-oob="true">', 1)


def _wf_panel_error(req: Request, pid: str, uid: str, msg: str, fallback_status: str):
    """HTMX 面板操作失败时，仍返回带 `#wf-panel` 锚点的片段。

    面板用 `hx-swap="outerHTML"` 替换，若失败直接回 JSON（或裸错误片段），
    锚点会被顶掉、后续所有按钮消失。这里按库中真实状态重建面板并附错误文案。
    """
    rows = db.query(pid, "SELECT status FROM unit WHERE id=?", (uid,))
    status = rows[0]["status"] if rows else fallback_status
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="_unit_wf_panel.html",
        context={
            "unit": {"id": uid, "status": status},
            "allowed_next": _allowed_next(status),
            "error": msg,
        },
    )


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
    # 时间戳在库里是 UTC ISO 串；页面直接展示会让人误读时区，这里换成本地时间
    # （REST API 仍返回原始 ISO，便于程序处理）
    for _k in ("produced_at", "closed_at", "created_at"):
        if _k in unit:
            unit[_k] = _local_ts(unit[_k])
    status = unit["status"]
    allowed_next = _allowed_next(status)
    gaps = [g.to_dict() for g in lg.list_gaps(pid, unit_id=uid)]
    claim_checks = db.query(pid, "SELECT * FROM claim_check WHERE unit_id=?", (uid,))
    turns = db.list_turns(pid, unit_id=uid, limit=100)
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="unit_detail.html",
        context={
            "project": active.to_dict(),
            "unit": unit,
            "allowed_next": allowed_next,
            "gaps": gaps,
            "claim_checks": claim_checks,
            "turns": turns,
            "three_questions": _latest_three_questions(pid, uid),
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
    turns = db.list_turns(pid, unit_id=uid, limit=100)
    return ok(
        {
            "unit": unit,
            "allowed_next": allowed_next,
            "gaps": gaps,
            "claim_checks": claim_checks,
            "turns": turns,
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
    try:
        vo = st.move(
            pid,
            uid,
            cur,
            nxt,
            reason=reason,
            actor="user",
            expected_version=expected_version,
        )
    except BizError as exc:
        if not _is_htmx(req):
            raise
        return _wf_panel_error(req, pid, uid, exc.msg, cur)
    result = vo.to_dict()
    if _is_htmx(req):
        panel = _TEMPLATES.TemplateResponse(
            request=req,
            name="_unit_wf_panel.html",
            context={"unit": {"id": uid, "status": result["status"]}, "allowed_next": result["allowed_next"]},
        )
        # 闭环区依赖当前状态（仅 JUDGE 可闭环），面板是局部替换，必须同步刷新
        return HTMLResponse(panel.body.decode("utf-8") + _close_panel_oob(uid, result["status"]))
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
    try:
        cc.arbitrate(pid, claim_id=cid, ruling=ruling)
    except BizError as exc:
        # M36/P1-B4：失败仍返回 _unit_cc_row.html 渲染的整行（保留 #cc-{cid} 锚点），
        # 否则 outerHTML 会把行整个顶掉成裸错误片段，用户无法重试。
        if not _is_htmx(req):
            raise
        rows = db.query(pid, "SELECT * FROM claim_check WHERE id=?", (cid,))
        row = dict(rows[0]) if rows else {
            "id": cid,
            "claim": "",
            "user_ruling": None,
            "residual": 0,
        }
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="_unit_cc_row.html",
            context={
                "unit": {"id": uid},
                "cc": row,
                "error": exc.msg,
                "error_code": exc.code,
            },
        )
    if _is_htmx(req):
        rows = db.query(pid, "SELECT * FROM claim_check WHERE id=?", (cid,))
        if not rows:
            # C10：cid 来自路径参数，拼进 HTML 前先转义（锚点 id 与提示文案同源）。
            cid_esc = _html.escape(cid, quote=True)
            return HTMLResponse(f'<div id="cc-{cid_esc}"><p class="notice">挑错 {cid_esc} 不存在</p></div>')
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
        # 不再用 HX-Refresh 整页刷新：那会让「已创建」提示刚出现就被新页面冲掉，
        # 用户看不到任何确认。改为列表区 OOB 就地替换 + 结果区保留提示。
        units = db.query(
            pid,
            "SELECT id,title,status,forced_close,closed_at FROM unit ORDER BY id ASC",
        )
        snippet = _TEMPLATES.TemplateResponse(
            request=req, name="_unit_list.html", context={"units": units}
        ).body.decode("utf-8")
        oob = snippet.replace(
            '<section id="unit-list-section">',
            '<section id="unit-list-section" hx-swap-oob="true">',
            1,
        )
        msg = f'<p class="ok">已创建单元「{_html.escape(title)}」，已加入下方列表。</p>'
        # 就地刷新不会清空表单，用户容易误以为没提交成功而重复点击；补一段 reset + 聚焦。
        # HTMX 默认 allowScriptEval=true 会执行 swap 进来的 script；若被关闭则只是不清空，无副作用。
        reset = (
            "<script>(function(){var f=document.querySelector('form[hx-post=\"/api/v1/units\"]');"
            "if(!f){return;}f.reset();var i=f.querySelector('input[name=\"title\"]');if(i){i.focus();}})()</script>"
        )
        return HTMLResponse(msg + reset + oob)
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


@api_router.post("/units/{uid}/three-questions", dependencies=[Depends(write_security)])
async def record_three_questions_api(req: Request, uid: str):
    """记录论文口径的固定三问（结构化落 attempt.rubric）。

    请求体：`{what_problem, how_it_works, when_not_applicable}`（各 1~1000 字符）。
    落库：attempt(kind='三问', unit_id=uid, rubric={"three_questions": {...}})。
    HTMX 成功 → 提示片段 + `hpsl:refresh` 事件（保留提示后再刷新）；否则 → `{attempt_id}`。
    """
    pid = _require_pid()
    _get_unit(pid, uid)
    data = await _parse_body(req)
    three_questions = _three_questions_payload(data)

    attempt_id = uuid.uuid4().hex
    db.write(
        pid,
        "attempt",
        {
            "id": attempt_id,
            "unit_id": uid,
            "kind": "三问",
            "rubric": {"three_questions": three_questions},
            "ts": db._now_utc(),
        },
        "user",
    )
    if _is_htmx(req):
        return refresh_with_toast("已记录固定三问")
    return ok({"attempt_id": attempt_id})


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
    try:
        vo = gr.close_unit(
            pid,
            uid,
            score,
            force_close=_as_bool(data.get("force_close")),
            close_reason=reason,
            is_keypoint=_as_bool(data.get("is_keypoint")),
            same_source=same_source,
            explicit_confirm=_as_bool(data.get("explicit_confirm")),
        )
    except BizError as exc:
        if not _is_htmx(req):
            raise
        return _wf_panel_error(req, pid, uid, exc.msg, "JUDGE")
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
        # 先整体替换闭环区（离开 JUDGE 后表单应消失），再单独塞入结果提示；
        # 顺序不可颠倒，否则提示会被随后整体替换的空白 #close-result 清掉。
        oob = _close_panel_oob(uid, payload["status"])
        oob += f'<div id="close-result" hx-swap-oob="true"><p class="ok">{msg}</p></div>'
        return HTMLResponse(panel.body.decode("utf-8") + oob)
    return ok(payload)


# ---------------------------------------------------------------------------
# HTMX 片段模板（面板替换，保持 #wf-panel / #cc-{cid} 锚点）
# - _unit_wf_panel.html：工作流面板（供页面 include 与 move 端点 outerHTML 替换共用）
# - _unit_cc_row.html：单条挑错行（供页面 include 与 arbitrate 端点 outerHTML 替换共用）
# ---------------------------------------------------------------------------
