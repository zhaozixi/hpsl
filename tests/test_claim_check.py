"""M5 挑错与异构 / BD-03 同源降级 / A050001 守卫测试（AC-M5-1~5）。

覆盖：
- F7.1 四规则触发（关键卡点 / 代码数字引用 / 答案机分歧 v0 恒 false / 手动）。
- F7.2 同源降级判定（heterogeneous 复用）+ 同源警告落账（agree=NULL）+ 关键卡点不自动闭环。
- 用户仲裁（采纳/驳回/待验证；待验证不阻断闭环）。
- BD-03 闭环门控：同源关键卡点 evaluate_close 拒绝自动闭环（close_allowed=False），
  须 explicit_confirm 才能闭环并留痕；治理计数（AC-5.4）恒为 0。
- A050001 评分要点作答前不可见守卫。
- 复用 M4 heterogeneous()/evaluate_close()，零 DDL；M4 既有 112 测试须仍全绿（AC-M5 回归）。

用临时 HPSL_HOME（conftest 已支持），不污染真实库。
"""

import json

import pytest

from hpsl import db, projects
from hpsl.common.enums import UnitState
from hpsl.common.errors import BizError
from hpsl.engine import claim_check as cc
from hpsl.engine.grading import (
    close_unit,
    evaluate_close,
    filter_judgment_data,
    has_unresolved_keypoint,
    rubric_reveal_allowed,
)

# 模型（当前 models.yaml 只启用国内三家）：
#   deepseek-flash(family deepseek) / qwen-plus(family qwen) /
#   deepseek-chat(family deepseek, derived_from gpt4o)
CRITIC_DS = "deepseek-flash"
ANSWER_QWEN = "qwen-plus"
CRITIC_DS_CHAT = "deepseek-chat"  # 与 deepseek-flash 同 family → 同源


def _make_project():
    vo = projects.create_project(name="M5挑错测试", goal="验证挑错与同源降级")
    return vo.id


def _make_judge_unit(pid, uid="u1", keypoint=False):
    db.write(
        pid, "unit",
        {"id": uid, "title": "测试单元", "status": UnitState.JUDGE.value,
         "degraded_from": ("k" if keypoint else None)},
        actor="user",
    )


# ---------------------------------------------------------------------------
# F7.1 四规则触发
# ---------------------------------------------------------------------------
def test_evaluate_triggers_keypoint():
    rules = cc.evaluate_triggers(is_keypoint=True, has_code_num_ref=False, manual=False)
    assert rules == [cc.TriggerRule.KEYPOINT]


def test_evaluate_triggers_code_num_ref():
    rules = cc.evaluate_triggers(is_keypoint=False, has_code_num_ref=True, manual=False)
    assert rules == [cc.TriggerRule.CODE_NUM_REF]


def test_evaluate_triggers_manual():
    rules = cc.evaluate_triggers(is_keypoint=False, has_code_num_ref=False, manual=True)
    assert rules == [cc.TriggerRule.MANUAL]


def test_evaluate_triggers_answer_disagreement_reserved_false_in_v0():
    # 规则 ③ 在 v0 单答案机下恒不触发（基线冻结，参数占位但永不命中）
    rules = cc.evaluate_triggers(
        is_keypoint=False, has_code_num_ref=False, manual=False, answer_disagreement=True
    )
    assert rules == []


def test_should_trigger_none():
    assert cc.should_trigger_critic(is_keypoint=False, has_code_num_ref=False, manual=False) is False
    assert cc.should_trigger_critic(is_keypoint=True, has_code_num_ref=False, manual=False) is True


# ---------------------------------------------------------------------------
# F7.2 同源降级判定
# ---------------------------------------------------------------------------
def test_same_source_true_for_distilled():
    # deepseek-chat 与 deepseek-flash 同 family → 同源
    assert cc.same_source(CRITIC_DS, CRITIC_DS_CHAT) is True


def test_same_source_false_for_diff_families():
    assert cc.same_source(CRITIC_DS, ANSWER_QWEN) is False


