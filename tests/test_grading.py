"""M4 判分协议测试（AC-M4-5/6，关键模块）。

覆盖：达标闭环、强行闭环理由必填（A050003）、确认得分缺失（A050004）、
关键卡点未清零阻断（A050002）、非 JUDGE 拒绝、未达标未强行拒绝、
close_unit 写账（closed_at / forced_close / close_reason）并 move(JUDGE→INTEGRATE)。
用临时 HPSL_HOME（conftest 已支持），不污染真实库。
"""

import pytest

from hpsl import db, projects
from hpsl.common.enums import UnitState
from hpsl.common.errors import BizError
from hpsl.engine.grading import (
    close_unit,
    evaluate_close,
    has_unresolved_keypoint,
)


def _make_project():
    vo = projects.create_project(name="M4判分测试", goal="验证判分协议")
    return vo.id


def _make_judge_unit(pid, uid="u1"):
    db.write(
        pid,
        "unit",
        {"id": uid, "title": "测试单元", "status": UnitState.JUDGE.value},
        actor="user",
    )


def test_evaluate_pass_close_allowed():
    pid = _make_project()
    _make_judge_unit(pid)
    d = evaluate_close(pid, "u1", 0.8)
    assert d.close_allowed is True
    assert d.forced is False
    assert d.passed is True


def test_evaluate_force_close_requires_reason_a050003():
    pid = _make_project()
    _make_judge_unit(pid)
    with pytest.raises(BizError) as exc:
        evaluate_close(pid, "u1", 0.5, force_close=True)
    assert exc.value.code == "A050003"


def test_evaluate_missing_score_a050004():
    pid = _make_project()
    _make_judge_unit(pid)
    with pytest.raises(BizError) as exc:
        evaluate_close(pid, "u1", None)
    assert exc.value.code == "A050004"


def test_evaluate_residual_blocks_a050002():
    pid = _make_project()
    _make_judge_unit(pid, "u2")
    db.write(
        pid, "claim_check",
        {"id": "c1", "claim": "某实质错误", "unit_id": "u2", "providers": [], "residual": 1},
        actor="user",
    )
    assert has_unresolved_keypoint(pid, "u2") is True
    with pytest.raises(BizError) as exc:
        evaluate_close(pid, "u2", 0.9)
    assert exc.value.code == "A050002"


def test_evaluate_not_judge_rejected_a020001():
    pid = _make_project()
    db.write(
        pid, "unit", {"id": "u3", "title": "t", "status": UnitState.INIT.value}, actor="user"
    )
    with pytest.raises(BizError) as exc:
        evaluate_close(pid, "u3", 0.9)
    assert exc.value.code == "A020001"


def test_evaluate_not_passed_no_force_rejected():
    pid = _make_project()
    _make_judge_unit(pid, "u4")
    with pytest.raises(BizError) as exc:
        close_unit(pid, "u4", 0.5)  # 未达标且未强行
    assert exc.value.code == "A020001"


def test_close_unit_pass_writes_and_moves():
    pid = _make_project()
    _make_judge_unit(pid, "u5")
    vo = close_unit(pid, "u5", 0.85)
    assert vo.status == UnitState.INTEGRATE.value
    rows = db.query(pid, "SELECT * FROM unit WHERE id=?", ("u5",))
    unit = rows[0]
    assert unit["closed_at"] is not None
    assert unit["forced_close"] == 0
    assert unit["close_reason"] is None
    assert unit["status"] == UnitState.INTEGRATE.value


def test_close_unit_force_writes_forced_and_reason():
    pid = _make_project()
    _make_judge_unit(pid, "u6")
    vo = close_unit(pid, "u6", 0.5, force_close=True, close_reason="学习者已理解核心，强行闭环")
    assert vo.status == UnitState.INTEGRATE.value
    rows = db.query(pid, "SELECT * FROM unit WHERE id=?", ("u6",))
    unit = rows[0]
    assert unit["forced_close"] == 1
    assert unit["close_reason"] == "学习者已理解核心，强行闭环"
    # forced_close 不计入进度的标记已在库内落地（M9 统计剔除依据）
    assert unit["status"] == UnitState.INTEGRATE.value


def test_close_unit_residual_blocks_a050002():
    pid = _make_project()
    _make_judge_unit(pid, "u7")
    db.write(
        pid, "claim_check",
        {"id": "c2", "claim": "残留错误", "unit_id": "u7", "providers": [], "residual": 1},
        actor="user",
    )
    with pytest.raises(BizError) as exc:
        close_unit(pid, "u7", 0.9)
    assert exc.value.code == "A050002"
