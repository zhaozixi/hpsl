"""治理度量测试（M9：compute_counts / plane_signals / counts_context）。

全部走临时 HPSL_HOME（conftest 已支持），不污染真实 ~/.hpsl。
内部 API 接受 project_id（create_project 自动 set_active，亦可显式传 pid / 留空回退激活项目）。
"""

from hpsl import db, projects
from hpsl.common.dto import CountsVO, ConfigVO
from hpsl.common.enums import AttemptKind
from hpsl.engine.governance import compute_counts, counts_context, plane_signals


def _new_pid():
    return projects.create_project("治理测试", "g").id


def _unit(pid, uid, *, status="CLOSED", forced_close=0, skipped_produce=0):
    closed_at = "2026-01-01T00:00:00.000Z" if status == "CLOSED" else None
    db.write(
        pid, "unit",
        {
            "id": uid, "title": f"单元-{uid}", "status": status,
            "forced_close": forced_close, "close_reason": None,
            "difficulty_band": None, "degraded_from": None, "degrade_count": 0,
            "production": None, "produced_at": None,
            "skipped_produce": skipped_produce, "chosen_from": None,
            "closed_at": closed_at,
        },
        "system",
    )


def _attempt(pid, aid, uid, kind, self_score, confirmed_score):
    db.write(
        pid, "attempt",
        {
            "id": aid, "unit_id": uid, "concept_id": None, "kind": kind,
            "rubric": None, "self_score": self_score, "confirmed_score": confirmed_score,
            "seconds": 10, "ts": "2026-01-01T00:00:00.000Z",
        },
        "system",
    )


def _session(pid, sid, uid, gaps_confirmed=0):
    db.write(
        pid, "session",
        {
            "id": sid, "unit_id": uid, "started_at": "2026-01-01T00:00:00.000Z",
            "ended_at": None, "gaps_confirmed": gaps_confirmed,
        },
        "system",
    )


def _gap(pid, gid, uid, sid, level, status):
    db.write(
        pid, "gap",
        {
            "id": gid, "unit_id": uid, "session_id": sid, "level": level, "text": "g",
            "status": status, "context": None,
            "created_at": "2026-01-01T00:00:00.000Z", "updated_at": None,
        },
        "system",
    )


def _concept(pid, cid):
    db.write(
        pid, "concept",
        {"id": cid, "name": cid, "verify_status": "未验证", "last_verified_at": None},
        "system",
    )


def _edge(pid, eid, src, dst):
    db.write(pid, "concept_edge", {"id": eid, "src": src, "dst": dst, "kind": "关联"}, "system")


# ---- 空项目：全部指标为 0.0 / 0 ----
def test_empty_project_all_zero():
    pid = _new_pid()
    c = compute_counts(pid)
    assert isinstance(c, CountsVO)
    assert c.pass_rate == 0.0
    assert c.review_pass_rate == 0.0
    assert c.calibration_gap == 0.0
    assert c.l1_gap_sessions == 0
    assert c.zero_gap_ratio == 0.0
    assert c.unit_completion == 0.0
    assert c.gap_clear_ratio == 0.0
    assert c.l2_gap_backlog == 0
    assert c.forced_close_ratio == 0.0
    assert c.skipped_produce_ratio == 0.0
    assert c.unresolved_disagreements == 0
    assert c.isolated_node_ratio == 0.0


# ---- 样本不足：不触发「通过率偏低」等误报 ----
def test_empty_project_no_misleading_prompts():
    pid = _new_pid()
    c = compute_counts(pid)
    prompts = plane_signals(c, ConfigVO())
    assert prompts == []


# ---- pass_rate：剔除强行闭环单元 + 复测加权 ----
def test_pass_rate_excludes_forced_and_weights_retest():
    pid = _new_pid()
    _unit(pid, "u1")
    _unit(pid, "u2", forced_close=1)  # 强行闭环
    _attempt(pid, "a1", "u1", AttemptKind.RESTATE.value, 0.9, 1.0)
    _attempt(pid, "a2", "u1", AttemptKind.BOUNDARY.value, 0.2, 0.0)
    _attempt(pid, "a3", "u1", AttemptKind.RETEST.value, 0.8, 1.0)
    _attempt(pid, "a4", "u2", AttemptKind.RESTATE.value, 0.9, 1.0)  # 剔除
    c = compute_counts(pid)
    # 权重：重述1(过) + 边界1(不过) + 复测2(过) = 4，过 = 1+0+2 = 3 → 0.75
    assert abs(c.pass_rate - 0.75) < 1e-9
    # 复测仅 a3 通过 → 1.0
    assert c.review_pass_rate == 1.0


