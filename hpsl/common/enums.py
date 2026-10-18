"""共享枚举：Role / AttemptKind / UnitState / GapLevel（按规格 §9.3 / 锁定基线 §7）。

M0 仅占位引用，不强制业务校验；枚举值与基线 DDL 注释、角色清单逐字对齐。
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """对话/写入角色（对应 turn.role 与 llm_call.role）。"""

    USER = "user"
    ANSWER_MACHINE = "answer_machine"
    QUESTIONER = "questioner"
    BOUNDARY_SETTER = "boundary_setter"
    CRITIC = "critic"
    CLERK = "clerk"
    TOPIC_PICKER = "topic_picker"


class AttemptKind(str, Enum):
    """尝试类型（对应 attempt.kind）。"""

    RESTATE = "重述"
    BOUNDARY = "边界"
    PROBE = "追问"
    RETEST = "复测"


class UnitState(str, Enum):
    """单元状态机（对应 unit.status）。"""

    INIT = "INIT"
    PICK = "PICK"
    LEARN = "LEARN"
    PRODUCE = "PRODUCE"
    VERIFY = "VERIFY"
    JUDGE = "JUDGE"
    INTEGRATE = "INTEGRATE"
    CLOSED = "CLOSED"


class GapLevel(int, Enum):
    """缺口严重级别（对应 gap.level）。"""

    L1 = 1
    L2 = 2
    L3 = 3


# audit_log.actor 允许取值（写入口强制校验）
ACTORS = ("user", "clerk", "system")
