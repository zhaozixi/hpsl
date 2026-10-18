"""复测队列测试（AC-M2-4 / 5 / 6 + 入队幂等 + 隔离）。

全部走临时 HPSL_HOME（conftest 已支持），不污染真实 ~/.hpsl。
"""

import pytest

from hpsl import db, projects
from hpsl.engine.review_queue import (
    enqueue_reviews,
    due_reviews,
    submit_review,
    submit_review_by_id,
    start_review,
    ReviewStartVO,
    REVIEW_INTERVALS,
    _add_days,
)
from hpsl.common.errors import BizError


def _new_pid():
    return projects.create_project("复测测试", "g").id


def _insert_concept(pid, cid, name=None, verify_status="未验证"):
    db.write(pid, "concept", {"id": cid, "name": name or f"概念-{cid}", "verify_status": verify_status}, "user")
    return cid


def _unit_count(pid):
    return db.query(pid, "SELECT count(*) c FROM unit")[0]["c"]


def _gap_count(pid):
    return db.query(pid, "SELECT count(*) c FROM gap")[0]["c"]


def _review_active_count(pid, cid):
    return db.query(
        pid, "SELECT count(*) c FROM review WHERE concept_id=? AND result IS NULL", (cid,)
    )[0]["c"]


# AC-M2-4：submit_review 不创建/开始任何单元，单元状态机零转移
def test_submit_review_creates_no_unit():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    units_before = _unit_count(pid)
    submit_review(pid, "c1", 0.9)
    assert _unit_count(pid) == units_before  # 未创建任何单元
    assert _unit_count(pid) == 0  # 且没有任何单元行


# AC-M2-5：写回 concept + attempt(复测) + review.result；当前单元账本零写入
def test_submit_review_writes_back_and_isolates():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1", verify_status="未验证")
    closed_at = db._now_utc()
    enqueue_reviews(pid, ["c1"], closed_at)
    enqueue_due = db.query(pid, "SELECT due FROM review WHERE concept_id=?", ("c1",))[0]["due"]

    units_before = _unit_count(pid)
    gaps_before = _gap_count(pid)

    res = submit_review(pid, "c1", 0.8)  # >= 0.7 通过

    # 写回 concept
    c = db.query(pid, "SELECT * FROM concept WHERE id=?", ("c1",))[0]
    assert c["verify_status"] == "已验证"
    assert c["last_verified_at"] is not None
    # attempt(kind=复测) 承载复测结果
    a = db.query(pid, "SELECT * FROM attempt WHERE concept_id=?", ("c1",))[0]
    assert a["kind"] == "复测"
    assert a["confirmed_score"] == 0.8
    # review 行保持活跃（result 维持 NULL，D-M2-1 修复），仅 due 推进到下一档，并回写 attempt_id
    r = db.query(pid, "SELECT * FROM review WHERE concept_id=?", ("c1",))[0]
    assert r["result"] is None
    assert r["attempt_id"] == a["id"]
    assert _review_active_count(pid, "c1") == 1
    assert r["due"] > enqueue_due  # due 已推进
    # 当前单元账本零写入（隔离不变量）
    assert _unit_count(pid) == units_before
    assert _gap_count(pid) == gaps_before
    # 纪律不变量
    assert res.wrote_to_current_unit is False
    assert res.passed is True
    assert res.review_id == r["id"]


# AC-M2-5 未通过分支
def test_submit_review_fails_below_threshold():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1", verify_status="已验证")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    res = submit_review(pid, "c1", 0.5)  # < 0.7 未通过
    c = db.query(pid, "SELECT * FROM concept WHERE id=?", ("c1",))[0]
    assert c["verify_status"] == "待验证"
    assert res.passed is False
    # 复测结果以 attempt 为准；review 行保持活跃（D-M2-1 修复）
    a = db.query(pid, "SELECT * FROM attempt WHERE concept_id=?", ("c1",))[0]
    assert a["confirmed_score"] == 0.5
    r = db.query(pid, "SELECT * FROM review WHERE concept_id=?", ("c1",))[0]
    assert r["result"] is None
    assert _review_active_count(pid, "c1") == 1


