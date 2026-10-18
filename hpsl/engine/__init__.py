"""流程 / 复测 / 判分 / 角色接线引擎包（M2–M4 新增，headless，不依赖 routes/app）。

导出：
- state：TRANSITIONS / move / UnitStateVO（单元状态机，M2）
- review_queue：REVIEW_INTERVALS / enqueue_reviews / due_reviews / submit_review /
  submit_review_by_id / start_review / ReviewItemVO / ReviewResultVO / ReviewSubmitRequest /
  ReviewStartVO（最小延迟复测队列，M2；start_review + submit_review_by_id 复测卷幂等，M6）
- ledger：confirm_gap_candidates / list_gaps / ledger_summary（四本账与审计编排层，M7）
- grading：CloseDecisionVO / has_unresolved_keypoint / evaluate_close / close_unit /
  rubric_reveal_allowed / filter_judgment_data（判分协议 + JUDGE 闭环门控，M4；BD-03 同源降级 + A050001 守卫，M5）
- claim_check：TriggerRule / evaluate_triggers / should_trigger_critic / same_source /
  write_claim_check / record_same_source_warning / arbitrate / trigger_critic_if_needed /
  mark_same_source_confirmed / count_same_source_keypoint_auto_close（F7 挑错与异构，M5）
- governance：compute_counts / counts_context / plane_signals（四平面治理度量，M9；只读零 DDL）
- role_llm：RoleLLM（六角色 LLM 接线，复用 M3 C-04 隔离，M4）

依赖方向：engine -> common, db, roles（仅契约）, models（防腐层）；禁止反向；
不依赖 routes/app，保持 headless 便于 M10 接入与测试。
"""

from .state import TRANSITIONS, move, UnitStateVO
from .review_queue import (
    REVIEW_INTERVALS,
    enqueue_reviews,
    due_reviews,
    submit_review,
    submit_review_by_id,
    start_review,
    ReviewItemVO,
    ReviewResultVO,
    ReviewSubmitRequest,
    ReviewStartVO,
)
from .ledger import (
    confirm_gap_candidates,
    list_gaps,
    ledger_summary,
)
from .cost import (
    record_call,
    session_cost,
    budget_status,
    confirm_continue,
)
from .grading import (
    CloseDecisionVO,
    close_unit,
    evaluate_close,
    has_unresolved_keypoint,
    rubric_reveal_allowed,
    filter_judgment_data,
)
from .claim_check import (
    TriggerRule,
    evaluate_triggers,
    should_trigger_critic,
    same_source,
    write_claim_check,
    record_same_source_warning,
    arbitrate,
    trigger_critic_if_needed,
    mark_same_source_confirmed,
    count_same_source_keypoint_auto_close,
)
from .governance import compute_counts, counts_context, plane_signals
from .role_llm import RoleLLM

__all__ = [
    "TRANSITIONS",
    "move",
    "UnitStateVO",
    "REVIEW_INTERVALS",
    "enqueue_reviews",
    "due_reviews",
    "submit_review",
    "submit_review_by_id",
    "start_review",
    "ReviewItemVO",
    "ReviewResultVO",
    "ReviewSubmitRequest",
    "ReviewStartVO",
    "confirm_gap_candidates",
    "list_gaps",
    "ledger_summary",
    "record_call",
    "session_cost",
    "budget_status",
    "confirm_continue",
    "CloseDecisionVO",
    "has_unresolved_keypoint",
    "evaluate_close",
    "close_unit",
    "rubric_reveal_allowed",
    "filter_judgment_data",
    "TriggerRule",
    "evaluate_triggers",
    "should_trigger_critic",
    "same_source",
    "write_claim_check",
    "record_same_source_warning",
    "arbitrate",
    "trigger_critic_if_needed",
    "mark_same_source_confirmed",
    "count_same_source_keypoint_auto_close",
    "compute_counts",
    "counts_context",
    "plane_signals",
    "RoleLLM",
]
