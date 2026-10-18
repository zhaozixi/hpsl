"""成本估算与预算测试（M8：record_call / session_cost / budget_status / confirm_continue）。

全部走临时 HPSL_HOME（conftest 已支持），不污染真实 ~/.hpsl。

M8 内部 API 签名锁定不含 pid（§3.2.M8.2），依赖激活项目上下文——测试经 create_project
（自动 set_active）或显式 set_active 提供激活项目。
"""

import sqlite3

import pytest

from hpsl import db, projects
from hpsl.common.dto import CostEstimateVO
from hpsl.common.errors import BizError
from hpsl.engine.cost import (
    record_call,
    session_cost,
    budget_status,
    confirm_continue,
)
from hpsl.models.base import Reply


def _new_pid():
    return projects.create_project("成本测试", "g").id


def _seed(pid, sid, uid):
    """建 session + unit（llm_call FK 前置；record_call 不自动建）。"""
    db.write(pid, "unit", {"id": uid, "title": f"单元-{uid}", "status": "INIT"}, "system")
    db.write(pid, "session", {"id": sid, "unit_id": uid, "started_at": db._now_utc()}, "system")


def _reply(model="gpt-4o-mini", prompt=1_000_000, completion=1_000_000, shape="openai"):
    """构造 Reply（raw.usage 兼容 OpenAI / Anthropic 两种形态）。

    gpt-4o-mini：in_per_1k=0.00015, out_per_1k=0.0006 → cost=(prompt*0.00015+completion*0.0006)/1000。
    1M/1M → 0.75 USD。
    """
    if shape == "anthropic":
        usage = {"input_tokens": prompt, "output_tokens": completion}
    else:
        usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    return Reply(text="答案", finish_reason="stop", model=model, raw={"usage": usage})


# ---- record_call：记账 + 返回 VO ----
def test_record_call_writes_llm_call_and_returns_vo():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    vo = record_call("teacher", _reply(), "s1", "u1")
    assert isinstance(vo, CostEstimateVO)
    rows = db.query(pid, "SELECT * FROM llm_call")
    assert len(rows) == 1
    r = rows[0]
    assert r["role"] == "teacher"
    assert r["provider"] == "openai"   # 由 model 推导
    assert r["model"] == "gpt-4o-mini"
    assert r["tokens_in"] == 1_000_000
    assert r["tokens_out"] == 1_000_000
    assert abs(r["cost"] - 0.75) < 1e-9
    assert vo.cost_usd == 0.75
    assert vo.status == "ok"


# ---- record_call：Anthropic usage 形态归一化 ----
def test_record_call_anthropic_usage_shape():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    vo = record_call("critic", _reply(model="claude-3-5-haiku-20241022", shape="anthropic"), "s1", "u1")
    rows = db.query(pid, "SELECT * FROM llm_call")
    assert rows[0]["tokens_in"] == 1_000_000
    assert rows[0]["tokens_out"] == 1_000_000
    # claude haiku: in_per_1k=0.00025, out_per_1k=0.00125 → (1e6*0.00025+1e6*0.00125)/1000 = 1.5
    assert abs(rows[0]["cost"] - 1.5) < 1e-9
    assert vo.cost_usd == 1.5


# ---- record_call：未知模型 → cost 0.0 + provider 'unknown' ----
def test_record_call_unknown_model_cost_zero():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    vo = record_call("teacher", _reply(model="no-such-model"), "s1", "u1")
    r = db.query(pid, "SELECT * FROM llm_call")[0]
    assert r["provider"] == "unknown"
    assert r["cost"] == 0.0
    assert vo.cost_usd == 0.0


# ---- session_cost：聚合 + 预算三态 ok ----
def test_session_cost_aggregation_ok():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    record_call("teacher", _reply(), "s1", "u1")
    record_call("critic", _reply(prompt=2_000_000, completion=1_000_000), "s1", "u1")
    vo = session_cost("s1")
    assert vo.tokens_in == 3_000_000
    assert vo.tokens_out == 2_000_000
    # 0.75 + (2e6*0.00015+1e6*0.0006)/1000 = 0.75 + 0.9 = 1.65
    assert abs(vo.cost_usd - 1.65) < 1e-9
    assert vo.cumulative_usd == vo.cost_usd      # 当月仅此会话
    assert vo.budget_cap_usd == 10.0
    assert vo.confirmations == 0
    assert vo.status == "ok"


# ---- budget_status：warn 区间（80% ≤ 累计 < 100%） ----
def test_budget_status_warn():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    projects.set_config(pid, {"budget_cap_usd": 1.0}, "user")
    # 单调用 cost=0.9 → 0.8 ≤ 0.9 < 1.0 → warn
    record_call("teacher", _reply(prompt=6_000_000, completion=0), "s1", "u1")
    assert budget_status("s1") == "warn"
    assert session_cost("s1").status == "warn"


# ---- budget_status：blocked（累计 ≥ 100%） ----
def test_budget_status_blocked():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    projects.set_config(pid, {"budget_cap_usd": 0.5}, "user")
    # 单调用 cost=0.9 ≥ 0.5 → blocked
    record_call("teacher", _reply(prompt=6_000_000, completion=0), "s1", "u1")
    assert budget_status("s1") == "blocked"
    assert session_cost("s1").status == "blocked"


# ---- confirm_continue：blocked → 放行 → warn（本会话） + 审计留痕 ----
def test_confirm_continue_releases_blocked_session():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    projects.set_config(pid, {"budget_cap_usd": 0.5}, "user")
    record_call("teacher", _reply(prompt=6_000_000, completion=0), "s1", "u1")
    assert budget_status("s1") == "blocked"
    vo = confirm_continue("s1", actor="user")
    # 放行：blocked → warn
    assert vo.status == "warn"
    assert vo.confirmations == 1
    assert budget_status("s1") == "warn"
    # 审计留痕：audit_log 中一条 cost_confirm
    audits = db.query(
        pid, "SELECT * FROM audit_log WHERE table_name=? AND row_id=?", ("cost_confirm", "s1")
    )
    assert len(audits) == 1
    assert audits[0]["actor"] == "user"


