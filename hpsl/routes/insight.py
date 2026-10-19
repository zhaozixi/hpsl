"""洞察与周盘点（P2-05 / P2-06 / P2-07）：难度建议 + 验证衰减 + 一屏批量裁决。

REST（api_router，prefix=/api/v1）：
- GET  /bands：返回 {counts, band}；band = governance.suggest_band(...)（四平面优先级裁决）。
- GET  /concepts/decay：概念验证状态「读时」衰减列表（engine.decay，只读）。
- POST /gaps/status-batch：缺口状态批量更新（逐条 db.write，actor=user）。

页面（page_router，Jinja2 + HTMX）：
- GET /weekly：周盘点页。列出 level∈{2,3} 且 status=待补 的缺口（含单元标题）与 level=1 积压，
  每行一个 `select[name=status]`（仅 已补 / 废弃），HTMX 表单提交到 /api/v1/gaps/status-batch，
  成功后返回提示片段 + `HX-Trigger: hpsl:refresh`（先提示再整页刷新）。

纪律（冻结）：
- 零 DDL：只读 concept / 写既有 gap 表（经 db.write 唯一入口，同事务审计）；16 库对象不变。
- 不新增错误码：复用 A010001（无激活项目）、C400001（参数 / 非法状态）、C403001 / C429001（安全层）。
- 不改 app.py：由主代理挂载本模块的 `api_router` / `page_router`。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from ..templating import make_templates

from .. import db, projects as proj
from ..common.errors import BizError
from ..common.result import ok
from ..db import _now_utc
from ..engine import decay, governance
from ._htmx import refresh_with_toast
from .security import read_security, write_security

api_router = APIRouter(prefix="/api/v1")
page_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = make_templates(str(_TEMPLATES_DIR))

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


def _batch_fragment(updated: int, skipped: int):
    """HTMX 批量提交成功片段：提示 + HX-Trigger 触发「提示后刷新」。"""
    return refresh_with_toast(f"已更新 {updated} 条缺口，跳过 {skipped} 条；正在刷新…")


# ---------------------------------------------------------------------------
# 缺口元信息（E-07）：推迟到某天 / 需要先补前置；存 gap.context JSON（零 DDL）
# ---------------------------------------------------------------------------
def _context_dict(raw: Any) -> dict:
    """gap.context 为 TEXT JSON（db.query 不自动解析）；坏数据回退空 dict。"""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _defer_date(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def _validate_meta(status: Any, defer_days: Any, prereq: Any) -> None:
    """先全量校验，避免批量中途留下部分写入。"""
    if status not in (None, "") and status not in _ALLOWED_GAP_STATUSES:
        raise BizError("C400001", msg="status 仅允许 已补 / 已验证 / 废弃")
    if defer_days not in (None, ""):
        try:
            days = int(defer_days)
        except (TypeError, ValueError):
            raise BizError("C400001", msg="defer_days 须为 1~365 的整数")
        if not (1 <= days <= 365):
            raise BizError("C400001", msg="defer_days 须为 1~365 的整数")
    if prereq is not None and (not isinstance(prereq, str) or len(prereq) > 200):
        raise BizError("C400001", msg="prereq 须为 ≤200 字符的字符串")


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
    "SELECT gap.id, gap.unit_id, gap.level, gap.text, gap.status, gap.context, gap.created_at, "
    "unit.title AS unit_title "
    "FROM gap LEFT JOIN unit ON gap.unit_id = unit.id "
    "WHERE gap.level IN (2, 3) AND gap.status = '待补' "
    "ORDER BY gap.level DESC, gap.created_at ASC, gap.id ASC"
)
_BACKLOG_L1_SQL = (
    "SELECT gap.id, gap.unit_id, gap.level, gap.text, gap.status, gap.context, gap.created_at, "
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
    review_rows = db.query(pid, _REVIEW_GAPS_SQL)
    backlog_l1 = db.query(pid, _BACKLOG_L1_SQL)

    # E-07：按 gap.context.defer_until 分出「本周处理」与「下周再看」
    today = _today_utc()
    for g in review_rows:
        meta = _context_dict(g.get("context"))
        g["defer_until"] = meta.get("defer_until")
        g["prereq"] = meta.get("prereq")
    review_gaps = [
        g for g in review_rows if not g["defer_until"] or g["defer_until"] <= today
    ]
    deferred = [
        g for g in review_rows if g["defer_until"] and g["defer_until"] > today
    ]

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
            "deferred": deferred,
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
    - 返回 {"updated": n, "skipped": m}；HTMX 请求额外返回提示片段 + HX-Trigger。
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


# ---------------------------------------------------------------------------
# REST：POST /api/v1/gaps/weekly-batch（E-07：状态 + 推迟 + 前置一次提交）
# ---------------------------------------------------------------------------
def _weekly_json_items(body: Any) -> list[dict]:
    if not isinstance(body, dict):
        raise BizError("C400001", msg="请求体需为 JSON 对象")
    raw = body.get("items")
    if not isinstance(raw, list):
        raise BizError("C400001", msg="items 必须为数组")
    out: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            raise BizError("C400001", msg="items 的每一项都必须是对象")
        out.append(
            {
                "id": it.get("id"),
                "status": it.get("status"),
                "defer_days": it.get("defer_days"),
                "prereq": it.get("prereq"),
            }
        )
    return out


def _weekly_form_items(form) -> list[dict]:
    """表单按行字段名归一：`status-<gid>` / `defer-<gid>` / `prereq-<gid>`。"""
    items: dict[str, dict] = {}
    for key, value in form.multi_items():
        for prefix, field in (
            ("status-", "status"),
            ("defer-", "defer_days"),
            ("prereq-", "prereq"),
        ):
            if not key.startswith(prefix):
                continue
            gid = key[len(prefix):].strip()
            if not gid:
                continue
            if field == "status" and not value:
                continue
            if field == "prereq" and not str(value).strip():
                continue
            if field == "defer_days" and not value:
                continue
            items.setdefault(gid, {"id": gid})[field] = value
            break
    return list(items.values())


async def _parse_weekly_items(req: Request) -> list[dict]:
    if "application/json" in req.headers.get("content-type", ""):
        try:
            body = await req.json()
        except Exception:
            raise BizError("C400001", msg="请求体需为 JSON")
        return _weekly_json_items(body)
    try:
        form = await req.form()
    except Exception:
        raise BizError("C400001", msg="请求体需为 JSON 或表单")
    return _weekly_form_items(form)


@api_router.post("/gaps/{gid}/undefer", dependencies=[Depends(write_security)])
def gaps_undefer_api(gid: str, req: Request):
    """取消「下周再看」：清除 gap.context.defer_until（幂等；不存在 → C400001）。"""
    pid = _require_pid()
    if not isinstance(gid, str) or not gid.strip():
        raise BizError("C400001", msg="缺口 id 不能为空")
    gid = gid.strip()
    rows = db.query(pid, "SELECT context FROM gap WHERE id=?", (gid,))
    if not rows:
        raise BizError("C400001", msg=f"缺口不存在：{gid}")
    ctx = _context_dict(rows[0].get("context"))
    had = ctx.pop("defer_until", None) is not None
    db.write(
        pid,
        "gap",
        {
            "id": gid,
            "context": json.dumps(ctx, ensure_ascii=False),
            "updated_at": _now_utc(),
        },
        "user",
    )
    if _is_htmx(req):
        return refresh_with_toast("已取消推迟，缺口回到本周清单")
    return ok({"id": gid, "cleared": had})


@api_router.post("/gaps/weekly-batch", dependencies=[Depends(write_security)])
async def gaps_weekly_batch_api(req: Request):
    """周盘点批量提交：状态 + 推迟天数 + 前置说明，逐条 db.write（审计可撤销）。

    - status ∈ {已补,已验证,废弃}（可省略 = 只改元信息）；
    - defer_days ∈ 1~365（写入 `gap.context.defer_until`，到期自动回到本周清单）；
    - prereq ≤200 字符（写入 `gap.context.prereq`）；
    - 先全量校验再写；不存在 id / 无任何动作 → skipped。
    """
    pid = _require_pid()
    items = await _parse_weekly_items(req)
    for it in items:
        _validate_meta(it.get("status"), it.get("defer_days"), it.get("prereq"))

    known_ids = {r["id"] for r in db.query(pid, "SELECT id FROM gap")}
    updated = skipped = 0
    seen: set[str] = set()
    for it in items:
        gid = it.get("id")
        if not isinstance(gid, str) or not gid.strip():
            raise BizError("C400001", msg="每条 items 需提供缺口 id")
        gid = gid.strip()
        if gid not in known_ids or gid in seen:
            skipped += 1
            continue
        seen.add(gid)

        has_action = bool(it.get("status") or it.get("defer_days") or it.get("prereq"))
        if not has_action:
            skipped += 1
            continue

        rows = db.query(pid, "SELECT context FROM gap WHERE id=?", (gid,))
        ctx = _context_dict(rows[0].get("context")) if rows else {}
        patch: dict = {"id": gid}
        if it.get("defer_days"):
            ctx["defer_until"] = _defer_date(int(it["defer_days"]))
        if it.get("prereq"):
            ctx["prereq"] = str(it["prereq"]).strip()
        if it.get("status"):
            patch["status"] = it["status"]
        # gap.context 不在 db.JSON_COLUMNS 中，需自行序列化为 TEXT
        patch["context"] = json.dumps(ctx, ensure_ascii=False)
        patch["updated_at"] = _now_utc()
        db.write(pid, "gap", patch, "user")
        updated += 1

    if _is_htmx(req):
        return _batch_fragment(updated, skipped)
    return ok({"updated": updated, "skipped": skipped})