# ---- calibration_gap：自评 - 确认 平均差（正值=高估） ----
def test_calibration_gap_positive_when_overestimate():
    pid = _new_pid()
    _unit(pid, "u1")
    _attempt(pid, "a1", "u1", AttemptKind.RESTATE.value, 0.9, 0.5)  # +0.4
    _attempt(pid, "a2", "u1", AttemptKind.BOUNDARY.value, 0.8, 0.6)  # +0.2
    c = compute_counts(pid)
    assert abs(c.calibration_gap - 0.3) < 1e-9


# ---- zero_gap_ratio：分母只算 gaps_confirmed=1 会话 ----
def test_zero_gap_ratio_denominator_only_confirmed():
    pid = _new_pid()
    _unit(pid, "u1")
    _unit(pid, "u2")
    _session(pid, "s1", "u1", gaps_confirmed=1)
    _session(pid, "s2", "u2", gaps_confirmed=1)
    _session(pid, "s3", "u1", gaps_confirmed=0)  # 不计入分母
    _gap(pid, "g1", "u1", "s1", 1, "待补")  # u1 有缺口 → s1 非零缺口
    c = compute_counts(pid)
    # 分母 2（s1,s2）；零缺口 = 仅 s2 归属的 u2 → 1/2
    assert abs(c.zero_gap_ratio - 0.5) < 1e-9


# ---- l1_gap_sessions：一级缺口超上限（默认 5）的会话数 ----
def test_l1_gap_sessions_exceeds_limit():
    pid = _new_pid()
    _unit(pid, "u1")
    _session(pid, "s1", "u1")
    for i in range(6):  # 6 个一级缺口挂 s1 > 5
        _gap(pid, f"g{i}", "u1", "s1", 1, "待补")
    c = compute_counts(pid)
    assert c.l1_gap_sessions == 1


# ---- 单元比率：完成 / 强行闭环 / 跳过产出 ----
def test_unit_ratios():
    pid = _new_pid()
    _unit(pid, "u1", forced_close=0)                 # closed_ok
    _unit(pid, "u2", forced_close=1)                 # 强行闭环
    _unit(pid, "u3", status="INIT", skipped_produce=1)  # 跳过产出（未闭环）
    _unit(pid, "u4", status="INIT")                  # 未完成
    c = compute_counts(pid)
    assert abs(c.unit_completion - 0.25) < 1e-9       # 1/4
    assert abs(c.forced_close_ratio - 0.25) < 1e-9    # 1/4
    assert abs(c.skipped_produce_ratio - 0.25) < 1e-9  # 1/4


# ---- 缺口清账率 + 二级缺口积压 ----
def test_gap_clear_and_l2_backlog():
    pid = _new_pid()
    _unit(pid, "u1")
    _session(pid, "s1", "u1")
    _gap(pid, "g1", "u1", "s1", 2, "待补")   # L2 待补 → 积压
    _gap(pid, "g2", "u1", "s1", 2, "已补")   # 已清
    _gap(pid, "g3", "u1", "s1", 1, "已验证") # 已清
    _gap(pid, "g4", "u1", "s1", 1, "待补")   # L1 待补（不计 L2 积压）
    c = compute_counts(pid)
    assert c.l2_gap_backlog == 1
    assert abs(c.gap_clear_ratio - 0.5) < 1e-9  # 已清 2 / 总 4


# ---- isolated_node_ratio：无出入边的概念占比 ----
def test_isolated_node_ratio():
    pid = _new_pid()
    _concept(pid, "c1")
    _concept(pid, "c2")
    _concept(pid, "c3")
    _edge(pid, "e1", "c1", "c2")  # c3 孤岛
    c = compute_counts(pid)
    assert abs(c.isolated_node_ratio - (1 / 3)) < 1e-9


# ---- unresolved_disagreements：排除同源降级告警与已裁决 ----
def test_unresolved_disagreements_excludes_same_source():
    pid = _new_pid()
    _unit(pid, "u1")
    db.write(
        pid, "claim_check",
        {"id": "cc1", "claim": "分歧", "providers": {"same_source": False},
         "unit_id": "u1", "session_id": None, "agree": 0, "diff": "x",
         "user_ruling": None, "residual": 1}, "system",
    )
    db.write(
        pid, "claim_check",
        {"id": "cc2", "claim": "同源", "providers": {"same_source": True},
         "unit_id": "u1", "session_id": None, "agree": None, "diff": "x",
         "user_ruling": None, "residual": 0}, "system",
    )
    db.write(
        pid, "claim_check",
        {"id": "cc3", "claim": "已裁决", "providers": {"same_source": False},
         "unit_id": "u1", "session_id": None, "agree": 0, "diff": "x",
         "user_ruling": "驳回", "residual": 0}, "system",
    )
    c = compute_counts(pid)
    assert c.unresolved_disagreements == 1