# ---- confirmations 幂等：多次确认累加 ----
def test_confirm_continue_idempotent_accumulates():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    projects.set_config(pid, {"budget_cap_usd": 0.5}, "user")
    record_call("teacher", _reply(prompt=6_000_000, completion=0), "s1", "u1")
    confirm_continue("s1", actor="user")
    confirm_continue("s1", actor="user")
    vo = session_cost("s1")
    assert vo.confirmations == 2
    # 仍放行（blocked→warn），不回退为 blocked
    assert vo.status == "warn"


# ---- 当月累计跨会话聚合 ----
def test_monthly_cumulative_across_sessions():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    _seed(pid, "s2", "u1")
    projects.set_config(pid, {"budget_cap_usd": 100.0}, "user")
    record_call("teacher", _reply(), "s1", "u1")   # 0.75
    record_call("critic", _reply(), "s2", "u1")    # 0.75
    # 每会话自身花费 0.75，但当月累计 = 1.5
    assert session_cost("s1").cost_usd == 0.75
    assert session_cost("s2").cost_usd == 0.75
    assert session_cost("s1").cumulative_usd == 1.5
    assert session_cost("s2").cumulative_usd == 1.5
    assert budget_status("s1") == "ok"


# ---- 未激活项目 → A080002 ----
def test_no_active_project_raises():
    saved = projects._active_pid
    projects._active_pid = None
    try:
        with pytest.raises(BizError) as exc:
            budget_status("s1")
        assert exc.value.code == "A080002"
        with pytest.raises(BizError) as exc2:
            session_cost("s1")
        assert exc2.value.code == "A080002"
    finally:
        projects._active_pid = saved


# ---- confirm_continue 非法 actor → ValueError（与 db.write 一致） ----
def test_confirm_continue_invalid_actor():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    with pytest.raises(ValueError):
        confirm_continue("s1", actor="ghost")


# ---- 跨月累计：上月调用不计入当月累计（substr(ts,1,7) 自然月聚合） ----
def test_monthly_cumulative_excludes_other_months():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    _seed(pid, "s_old", "u1")
    projects.set_config(pid, {"budget_cap_usd": 100.0}, "user")
    record_call("teacher", _reply(), "s1", "u1")   # 当月，cost 0.75
    # 直接插一条上月 ts 的 llm_call（不同会话，避免污染 s1 会话聚合）
    db.write(
        pid, "llm_call",
        {
            "id": "old1", "role": "teacher", "provider": "openai", "model": "gpt-4o-mini",
            "session_id": "s_old", "unit_id": "u1", "input_hash": "x",
            "tokens_in": 1_000_000, "tokens_out": 1_000_000, "cost": 0.75,
            "ts": "2020-01-15T08:30:00.000Z",
        },
        "system",
    )
    vo = session_cost("s1")
    assert vo.cost_usd == 0.75            # 仅当月会话聚合
    assert vo.cumulative_usd == 0.75      # 当月跨会话累计不含上月
    assert db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"] == 2


# ---- 三态边界：cumulative == 0.8*cap → warn（>= 0.8*cap 触发） ----
def test_budget_status_boundary_warn_at_80pct():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    projects.set_config(pid, {"budget_cap_usd": 0.9375}, "user")  # 0.8*cap = 0.75
    record_call("teacher", _reply(), "s1", "u1")   # cost 0.75 == 0.8*cap
    assert budget_status("s1") == "warn"


# ---- 三态边界：cumulative == 1.0*cap → blocked（>= cap 触发） ----
def test_budget_status_boundary_blocked_at_100pct():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    projects.set_config(pid, {"budget_cap_usd": 0.75}, "user")  # 1.0*cap = 0.75
    record_call("teacher", _reply(), "s1", "u1")   # cost 0.75 == 1.0*cap
    assert budget_status("s1") == "blocked"


# ---- reply.raw=None → tokens 0/0, cost 0.0，不崩 ----
def test_record_call_no_raw_usage_safe():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    reply = Reply(text="x", finish_reason="stop", model="gpt-4o-mini", raw=None)
    vo = record_call("teacher", reply, "s1", "u1")
    r = db.query(pid, "SELECT * FROM llm_call")[0]
    assert r["tokens_in"] == 0 and r["tokens_out"] == 0
    assert r["cost"] == 0.0
    assert vo.cost_usd == 0.0
    assert vo.status == "ok"


# ---- confirm_continue 合法非 user actor（clerk）放行 ----
def test_confirm_continue_clerk_actor():
    pid = _new_pid()
    _seed(pid, "s1", "u1")
    projects.set_config(pid, {"budget_cap_usd": 0.5}, "user")
    record_call("teacher", _reply(prompt=6_000_000, completion=0), "s1", "u1")
    vo = confirm_continue("s1", actor="clerk")
    assert vo.status == "warn"
    assert vo.confirmations == 1
    audits = db.query(
        pid, "SELECT actor FROM audit_log WHERE table_name='cost_confirm' AND row_id='s1'"
    )
    assert audits[0]["actor"] == "clerk"


# ---- record_call 引用不存在的 session → FK 强制 IntegrityError ----
def test_record_call_missing_session_fk_error():
    pid = _new_pid()
    db.write(pid, "unit", {"id": "u1", "title": "u", "status": "INIT"}, "system")
    with pytest.raises(sqlite3.IntegrityError):
        record_call("teacher", _reply(), "no-such-session", "u1")