# D-M2-1 修复验证：复测间隔 2→7→21→21 循环，每次复测后 review 保持活跃、due 推进到下一档
def test_review_interval_cycling():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1")
    closed_at = db._now_utc()
    enqueue_reviews(pid, ["c1"], closed_at)  # due = closed_at + 2d
    enqueue_due = db.query(pid, "SELECT due FROM review WHERE concept_id=?", ("c1",))[0]["due"]
    assert enqueue_due == _add_days(closed_at, 2)

    # 第 1 次复测（2 天档）：now≈closed_at → due 推进到 ~closed_at+7d，review 保持活跃
    submit_review(pid, "c1", 0.9)
    r1 = db.query(pid, "SELECT * FROM review WHERE concept_id=?", ("c1",))[0]
    assert r1["result"] is None
    assert _review_active_count(pid, "c1") == 1
    assert r1["due"] >= _add_days(closed_at, 7)

    # 越过新 due 后，due_reviews 再次返回 c1
    items = due_reviews(pid, now=_add_days(closed_at, 8))
    assert [it.concept_id for it in items] == ["c1"]

    # 第 2 次复测（7 天档）：due 推进到 ~closed_at+21d
    submit_review(pid, "c1", 0.9)
    r2 = db.query(pid, "SELECT * FROM review WHERE concept_id=?", ("c1",))[0]
    assert r2["result"] is None
    assert _review_active_count(pid, "c1") == 1
    assert r2["due"] >= _add_days(closed_at, 21)

    # 越过新 due 后，due_reviews 返回 c1 且 interval_days==21
    items = due_reviews(pid, now=_add_days(closed_at, 22))
    assert [it.concept_id for it in items] == ["c1"]
    assert items[0].interval_days == 21

    # 第 3 次复测（21→21 循环）：due 仍停在 ~closed_at+21d，review 保持活跃
    submit_review(pid, "c1", 0.9)
    r3 = db.query(pid, "SELECT * FROM review WHERE concept_id=?", ("c1",))[0]
    assert r3["result"] is None
    assert _review_active_count(pid, "c1") == 1
    assert r3["due"] >= _add_days(closed_at, 21)


# AC-M2-6：写回经 db.write 同事务审计，审计条目必存在（attempt + concept + review = 3 笔）
def test_submit_review_audit_entries():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    before = db.query(pid, "SELECT count(*) c FROM audit_log")[0]["c"]
    submit_review(pid, "c1", 0.9)
    after = db.query(pid, "SELECT count(*) c FROM audit_log")[0]["c"]
    assert after - before == 3
    recent = db.query(
        pid, "SELECT table_name FROM audit_log WHERE id > (SELECT max(id) FROM audit_log) - 3"
    )
    tables = {row["table_name"] for row in recent}
    assert {"attempt", "concept", "review"} <= tables


# 入队幂等：重复 enqueue 不产生重复活跃行
def test_enqueue_idempotent():
    pid = _new_pid()
    cid1 = _insert_concept(pid, "c1")
    cid2 = _insert_concept(pid, "c2")
    closed_at = db._now_utc()
    enqueue_reviews(pid, ["c1"], closed_at)
    enqueue_reviews(pid, ["c1"], closed_at)  # 重复
    assert _review_active_count(pid, "c1") == 1
    # 一次批量入队多概念，各自一条活跃行；已存在的被跳过
    enqueue_reviews(pid, ["c1", "c2"], closed_at)
    assert _review_active_count(pid, "c1") == 1
    assert _review_active_count(pid, "c2") == 1


