"""四本账与审计测试（M7：confirm_gap_candidates / list_gaps / ledger_summary + undo 集成）。

全部走临时 HPSL_HOME（conftest 已支持），不污染真实 ~/.hpsl。
"""

import pytest

from hpsl import db, projects
from hpsl.common.errors import BizError
from hpsl.engine.ledger import confirm_gap_candidates, list_gaps, ledger_summary


def _new_pid():
    return projects.create_project("账本测试", "g").id


def _make_unit(pid, uid, status="INIT"):
    db.write(pid, "unit", {"id": uid, "title": f"单元-{uid}", "status": status}, "system")


def _make_candidate(pid, cid, uid, text, level=1, status="待确认"):
    db.write(
        pid,
        "gap_candidate",
        {"id": cid, "unit_id": uid, "text": text, "source_turn": None, "status": status, "suggested_level": level},
        "clerk",
    )


def _gap_count(pid):
    return db.query(pid, "SELECT count(*) c FROM gap")[0]["c"]


def _cand_status(pid, cid):
    return db.query(pid, "SELECT status FROM gap_candidate WHERE id=?", (cid,))[0]["status"]


# confirm 入账：候选 → gap，候选置已确认
def test_confirm_creates_gap_and_marks_candidate():
    pid = _new_pid()
    _make_unit(pid, "u1")
    _make_candidate(pid, "gc1", "u1", "什么是 X", level=2)
    before = _gap_count(pid)
    res = confirm_gap_candidates(pid, [{"candidate_id": "gc1", "action": "confirm", "level": 2}])
    assert res == {"confirmed": 1, "discarded": 0, "skipped": 0}
    assert _gap_count(pid) == before + 1
    g = db.query(pid, "SELECT * FROM gap WHERE unit_id=?", ("u1",))[0]
    assert g["status"] == "待补"
    assert g["level"] == 2
    assert g["text"] == "什么是 X"
    assert _cand_status(pid, "gc1") == "已确认"


# confirm 缺省等级取 suggested_level
def test_confirm_default_level_from_candidate():
    pid = _new_pid()
    _make_unit(pid, "u1")
    _make_candidate(pid, "gc1", "u1", "什么是 Y", level=3)
    confirm_gap_candidates(pid, [{"candidate_id": "gc1", "action": "confirm"}])  # 不传 level
    g = db.query(pid, "SELECT * FROM gap WHERE unit_id=?", ("u1",))[0]
    assert g["level"] == 3


# discard：候选置废弃，不入 gap
def test_discard_marks_candidate_abandoned():
    pid = _new_pid()
    _make_unit(pid, "u1")
    _make_candidate(pid, "gc1", "u1", "无关紧要", level=1)
    before = _gap_count(pid)
    res = confirm_gap_candidates(pid, [{"candidate_id": "gc1", "action": "discard"}])
    assert res == {"confirmed": 0, "discarded": 1, "skipped": 0}
    assert _gap_count(pid) == before  # 无 gap 写入
    assert _cand_status(pid, "gc1") == "废弃"


# 幂等：重复确认同一候选只建一条 gap
def test_confirm_idempotent():
    pid = _new_pid()
    _make_unit(pid, "u1")
    _make_candidate(pid, "gc1", "u1", "重复确认", level=1)
    confirm_gap_candidates(pid, [{"candidate_id": "gc1", "action": "confirm", "level": 1}])
    confirm_gap_candidates(pid, [{"candidate_id": "gc1", "action": "confirm", "level": 1}])
    assert _gap_count(pid) == 1
    assert _cand_status(pid, "gc1") == "已确认"


# 非法 action → C400001
def test_confirm_invalid_action_c400001():
    pid = _new_pid()
    _make_unit(pid, "u1")
    _make_candidate(pid, "gc1", "u1", "X", level=1)
    with pytest.raises(BizError) as exc:
        confirm_gap_candidates(pid, [{"candidate_id": "gc1", "action": "frobnicate"}])
    assert exc.value.code == "C400001"