# ---------------------------------------------------------------------------
# claim_check 落账（零 DDL）
# ---------------------------------------------------------------------------
def test_write_claim_check_and_read():
    pid = _make_project()
    _make_judge_unit(pid, "u1")
    cid = cc.write_claim_check(
        pid, claim="某断言", unit_id="u1",
        providers=[CRITIC_DS, ANSWER_QWEN], agree=0, diff='{"x":1}', residual=1,
    )
    rows = db.query(pid, "SELECT * FROM claim_check WHERE id=?", (cid,))
    row = rows[0]
    assert row["claim"] == "某断言"
    assert row["agree"] == 0
    assert row["residual"] == 1
    # providers 为 JSON 字段，db.query 返回字符串，调用方解码
    assert json.loads(row["providers"]) == [CRITIC_DS, ANSWER_QWEN]


def test_record_same_source_warning():
    pid = _make_project()
    _make_judge_unit(pid, "u1", keypoint=True)
    cid = cc.record_same_source_warning(
        pid, unit_id="u1", critic_model_id=CRITIC_DS, answer_model_id=CRITIC_DS_CHAT,
        is_keypoint=True,
    )
    rows = db.query(pid, "SELECT * FROM claim_check WHERE id=?", (cid,))
    row = rows[0]
    assert row["agree"] is None  # 同源降级未评
    prov = json.loads(row["providers"])
    assert prov["same_source"] is True
    assert prov["keypoint"] is True
    assert "共识幻觉风险" in (row["diff"] or "")


# ---------------------------------------------------------------------------
# 用户仲裁（待验证不阻断）
# ---------------------------------------------------------------------------
def test_arbitrate_adopt_blocks():
    pid = _make_project()
    _make_judge_unit(pid, "u1")
    cid = cc.write_claim_check(pid, claim="错误断言", unit_id="u1", residual=1)
    cc.arbitrate(pid, claim_id=cid, ruling=cc.RULING_ADOPT)
    assert has_unresolved_keypoint(pid, "u1") is True  # 采纳 → 阻断


def test_arbitrate_reject_not_block():
    pid = _make_project()
    _make_judge_unit(pid, "u1")
    cid = cc.write_claim_check(pid, claim="误报", unit_id="u1", residual=1)
    cc.arbitrate(pid, claim_id=cid, ruling=cc.RULING_REJECT)
    assert has_unresolved_keypoint(pid, "u1") is False  # 驳回 → 不阻断


def test_arbitrate_pending_not_block():
    pid = _make_project()
    _make_judge_unit(pid, "u1")
    cid = cc.write_claim_check(pid, claim="存疑", unit_id="u1", residual=1)
    cc.arbitrate(pid, claim_id=cid, ruling=cc.RULING_PENDING)
    # 待验证：残留但不阻断闭环（仅入质量度量）
    assert has_unresolved_keypoint(pid, "u1") is False


def test_arbitrate_invalid_ruling():
    pid = _make_project()
    _make_judge_unit(pid, "u1")
    cid = cc.write_claim_check(pid, claim="x", unit_id="u1")
    with pytest.raises(BizError) as exc:
        cc.arbitrate(pid, claim_id=cid, ruling="瞎裁决")
    assert exc.value.code == "C400001"


# ---------------------------------------------------------------------------
# BD-03 同源关键卡点闭环门控
# ---------------------------------------------------------------------------
def test_close_gate_ss_keypoint_blocks_auto():
    pid = _make_project()
    _make_judge_unit(pid, "u1", keypoint=True)
    d = evaluate_close(pid, "u1", 0.9, is_keypoint=True, same_source=True)
    assert d.close_allowed is False
    assert d.ss_keypoint_block is True
    assert d.forced is False


def test_close_gate_ss_keypoint_explicit_confirm_ok():
    pid = _make_project()
    _make_judge_unit(pid, "u1", keypoint=True)
    # 进入判定前检查已写同源降级警告（真实流程由 trigger_critic_if_needed 触发）
    cc.record_same_source_warning(
        pid, unit_id="u1", critic_model_id=CRITIC_DS, answer_model_id=CRITIC_DS_CHAT,
        is_keypoint=True,
    )
    d = evaluate_close(pid, "u1", 0.9, is_keypoint=True, same_source=True, explicit_confirm=True)
    assert d.close_allowed is True
    assert d.forced is False
    # 真实闭环：forced_close=0 且留痕
    vo = close_unit(pid, "u1", 0.9, is_keypoint=True, same_source=True, explicit_confirm=True)
    assert vo.status == UnitState.INTEGRATE.value
    rows = db.query(pid, "SELECT * FROM unit WHERE id=?", ("u1",))
    unit = rows[0]
    assert unit["forced_close"] == 0
    assert unit["closed_at"] is not None
    # 同源警告行已标记为「同源显式确认」
    cc_rows = db.query(pid, "SELECT * FROM claim_check WHERE unit_id=? AND agree IS NULL", ("u1",))
    assert any(r["user_ruling"] == cc.RULING_SS_CONFIRM for r in cc_rows)


