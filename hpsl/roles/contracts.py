"""六角色契约（Shared Kernel，BC-3 / C-04）。

定义每个 LLM 角色的**输入白名单** allow 元组（固定顺序）。assembler 仅按 allow
组装角色输入；任何角色读取 allow 之外的字段即视为契约越界（A030001）。

隔离核心卖点（基线 O13）：answer_machine_output 不在任何"消费角色"的 allow 中——
出题者 / 提问者 / 挑错者 / 选题 / 书记员 永不见答案机输出，避免同源污染与确认偏误。

纪律（冻结）：
- allow 必须为**有序元组**（非 set/list），以保证组装顺序稳定与可断言。
- 本模块为纯常数，被 assembler 与（未来）判分域共享（C-04 Shared Kernel），
  不得引入除 Role 枚举之外的运行时依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from ..common.enums import Role

# ---------------------------------------------------------------------------
# 规范上下文字段（角色可从中被授权读取的"数据湖"键名）
# ---------------------------------------------------------------------------
CTX_UNIT_TITLE = "unit_title"
CTX_DIFFICULTY = "difficulty_band"
CTX_CONCEPT_SUMMARY = "concept_summary"
CTX_TOPIC = "topic"
CTX_PRIOR_QA = "prior_qa"
CTX_RESTATEMENT = "learner_restatement"
CTX_RUBRIC = "rubric"
CTX_PRODUCTION = "learner_production"
CTX_ANSWER = "answer_machine_output"          # 隔离保护对象：仅答案机自身产出，任何消费角色不得读取
CTX_TRANSCRIPT = "conversation_transcript"
CTX_QUESTION = "question"                     # 仅答案机接收的问题


@dataclass(frozen=True)
class RoleContract:
    """单个角色的契约：固定顺序白名单 allow + 描述。"""

    role: Role
    allow: Tuple[str, ...]        # 固定顺序元组（非 set/list）
    description: str


CONTRACTS: dict[Role, RoleContract] = {
    Role.TOPIC_PICKER: RoleContract(
        Role.TOPIC_PICKER,
        (CTX_UNIT_TITLE, CTX_DIFFICULTY, CTX_CONCEPT_SUMMARY),
        "选题：基于单元标题 / 难度带 / 概念摘要给出候选主题",
    ),
    Role.ANSWER_MACHINE: RoleContract(
        Role.ANSWER_MACHINE,
        (CTX_UNIT_TITLE, CTX_TOPIC, CTX_QUESTION),
        "答案机：按需回答一级缺口，仅接收学习者的问题",
    ),
    Role.QUESTIONER: RoleContract(
        Role.QUESTIONER,
        (CTX_TOPIC, CTX_RESTATEMENT, CTX_PRIOR_QA),
        "提问者：基于学习者重述递进追问，禁止给答案（不见答案机输出）",
    ),
    Role.BOUNDARY_SETTER: RoleContract(
        Role.BOUNDARY_SETTER,
        (CTX_TOPIC, CTX_RESTATEMENT, CTX_RUBRIC),
        "出题者：基于重述与评分要点命制边界题（不见答案机输出）",
    ),
    Role.CRITIC: RoleContract(
        Role.CRITIC,
        (CTX_TOPIC, CTX_PRODUCTION, CTX_CONCEPT_SUMMARY),
        "挑错者：独立评判学习者产出（不见答案机输出，避免同源偏误）",
    ),
    Role.CLERK: RoleContract(
        Role.CLERK,
        (CTX_TRANSCRIPT, CTX_UNIT_TITLE),
        "书记员：从对话转录抽取候选缺口",
    ),
}


def _invariant_answer_isolated() -> bool:
    """隔离不变量（契约层哨兵，AC-M3-4）：answer_machine_output 不在任何角色 allow 中。"""
    return all(CTX_ANSWER not in c.allow for c in CONTRACTS.values())


# 模块导入即校验不变量（硬失败：阻断加载，属基线 O13 核心卖点守护）
assert _invariant_answer_isolated(), "隔离不变量被破坏：answer_machine_output 泄漏进某角色 allow"
