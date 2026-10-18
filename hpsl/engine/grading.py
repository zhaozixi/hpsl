"""判分协议与 JUDGE 闭环门控（F6 判分协议，M4 落实）。

M2 的 state.move() 已使 JUDGE→INTEGRATE 合法，但未落实判分逻辑——本模块落实：
- evaluate_close：达标判定 + 关键卡点例外 + 强行闭环理由必填 + 确认得分缺失。
- close_unit：经判分校验后写 unit 闭环字段（closed_at / forced_close / close_reason）
  并 move(JUDGE→INTEGRATE)；全部经 db.write 同事务审计。

错误码（已注册）：
- A050001：评分要点（rubric）作答前不可见（顺序违规），M5 守卫 rubric_reveal_allowed。
- A050002：关键卡点挑错未清零，不可闭环。
- A050003：强行闭环缺少理由。
- A050004：确认得分缺失（None 或非数值），不可闭环。
- A020001：仅 JUDGE 状态可评估闭环 / 未达标且未强行闭环（非法闭环转移）/
          同源关键卡点未显式确认（BD-03 经 block_reason 文案区分）。

要点隐藏守卫（A050001，M5 落实）：rubric_reveal_allowed / filter_judgment_data 保证
评分要点仅在「学习者完成产出且自评」后才进入判定屏数据；M10 交互层调用本守卫，
不反向泄露 rubric 给模型调用方或提前返回给界面。

BD-03 同源降级门控（M5 落实，复用 M4 heterogeneous）：evaluate_close 新增 is_keypoint /
same_source / explicit_confirm 参数；关键卡点且同源时拒绝自动闭环（close_allowed=False，
same_source_keypoint_block=True），须学习者显式确认（explicit_confirm=True）方可闭环并留痕。

纪律（冻结）：所有写经 db.write；不引入 ORM；不依赖 routes/app，保持 headless；零 DDL。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .. import db
from ..common.enums import UnitState
from ..common.errors import BizError
from ..projects import get_effective_config
from . import claim_check as claim_check_mod
from . import review_queue
from . import state


@dataclass
class CloseDecisionVO:
    """闭环判定结果视图对象（不含任何密钥 / 隔离外字段）。"""

    close_allowed: bool
    forced: bool
    passed: bool
    pending_residual: bool
    reason: Optional[str]
    ss_keypoint_block: bool = False          # BD-03：同源关键卡点拒绝自动闭环
    block_reason: Optional[str] = None      # 拒绝文案（同源关键卡点 / 未达标等）


def has_unresolved_keypoint(pid: str, uid: str) -> bool:
    """关键卡点是否仍残留**阻断性**实质错误（claim_check.residual=1）。

    门控语义（§3.2.M5.5 / §5.5）：residual=1 且 user_ruling ∈ {NULL（未裁决）, 采纳（确为错误）}
    才阻断闭环；「待验证」与「驳回」不阻断（待验证仅入质量度量）。
    防御性：claim_check 表不在 / 查询异常 → 视为无残留（False）。
    """
    try:
        rows = db.query(
            pid,
            "SELECT count(*) AS c FROM claim_check "
            "WHERE unit_id=? AND residual=1 "
            "AND (user_ruling IS NULL OR user_ruling='采纳')",
            (uid,),
        )
    except sqlite3.OperationalError:
        # 表不存在等：视为无残留，放行
        return False
    if not rows:
        return False
    return int(rows[0].get("c", 0)) > 0


def evaluate_close(
    pid: str,
    uid: str,
    confirmed_score: Optional[float],
    *,
    force_close: bool = False,
    close_reason: Optional[str] = None,
    is_keypoint: bool = False,
    same_source: Optional[bool] = None,
    explicit_confirm: bool = False,
) -> CloseDecisionVO:
    """评估单元是否允许闭环（JUDGE 状态）。

    门控顺序（任一命中即抛业务错误，不返回 CloseDecisionVO）：
    1. confirmed_score 为 None 或非数值 → A050004（确认得分缺失）。
    2. 单元不存在 → A020001。
    3. 单元状态非 JUDGE → A020001（仅 JUDGE 可评估闭环）。
    4. 关键卡点残留阻断性实质错误（residual=1 且未裁决/采纳）→ A050002。
    5. 强行闭环但理由为空 → A050003。
    6. 理由超 500 字符 → C400001（参数校验）。

    BD-03 同源降级门控（M5）：若 is_keypoint 且 same_source=True（挑错者与答案机同源），
    关键卡点单元不得自动闭环——close_allowed=False, same_source_keypoint_block=True；
    须学习者显式确认（explicit_confirm=True）方可闭环并留痕。

    返回 CloseDecisionVO：
    - 达标且非强行（且非同源关键卡点阻拦）：close_allowed=True, forced=False。
    - 未达标但强行且理由齐：close_allowed=True, forced=True。
    - 同源关键卡点且未显式确认：close_allowed=False, ss_keypoint_block=True。
    - 其余：close_allowed=False（由调用方据 reason 决定回补 LEARN 或提示）。
    """
    if not isinstance(confirmed_score, (int, float)):
        raise BizError("A050004", msg="确认得分缺失（非数值），不可闭环")

    rows = db.query(pid, "SELECT * FROM unit WHERE id=?", (uid,))
    if not rows:
        raise BizError("A020001", msg=f"单元 {uid} 不存在，无法评估闭环")
    unit = rows[0]
    if unit.get("status") != UnitState.JUDGE.value:
        raise BizError("A020001", msg=f"仅 JUDGE 状态可评估闭环，当前为 {unit.get('status')}")

    pending_residual = has_unresolved_keypoint(pid, uid)
    if pending_residual:
        raise BizError("A050002", msg="关键卡点挑错仍有未清零的实质错误，不可闭环")

    config = get_effective_config(pid)
    passed = float(confirmed_score) >= float(config.pass_threshold)

    if force_close and not close_reason:
        raise BizError("A050003", msg="强行闭环必须填写理由")
    if close_reason and len(close_reason) > 500:
        raise BizError("C400001", msg="闭环理由过长（≤500 字）")

    # BD-03：同源关键卡点拒绝自动闭环，须显式确认
    ss_block = bool(is_keypoint) and (same_source is True)
    if ss_block and not explicit_confirm:
        return CloseDecisionVO(
            close_allowed=False,
            forced=False,
            passed=passed,
            pending_residual=False,
            reason=None,
            ss_keypoint_block=True,
            block_reason="同源关键卡点：挑错者与答案机同源，共识幻觉风险未降低，不得自动闭环，需学习者显式确认",
        )

    if passed and not force_close:
        return CloseDecisionVO(
            close_allowed=True, forced=False, passed=True, pending_residual=False, reason=None,
            ss_keypoint_block=ss_block,
        )
    if (not passed) and force_close and close_reason:
        return CloseDecisionVO(
            close_allowed=True, forced=True, passed=False, pending_residual=False,
            reason=close_reason, ss_keypoint_block=ss_block,
        )
    return CloseDecisionVO(
        close_allowed=False, forced=force_close, passed=passed, pending_residual=False,
        reason=None, ss_keypoint_block=ss_block,
    )


def close_unit(
    pid: str,
    uid: str,
    confirmed_score: Optional[float],
    *,
    force_close: bool = False,
    close_reason: Optional[str] = None,
    is_keypoint: bool = False,
    same_source: Optional[bool] = None,
    explicit_confirm: bool = False,
) -> state.UnitStateVO:
    """经判分校验后闭环单元。

    1. evaluate_close 校验（不通过即抛对应错误；同源关键卡点未显式确认 → block_reason 文案）。
    2. BD-03 显式确认留痕：若同源关键卡点经 explicit_confirm 闭环，标记同源警告行为已确认。
    3. 写 unit 闭环字段（closed_at / forced_close / close_reason）经 db.write 审计。
    4. move(JUDGE→INTEGRATE) 经 db.write 审计。
    5. 返回新状态 VO。
    """
    decision = evaluate_close(
        pid, uid, confirmed_score, force_close=force_close, close_reason=close_reason,
        is_keypoint=is_keypoint, same_source=same_source, explicit_confirm=explicit_confirm,
    )
    if not decision.close_allowed:
        raise BizError(
            "A020001",
            msg=decision.block_reason or "未达标且未选择强行闭环，不可闭环",
        )

    # BD-03：同源关键卡点显式确认闭环，标记同源警告行留痕（区分「自动闭环违规」）
    if decision.ss_keypoint_block and explicit_confirm:
        claim_check_mod.mark_same_source_confirmed(pid, unit_id=uid, actor="user")

    now = db._now_utc()
    db.write(
        pid,
        "unit",
        {
            "id": uid,
            "closed_at": now,
            "forced_close": 1 if force_close else 0,
            "close_reason": close_reason if force_close else None,
        },
        actor="system",
    )
    return state.move(pid, uid, UnitState.JUDGE, UnitState.INTEGRATE, actor="system")


def enqueue_unit_reviews(pid: str, uid: str, closed_at: Optional[str] = None) -> list[str]:
    """单元闭环后为其涉及的概念入队延迟复测（跨单元队列，零 DDL）。

    - 概念来源：本单元 `attempt.concept_id` 去重（作答时已关联的概念）。
    - 无关联概念 → 返回空列表（不阻塞闭环；页面提示"本单元未关联概念，暂无复测"）。
    - 幂等：`enqueue_reviews` 对已有活跃 review 行跳过，可重复调用。
    """
    rows = db.query(
        pid,
        "SELECT DISTINCT concept_id FROM attempt WHERE unit_id=? AND concept_id IS NOT NULL",
        (uid,),
    )
    concept_ids = [r["concept_id"] for r in rows if r.get("concept_id")]
    if not concept_ids:
        return []
    if closed_at is None:
        # 以单元的闭环时间为基准（due = closed_at + 2d），而非入队瞬间
        unit_rows = db.query(pid, "SELECT closed_at FROM unit WHERE id=?", (uid,))
        closed_at = (unit_rows[0].get("closed_at") if unit_rows else None) or db._now_utc()
    return review_queue.enqueue_reviews(pid, concept_ids, closed_at)


# ---------------------------------------------------------------------------
# A050001 评分要点作答前不可见守卫（F5.2 / §5.5，M5 落实）
# ---------------------------------------------------------------------------
def rubric_reveal_allowed(*, answered: bool, self_scored: bool) -> bool:
    """评分要点（rubric）是否可揭示：学习者完成产出(answered)且自评(self_scored)后才可见。

    违反此不变量（提前揭示）→ A050001 语境下的顺序违规（系统缺陷，应由交互层保证）。
    """
    return bool(answered) and bool(self_scored)


def filter_judgment_data(
    data: dict,
    *,
    answered: bool,
    self_scored: bool,
) -> dict:
    """按 A050001 守卫裁剪判定屏数据：未达成「产出+自评」前剔除 rubric 键。

    - 达标（answered 且 self_scored）→ 原样返回（含 rubric）。
    - 未达标 → 返回剔除 "rubric" 键的副本，确保评分要点不提前流向界面。
    """
    if rubric_reveal_allowed(answered=answered, self_scored=self_scored):
        return dict(data)
    return {k: v for k, v in data.items() if k != "rubric"}