# due_reviews：读时到期，按 due 升序返回
def test_due_reviews_returns_due_items():
    pid = _new_pid()
    cid1 = _insert_concept(pid, "c1")
    cid2 = _insert_concept(pid, "c2")
    future = db._now_utc()
    enqueue_reviews(pid, ["c1", "c2"], future)
    # 以远未来为 now，两条均到期
    items = due_reviews(pid, now="2999-01-01T00:00:00.000Z")
    assert {it.concept_id for it in items} == {"c1", "c2"}
    assert len(items) == 2
    # 默认 now=当前，due = future+2d > now(≈future)，故不在到期列表
    assert due_reviews(pid) == []


# submit_review 无活跃复测 → A060001
def test_submit_without_active_review_a060001():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1")
    with pytest.raises(BizError) as exc:
        submit_review(pid, cid, 0.9)
    assert exc.value.code == "A060001"


# REVIEW_INTERVALS 常量
def test_review_intervals_constant():
    assert REVIEW_INTERVALS == (2, 7, 21)


# ===========================================================================
# M6 新增：start_review / submit_review_by_id 复测卷幂等 + Q1 纪律断言
# ===========================================================================

# M6：start_review 返回 ReviewStartVO 且字段齐全（含出题者传入的 question 透传）
def test_start_review_returns_vo():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1", name="概念甲")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    rid = db.query(pid, "SELECT id FROM review WHERE concept_id=?", ("c1",))[0]["id"]
    vo = start_review(pid, rid, question="边界题：请解释 X 与 Y 的区别")
    assert isinstance(vo, ReviewStartVO)
    assert vo.review_id == rid
    assert vo.concept_id == "c1"
    assert vo.concept_name == "概念甲"
    assert vo.question == "边界题：请解释 X 与 Y 的区别"
    assert vo.attempt_id  # 已建草稿卷
    assert vo.interval_days >= 1


# M6：start_review 幂等（单活跃卷），重复开始不重复建 attempt
def test_start_review_idempotent():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    rid = db.query(pid, "SELECT id FROM review WHERE concept_id=?", ("c1",))[0]["id"]
    vo1 = start_review(pid, rid)
    vo2 = start_review(pid, rid)  # 重复开始，应返回既有草稿卷
    assert vo1.attempt_id == vo2.attempt_id
    attempts = db.query(pid, "SELECT id FROM attempt WHERE concept_id=?", ("c1",))
    assert len(attempts) == 1  # 仅一条草稿卷
    assert _review_active_count(pid, "c1") == 1  # review 仍只一条活跃行


# M6：start_review 对不存在/已完成 review → A060001
def test_start_review_missing_review_a060001():
    pid = _new_pid()
    with pytest.raises(BizError) as exc:
        start_review(pid, "no-such-review")
    assert exc.value.code == "A060001"


# M6：submit_review_by_id 按 review_id 精确提交，写回隔离
def test_submit_review_by_id_writes_back_and_isolates():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1", verify_status="未验证")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    rid = db.query(pid, "SELECT id FROM review WHERE concept_id=?", ("c1",))[0]["id"]
    units_before = _unit_count(pid)
    gaps_before = _gap_count(pid)
    res = submit_review_by_id(pid, rid, 0.8)
    assert res.review_id == rid
    assert res.concept_id == "c1"
    assert res.wrote_to_current_unit is False
    c = db.query(pid, "SELECT * FROM concept WHERE id=?", ("c1",))[0]
    assert c["verify_status"] == "已验证"
    assert c["last_verified_at"] is not None
    r = db.query(pid, "SELECT * FROM review WHERE concept_id=?", ("c1",))[0]
    assert r["result"] is None  # 保持活跃
    assert r["attempt_id"] == res.attempt_id
    assert _unit_count(pid) == units_before
    assert _gap_count(pid) == gaps_before


# M6：submit_review_by_id 对不存在/已完成 review → A060001
def test_submit_review_by_id_missing_review_a060001():
    pid = _new_pid()
    with pytest.raises(BizError) as exc:
        submit_review_by_id(pid, "no-such-review", 0.9)
    assert exc.value.code == "A060001"


