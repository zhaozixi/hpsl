"""M27：账本/审计/成本 REST 与撤销入口（消费 M7 four-ledger + M8 cost）。

- `GET  /api/v1/ledger/summary`：四本账摘要
- `GET  /api/v1/ledger/gaps`：缺口账本（可按 unit_id / level / status 过滤）
- `GET  /api/v1/ledger/audit`：最近审计条目（默认 50 条）
- `POST /api/v1/audit/{audit_id}/undo`：按审计回滚（write_security；可选 expected_prev_hash）
- `GET  /api/v1/sessions/{session_id}/cost`：会话花费估算
- `GET  /api/v1/cost/monthly`：当月累计与预算三态

纪律：读接口 read_security；撤销为写接口（Origin + CSRF + 限流）；零 DDL；不新增错误码。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse

from .. import db, projects as proj
from ..common.errors import BizError
from ..common.result import ok
from ..engine import cost as cost_engine
from ..engine import ledger as ledger_engine
from .security import read_security, write_security

api_router = APIRouter(prefix="/api/v1")


def _require_pid() -> str:
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目")
    return active.id


async def _parse_body(req: Request) -> dict:
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


def _is_htmx(req: Request) -> bool:
    return req.headers.get("hx-request") is not None


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------
@api_router.get("/ledger/summary", dependencies=[Depends(read_security)])
def ledger_summary_api():
    pid = _require_pid()
    return ok(ledger_engine.ledger_summary(pid))


@api_router.get("/ledger/gaps", dependencies=[Depends(read_security)])
def ledger_gaps_api(
    unit_id: str | None = None,
    level: int | None = None,
    status: str | None = None,
):
    pid = _require_pid()
    if level is not None and level not in (1, 2, 3):
        raise BizError("C400001", msg="level 仅支持 1/2/3")
    return ok(
        [g.to_dict() for g in ledger_engine.list_gaps(pid, unit_id=unit_id, level=level, status=status)]
    )


@api_router.get("/ledger/audit", dependencies=[Depends(read_security)])
def ledger_audit_api(limit: int = 50):
    pid = _require_pid()
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    rows = db.query(
        pid,
        "SELECT id, ts, actor, table_name, row_id FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return ok(rows)


@api_router.post("/audit/{audit_id}/undo", dependencies=[Depends(write_security)])
async def audit_undo_api(req: Request, audit_id: int):
    """撤销一条审计对应的业务写入（撤销动作本身也留痕）。"""
    pid = _require_pid()
    data = await _parse_body(req)
    expected = data.get("expected_prev_hash")
    if expected is not None and not isinstance(expected, str):
        raise BizError("C400001", msg="expected_prev_hash 须为字符串或省略")
    db.undo(pid, audit_id, actor="user", expected_prev_hash=expected)
    if _is_htmx(req):
        return HTMLResponse(
            f"<p class='ok'>已撤销 #{audit_id}</p>", headers={"HX-Refresh": "true"}
        )
    return ok({"audit_id": audit_id, "undone": True})


# ---------------------------------------------------------------------------
# 成本
# ---------------------------------------------------------------------------
@api_router.get("/sessions/{session_id}/cost", dependencies=[Depends(read_security)])
def session_cost_api(session_id: str):
    return ok(cost_engine.session_cost(session_id).to_dict())


@api_router.get("/cost/monthly", dependencies=[Depends(read_security)])
def monthly_cost_api():
    pid = _require_pid()
    cap = proj.get_effective_config(pid).budget_cap_usd
    month = db._now_utc()[:7]
    rows = db.query(
        pid,
        "SELECT COALESCE(SUM(cost),0.0) AS c FROM llm_call WHERE substr(ts,1,7)=?",
        (month,),
    )
    cumulative = float(rows[0]["c"]) if rows else 0.0
    return ok(
        {
            "month": month,
            "cumulative_usd": cumulative,
            "budget_cap_usd": cap,
            "status": cost_engine._status(cumulative, cap, 0),
            # 未登记价格的模型：成本按 0 计，这里显式暴露，避免"预算永远不超"的假象
            "unpriced_models": cost_engine.unpriced_models(pid),
        }
    )
