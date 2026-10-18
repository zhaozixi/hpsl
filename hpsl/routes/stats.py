"""M16 学习分析仪表盘（只读聚合 concept/card/review/llm_call，零 DDL）。

- GET /stats：仪表盘页面（概念/卡片验证分布、待复测/逾期、累计花费、单元数）。
- GET /api/v1/stats：同名数据 REST。

纪律：只读消费既有表，读接口 read_security；零 DDL；无新错误码（仅 A010001 无激活）；
本批次不改引擎（直接 db.query，与 graph 路由同构）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates

from .. import db, projects as proj
from ..common.result import ok
from .security import read_security

page_router = APIRouter()
api_router = APIRouter(prefix="/api/v1")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _active_pid() -> Optional[str]:
    active = proj.active_project()
    return active.id if active is not None else None


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


_ALLOWED_RANGES = (7, 30, 90, 180)


def _parse_range(s: Optional[str]) -> int:
    try:
        v = int(s or "")
    except (TypeError, ValueError):
        return 30
    return v if v in _ALLOWED_RANGES else 30


def compute_stats(pid: str, days: int = 30) -> dict:
    def dist(table: str) -> dict:
        rows = db.query(pid, f"SELECT verify_status, COUNT(*) AS c FROM {table} GROUP BY verify_status")
        return {r["verify_status"]: r["c"] for r in rows}

    concept_dist = dist("concept")
    card_dist = dist("card")

    now = datetime.now(timezone.utc)
    reviews = db.query(pid, "SELECT id, due, result FROM review")
    active_reviews = [r for r in reviews if r["result"] is None]
    overdue = [r for r in active_reviews if (lambda d: d is not None and d < now)(_parse_dt(r["due"]))]

    cost_rows = db.query(pid, "SELECT COALESCE(SUM(cost),0) AS total FROM llm_call")
    total_cost = float(cost_rows[0]["total"]) if cost_rows else 0.0

    units = db.query(pid, "SELECT COUNT(*) AS c FROM unit")
    unit_count = int(units[0]["c"]) if units else 0

    # 时间序列：近 days 天每日花费（llm_call.ts 取日期前缀聚合，窗口外忽略）
    day_labels = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    buckets = {d: 0.0 for d in day_labels}
    for r in db.query(pid, "SELECT ts, cost FROM llm_call"):
        ts = r.get("ts") or ""
        d = ts[:10]
        if d in buckets:
            try:
                buckets[d] += float(r.get("cost") or 0)
            except (TypeError, ValueError):
                pass
    cost_series = [{"date": d, "cost": round(buckets[d], 4)} for d in day_labels]
    cost_max = max((s["cost"] for s in cost_series), default=0.0)

    # 概念验证时间线：近 days 天每日已验证概念数（concept.last_verified_at 取日期前缀，窗口外忽略）
    verify_buckets = {d: 0 for d in day_labels}
    for r in db.query(pid, "SELECT last_verified_at FROM concept WHERE last_verified_at IS NOT NULL"):
        lv = r.get("last_verified_at") or ""
        d = lv[:10]
        if d in verify_buckets:
            verify_buckets[d] += 1
    verify_timeline = [{"date": d, "count": verify_buckets[d]} for d in day_labels]
    verify_total = sum(verify_buckets.values())
    verify_max = max((s["count"] for s in verify_timeline), default=0)

    # 单元维度细分
    status_rows = db.query(pid, "SELECT status, COUNT(*) AS c FROM unit GROUP BY status")
    status_dist = {r["status"]: r["c"] for r in status_rows}
    diff_rows = db.query(pid, "SELECT difficulty_band, COUNT(*) AS c FROM unit GROUP BY difficulty_band")
    diff_dist = {(r["difficulty_band"] or "未分级"): r["c"] for r in diff_rows}
    prod_rows = db.query(pid, "SELECT COUNT(*) AS c FROM unit WHERE production IS NOT NULL AND production <> ''")
    produced = int(prod_rows[0]["c"]) if prod_rows else 0
    deg_rows = db.query(pid, "SELECT COUNT(*) AS c FROM unit WHERE degrade_count > 0")
    degraded = int(deg_rows[0]["c"]) if deg_rows else 0
    unit_breakdown = {
        "total": unit_count,
        "status": status_dist,
        "difficulty": diff_dist,
        "produced": produced,
        "degraded": degraded,
    }

    return {
        "concept": {"total": sum(concept_dist.values()), "dist": concept_dist},
        "card": {"total": sum(card_dist.values()), "dist": card_dist},
        "review": {"active": len(active_reviews), "overdue": len(overdue)},
        "cost_total": round(total_cost, 4),
        "unit_count": unit_count,
        "cost_series": cost_series,
        "cost_max": round(cost_max, 4),
        "verify_timeline": verify_timeline,
        "verify_total": verify_total,
        "verify_max": verify_max,
        "unit_breakdown": unit_breakdown,
    }


@page_router.get("/stats", dependencies=[Depends(read_security)])
def stats_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req, name="stats.html", context={"error": "尚未选择项目，请先在首页选择或新建项目"}
        )
    pid = active.id
    days = _parse_range(req.query_params.get("days"))
    stats = compute_stats(pid, days)
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="stats.html",
        context={"project": active.to_dict(), "stats": stats, "days": days},
    )


@api_router.get("/stats", dependencies=[Depends(read_security)])
def stats_api(req: Request):
    pid = _active_pid()
    if pid is None:
        return ok(None)
    return ok(compute_stats(pid, _parse_range(req.query_params.get("days"))))