# M6：草稿卷复用（start_review 后 submit_review_by_id 不重复建 attempt）
def test_draft_volume_reuse():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    rid = db.query(pid, "SELECT id FROM review WHERE concept_id=?", ("c1",))[0]["id"]
    vo = start_review(pid, rid, question="边界题")
    attempts_before = db.query(pid, "SELECT id FROM attempt WHERE concept_id=?", ("c1",))
    assert len(attempts_before) == 1
    assert attempts_before[0]["id"] == vo.attempt_id
    # 经 start_review 建立的草稿卷提交，应复用而非新建
    res = submit_review_by_id(pid, rid, 0.9)
    assert res.attempt_id == vo.attempt_id
    attempts_after = db.query(pid, "SELECT id FROM attempt WHERE concept_id=?", ("c1",))
    assert len(attempts_after) == 1
    a = db.query(pid, "SELECT confirmed_score FROM attempt WHERE id=?", (vo.attempt_id,))[0]
    assert a["confirmed_score"] == 0.9  # 草稿卷已被评分


# M6 Q1 纪律断言：完整路径（start_review -> submit_review_by_id）误写当前单元 0 条；
# 写回概念准确率（满分 -> 已验证）
def test_m6_q1_discipline_no_current_unit_write():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1", verify_status="未验证")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    units_before = _unit_count(pid)
    gaps_before = _gap_count(pid)
    rid = db.query(pid, "SELECT id FROM review WHERE concept_id=?", ("c1",))[0]["id"]
    start_review(pid, rid, question="边界题")
    res = submit_review_by_id(pid, rid, 1.0)
    assert res.wrote_to_current_unit is False
    assert _unit_count(pid) == units_before  # 误写当前单元 0 条
    assert _gap_count(pid) == gaps_before
    c = db.query(pid, "SELECT verify_status FROM concept WHERE id=?", ("c1",))[0]
    assert c["verify_status"] == "已验证"  # 写回概念准确率（100% -> 已验证）


# M6：start_review 后 review.attempt_id 被回填（标记「复测中」），且仍保持唯一活跃行
def test_start_review_marks_review_in_progress():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    rid = db.query(pid, "SELECT id FROM review WHERE concept_id=?", ("c1",))[0]["id"]
    start_review(pid, rid, question="边界题")
    r = db.query(pid, "SELECT * FROM review WHERE id=?", (rid,))[0]
    assert r["attempt_id"] is not None
    assert r["result"] is None
    assert _review_active_count(pid, "c1") == 1


# M6 独立验证 G1：复测 attempt 绝不绑定单元（隔离红线 O13，列级断言）
def test_review_attempt_unit_id_is_null():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1")
    enqueue_reviews(pid, ["c1"], db._now_utc())
    rid = db.query(pid, "SELECT id FROM review WHERE concept_id=?", ("c1",))[0]["id"]
    start_review(pid, rid, question="边界题")
    a_draft = db.query(pid, "SELECT unit_id FROM attempt WHERE concept_id=?", ("c1",))[0]
    assert a_draft["unit_id"] is None  # 草稿卷不绑单元
    submit_review_by_id(pid, rid, 0.9)
    a_final = db.query(pid, "SELECT unit_id FROM attempt WHERE concept_id=?", ("c1",))[0]
    assert a_final["unit_id"] is None  # 定稿卷仍不绑单元


# M6 独立验证 G2：A060001「已完成」分支（review 存在但 result 非 NULL）
def test_a060001_completed_branch():
    pid = _new_pid()
    cid = _insert_concept(pid, "c1")
    # 直接构造已完成 review（result 非 NULL），绕过活跃约束
    db.write(
        pid,
        "review",
        {
            "id": "rv-done",
            "concept_id": cid,
            "kind": "到期",
            "due": db._now_utc(),
            "result": "通过",
        },
        "system",
    )
    with pytest.raises(BizError) as exc1:
        start_review(pid, "rv-done")
    assert exc1.value.code == "A060001"
    with pytest.raises(BizError) as exc2:
        submit_review_by_id(pid, "rv-done", 0.9)
    assert exc2.value.code == "A060001"
