"""M10B 第二批：`/review` 复测页（消费 M6 start_review/submit_review_by_id；§3.2.M10B2）。

服务端渲染（Jinja2 + HTMX），消费 M6 复测队列引擎：
- GET /review：列出当前激活项目到期复测（due_reviews）。
- POST /api/v1/reviews/{id}/start：开始复测（生成草稿卷），返回 ReviewStartVO
  （question 透传；M10B 不经 LLM，question 缺省 None，页面以自评形态呈现）。
- POST /api/v1/reviews/{id}/submit：提交复测评分（score∈[0,1]），返回 ReviewResultVO。

纪律约束（严守锁定基线）：
- 零 DDL：仅经 engine -> db.write 写 attempt/concept/review 既有表（M0 锁定 16 库对象）。
- 写接口强制 write_security（Origin + CSRF + 限流）；读接口 read_security。
- 无新错误码：复用 A010001（无激活项目）/ A060001（复测条目不存在或已完成）/
  C400001（参数非法）/ C403001（来源不被允许）/ C429001（限流）。
- engine 层保持 headless，不依赖 routes/app。
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from ..templating import make_templates

from .. import projects as proj
from ..engine import review_queue as rq
from ..common.errors import BizError
from ..common.result import ok
from .security import write_security, read_security

api_router = APIRouter(prefix="/api/v1")
page_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = make_templates(str(_TEMPLATES_DIR))


def _require_pid() -> str:
    """校验存在激活项目；否则 A010001（页面渲染「尚未选择项目」，REST 返回错误码）。"""
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目")
    return active.id


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
# 页面：GET /review
# ---------------------------------------------------------------------------
@page_router.get("/review", dependencies=[Depends(read_security)])
def review_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="review.html",
            context={"error": "尚未选择项目，请先在首页选择或新建项目"},
        )
    pid = active.id
    items = [it.to_dict() for it in rq.due_reviews(pid)]
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="review.html",
        context={"project": active.to_dict(), "items": items},
    )


# ---------------------------------------------------------------------------
# API：GET /api/v1/reviews/due
# ---------------------------------------------------------------------------
@api_router.get("/reviews/due", dependencies=[Depends(read_security)])
def reviews_due_api():
    pid = _require_pid()
    items = [it.to_dict() for it in rq.due_reviews(pid)]
    return ok(items)


# ---------------------------------------------------------------------------
# API：POST /api/v1/reviews/{review_id}/start
# ---------------------------------------------------------------------------
@api_router.post("/reviews/{review_id}/start", dependencies=[Depends(write_security)])
async def start_review_api(req: Request, review_id: str):
    pid = _require_pid()
    data = await _parse_body(req)
    question = data.get("question") if isinstance(data.get("question"), str) else None
    try:
        vo = rq.start_review(pid, review_id, question=question)
    except BizError as exc:
        # M36/P1-B4：失败也要返回带 #panel-{review_id} 锚点的片段。
        # 否则 HTMX 用裸错误片段 outerHTML 掉整个面板，后续重试/提交都失去落点。
        if not _is_htmx(req):
            raise
        return _start_error_fragment(review_id, exc.msg, exc.code)
    if _is_htmx(req):
        return _start_fragment(vo.review_id, vo.question)
    # ReviewStartVO 无 to_dict（引擎未提供），此处手动构造
    return ok(
        {
            "review_id": vo.review_id,
            "concept_id": vo.concept_id,
            "concept_name": vo.concept_name,
            "question": vo.question,
            "due": vo.due,
            "interval_days": vo.interval_days,
            "attempt_id": vo.attempt_id,
        }
    )


# ---------------------------------------------------------------------------
# API：POST /api/v1/reviews/{review_id}/submit
# ---------------------------------------------------------------------------
@api_router.post("/reviews/{review_id}/submit", dependencies=[Depends(write_security)])
async def submit_review_api(req: Request, review_id: str):
    pid = _require_pid()
    data = await _parse_body(req)
    raw = data.get("score")
    if raw is None or raw == "":
        raise BizError("C400001", msg="缺少评分 score")
    try:
        score = float(raw)
    except (TypeError, ValueError):
        raise BizError("C400001", msg="score 须为数字")
    if not (0.0 <= score <= 1.0):
        raise BizError("C400001", msg="score 须在 [0,1] 区间")
    seconds = data.get("seconds")
    if seconds is not None:
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            seconds = None
    vo = rq.submit_review_by_id(pid, review_id, score, seconds=seconds, actor="user")
    if _is_htmx(req):
        return _result_fragment(vo.review_id, vo.score, vo.passed, vo.next_due)
    return ok(vo.to_dict())


# ---------------------------------------------------------------------------
# HTMX 片段（面板替换，保持 #panel-{review_id} 锚点）
# ---------------------------------------------------------------------------
def _start_error_fragment(review_id: str, msg: str, code: str) -> HTMLResponse:
    """开始复测失败：保留 #panel-{review_id} 锚点，错误就地展示并提供重试。"""
    rid = html.escape(review_id, quote=True)
    return HTMLResponse(
        f'<div id="panel-{rid}" aria-live="polite">'
        f'<p class="result err">{html.escape(msg)}（{html.escape(code)}）</p>'
        f'<button type="button" class="btn btn-sm" '
        f'hx-post="/api/v1/reviews/{rid}/start" '
        f'hx-target="#panel-{rid}" hx-swap="outerHTML">重试</button>'
        f"</div>"
    )


def _start_fragment(review_id: str, question: Optional[str]) -> HTMLResponse:
    """开始复测后返回评分表单片段（包裹于 #panel-{review_id}，供 outerHTML 替换）。"""
    # C10：question 可能来自 LLM / 用户输入，拼进 HTML 前必须转义。
    q = (
        f'<p class="prompt prompt-info"><span class="badge">复测卷</span>'
        f'<span class="msg">{html.escape(question)}</span></p>'
        if question
        else ""
    )
    frag = f"""
<div id="panel-{review_id}" aria-live="polite">
  {q}
  <form hx-post="/api/v1/reviews/{review_id}/submit"
        hx-target="#panel-{review_id}" hx-swap="outerHTML">
    <p>
      <label for="score-{review_id}">自评掌握程度（0–1）</label><br>
      <input type="number" step="0.01" min="0" max="1" id="score-{review_id}" name="score" required>
    </p>
    <p><button type="submit">提交复测</button></p>
  </form>
</div>
"""
    return HTMLResponse(frag)


def _result_fragment(review_id: str, score: float, passed: bool, next_due: str) -> HTMLResponse:
    """提交复测后返回结果片段（包裹于 #panel-{review_id}）。"""
    verdict = "通过" if passed else "未通过"
    cls = "ok" if passed else "notice"
    frag = f"""
<div id="panel-{review_id}" aria-live="polite">
  <p class="{cls}">{verdict}（得分 {score:.2f}）</p>
  <p><small>下次复测：{next_due}</small></p>
  <p><small>该复测已推进至下一间隔档，活跃复测数可在账本总览查看。</small></p>
</div>
"""
    return HTMLResponse(frag)
