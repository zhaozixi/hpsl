"""成本估算与预算编排层（规格 §3.2.M8，M8 新增；BC-7 / D-05）。

职责（在 M0 db.write / audit 基座之上增量）：
- record_call：每次 Provider 调用出口记一行 llm_call（同事务审计，actor=system）。
- session_cost：按会话聚合（SUM tokens × pricing）+ 当月累计（跨会话）+ 预算三态。
- budget_status：ok / warn / blocked 三态（读时计算，无持久状态）。
- confirm_continue：超限确认后放行并写审计留痕（零 DDL，confirmations 由 audit_log 派生）。

pid 解析：内部 API 按锁定基线 §3.2.M8.2 签名**（不含 pid）**，依赖 M4 调用出口 / M10 工作台
始终在「激活项目」上下文中运行——故经 projects.active_project() 解析 pid。测试须先 create_project
（其自动 set_active）或显式 set_active。

依赖方向：engine.cost -> common, db, projects, models.registry（防腐层）；禁止反向；保持 headless。
纪律：业务写只走 db.write；confirm_continue 经 db.append_audit_event 直写 audit_log
（§3.2.M8.4 序列图显式要求：C->>DB: 写 audit_log），属 M0 写入入口的受控扩展，零 DDL。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Optional

from .. import db, projects
from ..common.dto import CostEstimateVO
from ..common.errors import BizError
from ..models import registry as _registry


# ---------------------------------------------------------------------------
# 解析与取数助手
# ---------------------------------------------------------------------------
def _active_pid() -> str:
    """解析当前激活项目 pid；未激活 → A080002。"""
    active = projects.active_project()
    if active is None:
        raise BizError("A080002")
    return active.id


def _month_prefix_utc() -> str:
    """当前 UTC 自然月的 YYYY-MM 前缀（用于跨会话当月累计聚合，ts 为字典序 ISO）。"""
    return db._now_utc()[:7]


def _pricing_for(model_id: str) -> Optional[dict]:
    """取模型价格表（in_per_1k / out_per_1k，美元 / 1K tokens）；未登记返回 None。"""
    return _registry.pricing(model_id)


def _extract_usage(reply) -> tuple[int, int]:
    """从 Reply.raw 归一化 tokens_in / tokens_out（兼容 OpenAI / Anthropic usage 形态）。"""
    raw = getattr(reply, "raw", None)
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    tin = usage.get("prompt_tokens")
    if tin is None:
        tin = usage.get("input_tokens")
    tout = usage.get("completion_tokens")
    if tout is None:
        tout = usage.get("output_tokens")
    return int(tin or 0), int(tout or 0)


def _provider_of(model_id: str) -> str:
    """由模型 ID 推导 provider（reply.raw 不含 provider 字段）；未登记给 'unknown' 兜底。"""
    try:
        return _registry.get_model(model_id).provider
    except KeyError:
        return "unknown"


def unpriced_models(pid: str) -> list[str]:
    """返回本项目调用过、但 `pricing.yaml` 未登记价格的模型（成本计 0，需用户补价）。"""
    rows = db.query(pid, "SELECT DISTINCT model FROM llm_call")
    return sorted({r["model"] for r in rows if _pricing_for(r["model"]) is None})


def _input_hash(role: str, model_id: str, text: str) -> str:
    """调用输入指纹（幂等索引组成部分）。

    record_call 未持请求体，故取角色 + 模型 + 回复稳定签名。uk_llmcall_idem 还含微秒级 ts，
    实际调用时间戳不同即不会碰撞，此哈希仅作稳定签名。
    """
    sig = f"{role}|{model_id}|{len(text)}|{text[:64]}"
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()


def _call_cost(model_id: str, tokens_in: int, tokens_out: int) -> float:
    """按 pricing.yaml 计算单次调用花费（美元）。

    公式（基线 §3.2.M8.5）：cost = tokens_in / 1e6 × price_in + tokens_out / 1e6 × price_out。
    pricing.yaml 给的是「每 1K tokens」（in_per_1k / out_per_1k），故 price_in_per_1e6 = in_per_1k × 1000，
    得：cost = (tokens_in × in_per_1k + tokens_out × out_per_1k) / 1000.0。
    模型未登记价格 → 0.0（用户可维护 pricing.yaml；缺价时不计入预算，避免误拦截）。
    """
    p = _pricing_for(model_id)
    if not p:
        return 0.0
    in_per_1k = float(p.get("in_per_1k", 0.0))
    out_per_1k = float(p.get("out_per_1k", 0.0))
    return (tokens_in * in_per_1k + tokens_out * out_per_1k) / 1000.0


def _confirmations(pid: str, session_id: str) -> int:
    """本会话超限确认次数（由 audit_log 中 table_name='cost_confirm' 且 row_id=session_id 派生）。"""
    rows = db.query(
        pid,
        "SELECT count(*) c FROM audit_log WHERE table_name=? AND row_id=?",
        ("cost_confirm", session_id),
    )
    return int(rows[0]["c"]) if rows else 0


def _status(cumulative_usd: float, cap_usd: float, confirmations: int) -> str:
    """预算三态（读时计算）。

    ok：累计 < 80% × cap；warn：80% ≤ 累计 < 100% × cap；blocked：累计 ≥ 100% × cap。
    状态机（§3.2.M8.5）：blocked 经用户确认后本会话放行 → warn（仅当 confirmations ≥ 1 时，
    原始 blocked 提升为 warn，表示本会话已放行；warn/ok 维持原态）。
    """
    if cumulative_usd >= cap_usd:
        raw = "blocked"
    elif cumulative_usd >= 0.8 * cap_usd:
        raw = "warn"
    else:
        raw = "ok"
    if raw == "blocked" and confirmations >= 1:
        return "warn"
    return raw


# ---------------------------------------------------------------------------
# 内部 API（§3.2.M8.2，签名锁定不含 pid）
# ---------------------------------------------------------------------------
def record_call(role: str, reply, session_id: str, unit_id: str) -> CostEstimateVO:
    """每次 Provider 调用出口记一行 llm_call（同事务审计，actor=system）。

    前置：session_id（及 unit_id，若非 None）对应的业务行须已存在（FK 约束，由调用方 M4 保障）。
    - provider：由 reply.model 经 registry 推导（reply.raw 不含 provider）。
    - tokens_in / tokens_out：由 reply.raw['usage'] 兼容归一化（OpenAI/Anthropic）。
    - cost：按 pricing.yaml 计算（未登记模型记 0.0）。

    返回：记账后的会话花费估算（CostEstimateVO），便于 M4 即时刷新状态。
    """
    pid = _active_pid()
    model_id = reply.model
    provider = _provider_of(model_id)
    tokens_in, tokens_out = _extract_usage(reply)
    cost = _call_cost(model_id, tokens_in, tokens_out)
    now = db._now_utc()
    db.write(
        pid,
        "llm_call",
        {
            "id": uuid.uuid4().hex,
            "role": role,
            "provider": provider,
            "model": model_id,
            "session_id": session_id,
            "unit_id": unit_id,
            "input_hash": _input_hash(role, model_id, reply.text),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost": cost,
            "ts": now,
        },
        "system",
    )
    return session_cost(session_id)


def session_cost(session_id: str) -> CostEstimateVO:
    """按会话聚合花费 + 当月累计 + 预算三态（§3.2.M8.3 CostEstimateVO）。"""
    pid = _active_pid()
    cap = projects.get_effective_config(pid).budget_cap_usd

    sess_rows = db.query(
        pid,
        "SELECT COALESCE(SUM(tokens_in),0) AS t_in, COALESCE(SUM(tokens_out),0) AS t_out, "
        "COALESCE(SUM(cost),0.0) AS c FROM llm_call WHERE session_id=?",
        (session_id,),
    )
    ti = int(sess_rows[0]["t_in"])
    to = int(sess_rows[0]["t_out"])
    sess_cost = float(sess_rows[0]["c"])

    month = _month_prefix_utc()
    cum_rows = db.query(
        pid,
        "SELECT COALESCE(SUM(cost),0.0) c FROM llm_call WHERE substr(ts,1,7)=?",
        (month,),
    )
    cumulative = float(cum_rows[0]["c"]) if cum_rows else 0.0

    confirmations = _confirmations(pid, session_id)
    status = _status(cumulative, cap, confirmations)

    return CostEstimateVO(
        session_id=session_id,
        tokens_in=ti,
        tokens_out=to,
        cost_usd=sess_cost,
        cumulative_usd=cumulative,
        budget_cap_usd=cap,
        status=status,
        confirmations=confirmations,
        computed_at=db._now_utc(),
    )


def budget_status(session_id: str) -> str:
    """预算三态（ok / warn / blocked），读时计算；M4 闸门消费。blocked 时由 M4 升 A080001。"""
    pid = _active_pid()
    cap = projects.get_effective_config(pid).budget_cap_usd
    month = _month_prefix_utc()
    cum_rows = db.query(
        pid,
        "SELECT COALESCE(SUM(cost),0.0) c FROM llm_call WHERE substr(ts,1,7)=?",
        (month,),
    )
    cumulative = float(cum_rows[0]["c"]) if cum_rows else 0.0
    confirmations = _confirmations(pid, session_id)
    return _status(cumulative, cap, confirmations)


def confirm_continue(session_id: str, actor: str = "user") -> CostEstimateVO:
    """超限确认后本会话放行并写审计留痕（§3.2.M8.4 序列图）。

    - 写 audit_log（table_name='cost_confirm', row_id=session_id, actor=actor）经 db.append_audit_event。
    - confirmations 由 audit_log 派生，本会话放行（blocked→warn，状态机 §3.2.M8.5）。
    - 返回更新后的会话花费估算（CostEstimateVO）。
    """
    pid = _active_pid()
    if actor not in ("user", "clerk", "system"):
        raise ValueError(f"非法 actor：{actor!r}")
    now = db._now_utc()
    db.append_audit_event(
        pid,
        actor,
        "cost_confirm",
        session_id,
        {"kind": "cost_confirm", "session_id": session_id, "ts": now, "actor": actor},
    )
    return session_cost(session_id)