def test_close_gate_ss_keypoint_auto_rejected():
    pid = _make_project()
    _make_judge_unit(pid, "u1", keypoint=True)
    with pytest.raises(BizError) as exc:
        close_unit(pid, "u1", 0.9, is_keypoint=True, same_source=True)
    assert exc.value.code == "A020001"
    assert "同源" in exc.value.msg  # BD-03 文案区分


def test_hetero_keypoint_normal_close_allowed():
    pid = _make_project()
    _make_judge_unit(pid, "u1", keypoint=True)
    d = evaluate_close(pid, "u1", 0.9, is_keypoint=True, same_source=False)
    assert d.close_allowed is True
    assert d.ss_keypoint_block is False


def test_governance_count_zero_after_explicit_confirm():
    pid = _make_project()
    _make_judge_unit(pid, "u1", keypoint=True)
    close_unit(pid, "u1", 0.9, is_keypoint=True, same_source=True, explicit_confirm=True)
    assert cc.count_same_source_keypoint_auto_close(pid) == 0


def test_governance_count_zero_no_violation():
    pid = _make_project()
    _make_judge_unit(pid, "u1", keypoint=True)
    # 未触发闭环（仅写同源警告），不应计为自动闭环违规
    cc.record_same_source_warning(
        pid, unit_id="u1", critic_model_id=CRITIC_DS, answer_model_id=CRITIC_DS_CHAT,
        is_keypoint=True,
    )
    assert cc.count_same_source_keypoint_auto_close(pid) == 0


# ---------------------------------------------------------------------------
# 触发编排（trigger_critic_if_needed）
# ---------------------------------------------------------------------------
def test_trigger_critic_same_source_writes_warning():
    pid = _make_project()
    _make_judge_unit(pid, "u1", keypoint=True)
    ids = cc.trigger_critic_if_needed(
        pid, unit_id="u1", is_keypoint=True,
        critic_model_id=CRITIC_DS, answer_model_id=CRITIC_DS_CHAT,
    )
    assert len(ids) == 1
    row = db.query(pid, "SELECT * FROM claim_check WHERE id=?", (ids[0],))[0]
    assert row["agree"] is None
    assert json.loads(row["providers"])["same_source"] is True


def test_trigger_critic_not_needed_returns_empty():
    pid = _make_project()
    _make_judge_unit(pid, "u1")
    ids = cc.trigger_critic_if_needed(
        pid, unit_id="u1", is_keypoint=False, has_code_num_ref=False, manual=False,
        critic_model_id=CRITIC_DS, answer_model_id=ANSWER_QWEN,
    )
    assert ids == []


# ---------------------------------------------------------------------------
# A050001 评分要点作答前不可见守卫
# ---------------------------------------------------------------------------
def test_rubric_reveal_allowed():
    assert rubric_reveal_allowed(answered=False, self_scored=False) is False
    assert rubric_reveal_allowed(answered=True, self_scored=False) is False
    assert rubric_reveal_allowed(answered=False, self_scored=True) is False
    assert rubric_reveal_allowed(answered=True, self_scored=True) is True


def test_filter_judgment_data_hides_rubric_before_answered_self_scored():
    data = {"production": "我的重述", "self_score": 0.6, "rubric": "对照要点：…"}
    # 未达成「产出+自评」：剔除 rubric
    redacted = filter_judgment_data(data, answered=False, self_scored=False)
    assert "rubric" not in redacted
    assert "production" in redacted
    # 达成后：保留 rubric
    revealed = filter_judgment_data(data, answered=True, self_scored=True)
    assert revealed.get("rubric") == "对照要点：…"