# confirm 等级非 1/2/3 → C400001（suggested_level 非法且不传 level）
def test_confirm_invalid_level_c400001():
    pid = _new_pid()
    _make_unit(pid, "u1")
    _make_candidate(pid, "gc1", "u1", "X", level=5)
    with pytest.raises(BizError) as exc:
        confirm_gap_candidates(pid, [{"candidate_id": "gc1", "action": "confirm"}])
    assert exc.value.code == "C400001"


# 候选不存在 → 安全跳过（幂等重放）
def test_confirm_missing_candidate_skipped():
    pid = _new_pid()
    res = confirm_gap_candidates(pid, [{"candidate_id": "nope", "action": "confirm", "level": 1}])
    assert res["skipped"] == 1


# list_gaps：过滤 + 返回 VO
def test_list_gaps_filters():
    pid = _new_pid()
    _make_unit(pid, "u1")
    _make_candidate(pid, "gc1", "u1", "L1 缺口", level=1)
    _make_candidate(pid, "gc2", "u1", "L2 缺口", level=2)
    confirm_gap_candidates(
        pid,
        [
            {"candidate_id": "gc1", "action": "confirm", "level": 1},
            {"candidate_id": "gc2", "action": "confirm", "level": 2},
        ],
    )
    all_gaps = list_gaps(pid)
    assert len(all_gaps) == 2
    l1 = list_gaps(pid, level=1)
    assert len(l1) == 1 and l1[0].level == 1
    pending = list_gaps(pid, status="待补")
    assert len(pending) == 2
    assert all(hasattr(g, "to_dict") for g in all_gaps)


# ledger_summary：统计正确且不暴露原始对话
def test_ledger_summary_counts():
    pid = _new_pid()
    _make_unit(pid, "u1", status="CLOSED")
    _make_candidate(pid, "gc1", "u1", "A", level=1)
    _make_candidate(pid, "gc2", "u1", "B", level=2)
    confirm_gap_candidates(
        pid,
        [
            {"candidate_id": "gc1", "action": "confirm", "level": 1},
            {"candidate_id": "gc2", "action": "discard"},
        ],
    )
    # 手动补一条「已验证」缺口以验证状态分布
    db.write(
        pid,
        "gap",
        {
            "id": "g2", "unit_id": "u1", "level": 2, "text": "B", "status": "已验证",
            "created_at": db._now_utc(), "updated_at": db._now_utc(),
        },
        "system",
    )
    s = ledger_summary(pid)
    assert s["gaps"]["已验证"] == 1
    assert s["gaps"]["待补"] == 1
    assert s["gaps"]["total"] == 2
    assert s["units"]["total"] == 1
    assert s["units"].get("CLOSED") == 1
    assert "turn" not in s  # 不暴露原始对话


# 会话收尾：确认后 session.gaps_confirmed 置 1（M9 零缺口占比分母口径，§3.2.M7.5 Step 3）
def test_confirm_marks_session_gaps_confirmed():
    pid = _new_pid()
    _make_unit(pid, "u1")
    db.write(pid, "session", {"id": "s1", "unit_id": "u1", "started_at": db._now_utc()}, "system")
    _make_candidate(pid, "gc1", "u1", "A", level=1)
    confirm_gap_candidates(pid, [{"candidate_id": "gc1", "action": "confirm", "level": 1}])
    s = db.query(pid, "SELECT gaps_confirmed FROM session WHERE id=?", ("s1",))[0]
    assert s["gaps_confirmed"] == 1


# M7 写路径可撤销：确认生成的 gap 经 undo 回滚删除
def test_confirmed_gap_undo_removes_it():
    pid = _new_pid()
    _make_unit(pid, "u1")
    _make_candidate(pid, "gc1", "u1", "可撤销", level=1)
    confirm_gap_candidates(pid, [{"candidate_id": "gc1", "action": "confirm", "level": 1}])
    assert _gap_count(pid) == 1
    aid = db.query(
        pid, "SELECT id FROM audit_log WHERE table_name='gap' ORDER BY id DESC LIMIT 1"
    )[0]["id"]
    assert db.undo(pid, aid, actor="user") is True
    assert _gap_count(pid) == 0  # gap 被删除（prev=None 插入回滚）
    assert _cand_status(pid, "gc1") == "已确认"  # 候选状态不变（独立审计条目）