# ---- plane_signals：四平面阈值触发 + 风险优先 + 可关闭 ----
def test_plane_signals_priority_and_dismissible():
    pid = _new_pid()
    _unit(pid, "u0")  # 质量：正常单元低分
    _attempt(pid, "a0", "u0", AttemptKind.RESTATE.value, 0.1, 0.0)
    _unit(pid, "u1", forced_close=1)  # 风险：强行闭环占比超线
    _unit(pid, "u2", status="INIT")
    _unit(pid, "u3", status="INIT")
    c = compute_counts(pid)
    ctx = counts_context(pid)
    prompts = plane_signals(c, ConfigVO(), ctx=ctx)
    ids = {p.id for p in prompts}
    assert "quality-pass-low" in ids
    assert "risk-forced-close" in ids
    order = [p.plane for p in prompts]
    assert order.index("风险") < order.index("质量")
    assert all(p.dismissible for p in prompts)


# ---- plane_signals：每会话提示上限保留高优先级 ----
def test_max_prompts_truncation_keeps_priority():
    pid = _new_pid()
    _unit(pid, "u0")
    _attempt(pid, "a0", "u0", AttemptKind.RESTATE.value, 0.1, 0.0)
    _unit(pid, "u1", forced_close=1)
    _unit(pid, "u2", status="INIT")
    _unit(pid, "u3", status="INIT")
    c = compute_counts(pid)
    ctx = counts_context(pid)
    prompts = plane_signals(c, ConfigVO(), ctx=ctx, max_prompts=1)
    assert len(prompts) == 1
    assert prompts[0].plane == "风险"


# ---- plane_signals：零缺口占比超线（节奏） ----
def test_plane_signals_pace_zero_gap():
    pid = _new_pid()
    _unit(pid, "u1")
    _unit(pid, "u2")
    _session(pid, "s1", "u1", gaps_confirmed=1)
    _session(pid, "s2", "u2", gaps_confirmed=1)
    _gap(pid, "g1", "u1", "s1", 1, "待补")  # u1 有缺口；u2 零缺口 → 占比 0.5
    c = compute_counts(pid)
    ctx = counts_context(pid)
    prompts = plane_signals(c, ConfigVO(), ctx=ctx)
    assert "pace-zero-gap" in {p.id for p in prompts}


# ---- plane_signals：二级缺口积压（进度） ----
def test_plane_signals_progress_backlog():
    pid = _new_pid()
    _unit(pid, "u1")
    _session(pid, "s1", "u1")
    _gap(pid, "g1", "u1", "s1", 2, "待补")
    c = compute_counts(pid)
    ctx = counts_context(pid)
    prompts = plane_signals(c, ConfigVO(), ctx=ctx)
    assert "progress-gap-backlog" in {p.id for p in prompts}


# ---- counts_context：样本基数正确 ----
def test_counts_context_base_counts():
    pid = _new_pid()
    _unit(pid, "u1")
    _attempt(pid, "a1", "u1", AttemptKind.RESTATE.value, 0.9, 1.0)
    _session(pid, "s1", "u1", gaps_confirmed=1)
    ctx = counts_context(pid)
    assert ctx["attempts"] == 1
    assert ctx["calibration_samples"] == 1
    assert ctx["confirmed_sessions"] == 1
    assert ctx["total_units"] == 1


# ---- compute_counts 无 pid 时回退激活项目 ----
def test_compute_counts_uses_active_when_pid_none():
    pid = _new_pid()  # 自动 set_active
    _unit(pid, "u1")
    c = compute_counts()
    assert isinstance(c, CountsVO)


# ---- 零 DDL：治理度量不新增/改动库对象（M0 锁定 16） ----
def test_governance_zero_ddl():
    pid = _new_pid()
    _unit(pid, "u1")
    _attempt(pid, "a1", "u1", AttemptKind.RESTATE.value, 0.9, 1.0)
    compute_counts(pid)
    assert db.count_user_objects(pid) == 16
