"""洞察与周盘点（P2-05 / P2-06 / P2-07）：难度建议 + 验证衰减 + 一屏批量裁决。

REST（api_router，prefix=/api/v1）：
- GET  /bands：返回 {counts, band}；band = governance.suggest_band(...)（四平面优先级裁决）。
- GET  /concepts/decay：概念验证状态「读时」衰减列表（engine.decay，只读）。
- POST /gaps/status-batch：缺口状态批量更新（逐条 db.write，actor=user）。

页面（page_router，Jinja2 + HTMX）：
- GET /weekly：周盘点页。列出 level∈{2,3} 且 status=待补 的缺口（含单元标题）与 level=1 积压，
  每行一个 `select[name=status]`（仅 已补 / 废弃），HTMX 表单提交到 /api/v1/gaps/status-batch，
  成功后由服务端返回 `HX-Refresh: true` 整页刷新。

纪律（冻结）：
- 零 DDL：只读 concept / 写既有 gap 表（经 db.write 唯一入口，同事务审计）；16 库对象不变。
- 不新增错误码：复用 A010001（无激活项目）、C400001（参数 / 非法状态）、C403001 / C429001（安全层）。
- 不改 app.py：由主代理挂载本模块的 `api_router` / `page_router`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import db, projects as proj
from ..common.errors import BizError
from ..common.result import ok
from ..db import _now_utc
from ..engine import decay, governance
from .security import read_security, write_security

api_router = APIRouter(prefix="/api/v1")
page_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# 批量裁决允许的状态（「验证中」等流程态不在此列；复用既有 gap.status 枚举字面量）
_ALLOWED_GAP_STATUSES = ("已补", "已验证", "废弃")


# ---------------------------------------------------------------------------
# 公共助手
# ---------------------------------------------------------------------------
def _require_pid() -> str:
    """校验存在激活项目；否则 A010001（与 review / graph 路由同构）。"""
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目")
    return active.id


def _is_htmx(req: Request) -> bool:
    return req.headers.get("hx-request") is not None


def _batch_fragment(updated: int, skipped: int) -> HTMLResponse:
    """HTMX 批量提交成功片段：提示 + HX-Refresh 触发整页刷新（页面重新拉取待补列表）。"""
    return HTMLResponse(
        f"<p class='ok'>已更新 {updated} 条缺口，跳过 {skipped} 条；正在刷新…</p>",
        headers={"HX-Refresh": "true"},
    )


# ---------------------------------------------------------------------------
# REST：GET /api/v1/bands（难度建议）
# ---------------------------------------------------------------------------
@api_router.get("/bands", dependencies=[Depends(read_security)])
def bands_api():
    """返回当前项目的四平面计数与难度建议（只读重算，零 DDL）。"""
    pid = _require_pid()
    counts = governance.compute_counts(pid)
    cfg = proj.get_effective_config(pid)
    ctx = governance.counts_context(pid)
    band = governance.suggest_band(counts, cfg, ctx=ctx)
    return ok({"counts": counts.to_dict(), "band": band})


# ---------------------------------------------------------------------------
# REST：GET /api/v1/concepts/decay（验证状态衰减）
# ---------------------------------------------------------------------------
@api_router.get("/concepts/decay", dependencies=[Depends(read_security)])
def concepts_decay_api():
    """按 REVIEW_INTERVALS=(2,7,21) 读时计算概念有效验证状态（不写库）。"""
    pid = _require_pid()
    return ok(decay.decayed_concepts(pid))


# ---------------------------------------------------------------------------
# 页面：GET /weekly（周盘点一屏批量裁决）
# ---------------------------------------------------------------------------
_REVIEW_GAPS_SQL = (
    "SELECT gap.id, gap.unit_id, gap.level, gap.text, gap.status, gap.created_at, "
    "unit.title AS unit_title "
    "FROM gap LEFT JOIN unit ON gap.unit_id = unit.id "
    "WHERE gap.level IN (2, 3) AND gap.status = '待补' "
    "ORDER BY gap.level DESC, gap.created_at ASC, gap.id ASC"
)
_BACKLOG_L1_SQL = (
    "SELECT gap.id, gap.unit_id, gap.level, gap.text, gap.status, gap.created_at, "
    "unit.title AS unit_title "
    "FROM gap LEFT JOIN unit ON gap.unit_id = unit.id "
    "WHERE gap.level = 1 AND gap.status = '待补' "
    "ORDER BY gap.created_at ASC, gap.id ASC"
)


@page_router.get("/weekly", dependencies=[Depends(read_security)])
def weekly_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="weekly.html",
            context={"error": "尚未选择项目，请先在首页选择或新建项目"},
        )
    pid = active.id
    review_gaps = db.query(pid, _REVIEW_GAPS_SQL)
    backlog_l1 = db.query(pid, _BACKLOG_L1_SQL)

    counts = governance.compute_counts(pid)
    cfg = proj.get_effective_config(pid)
    ctx = governance.counts_context(pid)
    band = governance.suggest_band(counts, cfg, ctx=ctx)

    return _TEMPLATES.TemplateResponse(
        request=req,
        name="weekly.html",
        context={
            "project": active.to_dict(),
            "gaps": review_gaps,
            "backlog_l1": backlog_l1,
            "band": band,
            "stale_concepts": sum(
                1
                for c in decay.decayed_concepts(pid)
                if c["effective_status"] == decay.STATUS_UNVERIFIED
                and c["last_verified_at"] is not None
            ),
        },
    )


# ---------------------------------------------------------------------------
# REST：POST /api/v1/gaps/status-batch（批量裁决）
# ---------------------------------------------------------------------------
def _normalize_json_items(body: Any) -> list[dict]:
    """JSON 入参归一：{"items": [{"id", "status"}, ...]}。"""
    if not isinstance(body, dict):
        raise BizError("C400001", msg="请求体需为 JSON 对象")
    items = body.get("items")
    if not isinstance(items, list):
        raise BizError("C400001", msg="items 必须为数组")
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            raise BizError("C400001", msg="items 的每一项都必须是对象")
        out.append({"id": it.get("id"), "status": it.get("status")})
    return out


def _items_from_form(form) -> list[dict]:
    """表单入参归一（HTMX 默认 form-urlencoded）。

    页面每行写入一个 `input[name=gid]` + 一个 `select[name=status]`，两者按 DOM 顺序一一对应，
    分别取 getlist 后 zip；同时兼容 `status-<gid>` 形式的字段名。
    """
    gids = list(form.getlist("gid")) or list(form.getlist("gap_id"))
    statuses = list(form.getlist("status"))
    pairs = list(zip(gids, statuses))
    if not pairs:
        for key in form.keys():
            if key.startswith("status-"):
                pairs.append((key[len("status-"):], form.get(key)))
    return [{"id": gid, "status": status} for gid, status in pairs]


async def _parse_batch_items(req: Request) -> list[dict]:
    """兼容 JSON（REST / 测试）与表单（HTMX）两种提交方式。"""
    content_type = req.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await req.json()
        except Exception:
            raise BizError("C400001", msg="请求体需为 JSON")
        return _normalize_json_items(body)
    try:
        form = await req.form()
    except Exception:
        raise BizError("C400001", msg="请求体需为 JSON 或表单")
    return _items_from_form(form)


@api_router.post("/gaps/status-batch", dependencies=[Depends(write_security)])
async def gaps_status_batch_api(req: Request):
    """批量更新缺口状态：逐条 db.write(pid, "gap", {id,status,updated_at}, "user")。

    - status 仅允许 已补 / 已验证 / 废弃；非法 → C400001（先全量校验，避免部分写入）。
    - 不存在的 id 安全跳过（幂等重放），计入 skipped；同一 id 重复出现只处理一次。
    - 返回 {"updated": n, "skipped": m}；HTMX 请求额外返回 HX-Refresh: true。
    """
    pid = _require_pid()
    raw_items = await _parse_batch_items(req)

    # 先校验全部状态，保证「非法即整体拒绝」而非中途留下部分写入
    for it in raw_items:
        if it.get("status") not in _ALLOWED_GAP_STATUSES:
            raise BizError("C400001", msg="status 仅允许 已补 / 已验证 / 废弃")

    known_ids = {r["id"] for r in db.query(pid, "SELECT id FROM gap")}
    updated = skipped = 0
    seen: set[str] = set()
    for it in raw_items:
        gid = it.get("id")
        if not isinstance(gid, str) or not gid.strip():
            raise BizError("C400001", msg="每条 items 需提供缺口 id")
        gid = gid.strip()
        if gid not in known_ids or gid in seen:
            skipped += 1
            continue
        seen.add(gid)
        db.write(
            pid,
            "gap",
            {"id": gid, "status": it["status"], "updated_at": _now_utc()},
            "user",
        )
        updated += 1

    if _is_htmx(req):
        return _batch_fragment(updated, skipped)
    return ok({"updated": updated, "skipped": skipped})
