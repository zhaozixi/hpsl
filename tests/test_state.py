"""单元状态机引擎测试（AC-M2-1 / 2 / 3 / 7 / 6 + 冲突无写账）。

用 projects.create_project 建项目拿 pid，再 db.write 插 unit 作起点。
全部走临时 HPSL_HOME（conftest 已支持），不污染真实 ~/.hpsl。
"""

import json

import pytest

from hpsl import db, projects
from hpsl.engine.state import UnitState, move
from hpsl.common.errors import BizError


def _new_pid():
    return projects.create_project("状态机测试", "g").id


def _insert_unit(pid, uid, status, **extra):
    row = {"id": uid, "title": f"unit-{uid}", "status": status}
    row.update(extra)
    db.write(pid, "unit", row, "user")
    return uid


def _unit_row(pid, uid):
    return db.query(pid, "SELECT * FROM unit WHERE id=?", (uid,))[0]


def _unit_audit_count(pid, uid):
    return db.query(
        pid, "SELECT count(*) c FROM audit_log WHERE table_name='unit' AND row_id=?", (uid,)
    )[0]["c"]


# AC-M2-1：非法转移被拒绝（A020001），unit.status 与审计均无该转移记录
def test_illegal_transition_rejected_a020001():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "INIT")
    before = _unit_audit_count(pid, uid)
    with pytest.raises(BizError) as exc:
        move(pid, uid, UnitState.INIT, UnitState.VERIFY)  # INIT 只能 -> PICK
    assert exc.value.code == "A020001"
    # status 不变
    assert _unit_row(pid, uid)["status"] == "INIT"
    # 无该转移的审计记录（本次 move 未产生新的 unit 审计条目）
    assert _unit_audit_count(pid, uid) == before


# AC-M2-2：未产出进入验证被拒绝（status 不变、无审计）
# 注：详细接口契约（规格 §4.1 / 任务书 §c）明确“无 reason → A020003”；
# AC 表(§5)对 AC-M2-2 标注 A020002，二者冲突，本实现以详细契约为准用 A020003（见交付说明）。
def test_no_production_no_reason_rejected():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "PRODUCE", production="")
    before = _unit_audit_count(pid, uid)
    with pytest.raises(BizError) as exc:
        move(pid, uid, UnitState.PRODUCE, UnitState.VERIFY)
    assert exc.value.code == "A020003"
    assert _unit_row(pid, uid)["status"] == "PRODUCE"
    assert _unit_audit_count(pid, uid) == before


# AC-M2-3：填理由跳过产出后放行，带 skipped_produce=1 标记（不写 produced_at）
def test_skip_produce_with_reason_sets_flag():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "PRODUCE", production="")
    vo = move(pid, uid, UnitState.PRODUCE, UnitState.VERIFY, reason="质量不达标，跳过产出")
    assert vo.status == "VERIFY"
    row = _unit_row(pid, uid)
    assert row["status"] == "VERIFY"
    assert row["skipped_produce"] == 1
    assert row["produced_at"] is None  # 跳过路径不写 produced_at


# O-M2-1：LEARN→VERIFY 直接跳过路径（基线 §3.2.M2.5 Step2 / 规格 §4.1 异常路径）须合法可达，
# 且仍落 PRODUCE 闸门：无 reason → A020003，status 不变、无审计
def test_learn_to_verify_no_reason_rejected():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "LEARN", production="")
    before = _unit_audit_count(pid, uid)
    with pytest.raises(BizError) as exc:
        move(pid, uid, "LEARN", "VERIFY")  # 字符串入参亦可
    assert exc.value.code == "A020003"
    assert _unit_row(pid, uid)["status"] == "LEARN"
    assert _unit_audit_count(pid, uid) == before


# O-M2-1：LEARN→VERIFY 带 reason 跳过 → 放行，置 skipped_produce=1，不写 produced_at
def test_learn_to_verify_with_reason_skips():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "LEARN", production="")
    vo = move(pid, uid, "LEARN", "VERIFY", reason="临时跳过")
    assert vo.status == "VERIFY"
    row = _unit_row(pid, uid)
    assert row["status"] == "VERIFY"
    assert row["skipped_produce"] == 1
    assert row["produced_at"] is None


# AC-M2-7：熔断降级 LEARN→PICK 记 degraded_from + degrade_count+=1
def test_degrade_learn_to_pick_records():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "LEARN", difficulty_band="hard", degrade_count=0)
    vo = move(pid, uid, UnitState.LEARN, UnitState.PICK)
    assert vo.status == "PICK"
    row = _unit_row(pid, uid)
    assert row["degraded_from"] == "hard"
    assert row["degrade_count"] == 1


# AC-M2-7 叠加：连续降级计数累加
def test_degrade_count_accumulates():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "LEARN", difficulty_band="medium", degrade_count=2)
    move(pid, uid, UnitState.LEARN, UnitState.PICK)
    row = _unit_row(pid, uid)
    assert row["degrade_count"] == 3
    assert row["degraded_from"] == "medium"


# AC-M2-6：合法转移经 db.write 留审计
def test_legal_transition_writes_audit():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "INIT")
    before = _unit_audit_count(pid, uid)
    move(pid, uid, UnitState.INIT, UnitState.PICK)
    after = _unit_audit_count(pid, uid)
    assert after == before + 1
    a = db.query(
        pid,
        "SELECT change FROM audit_log WHERE table_name='unit' AND row_id=? ORDER BY id DESC LIMIT 1",
        (uid,),
    )[0]
    assert json.loads(a["change"])["status"] == "PICK"


# 正常 PRODUCE→VERIFY 写 produced_at，且返回 allowed_next
def test_produce_to_verify_sets_produced_at():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "PRODUCE", production="这是我的产出")
    vo = move(pid, uid, UnitState.PRODUCE, UnitState.VERIFY)
    assert vo.status == "VERIFY"
    assert vo.allowed_next == ["JUDGE", "LEARN"]
    row = _unit_row(pid, uid)
    assert row["status"] == "VERIFY"
    assert row["produced_at"] is not None
    assert row["skipped_produce"] == 0


# 冲突：cur 与库内 status 不符 → A020001，无写账、无审计
def test_conflict_cur_mismatch_no_write():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "INIT")
    before = _unit_audit_count(pid, uid)
    with pytest.raises(BizError) as exc:
        move(pid, uid, UnitState.PRODUCE, UnitState.VERIFY)  # cur 错配
    assert exc.value.code == "A020001"
    assert _unit_row(pid, uid)["status"] == "INIT"
    assert _unit_audit_count(pid, uid) == before


# 乐观锁：expected_version 与库内 status 不符 → A020001，无写账
def test_expected_version_conflict_no_write():
    pid = _new_pid()
    uid = _insert_unit(pid, "u1", "INIT")
    before = _unit_audit_count(pid, uid)
    with pytest.raises(BizError) as exc:
        move(pid, uid, UnitState.INIT, UnitState.PICK, expected_version="PRODUCE")
    assert exc.value.code == "A020001"
    assert _unit_row(pid, uid)["status"] == "INIT"
    assert _unit_audit_count(pid, uid) == before
