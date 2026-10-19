"""跨模块传输结构（DTO / VO），按规格 §9.3 与锁定基线 §3.2.M1.3。

- ProjectCreateRequest：创建项目入参（name/goal/initial_belief）。
- ProjectVO：项目视图对象（name 由 project.config 还原，见 projects.py 说明）。
- ConfigVO：全局默认 + 项目覆盖合并后的配置值（阈值集合）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from .enums import ACTORS


@dataclass
class ProjectCreateRequest:
    name: str
    goal: str
    initial_belief: Optional[str] = None

    def validate(self) -> None:
        """参数校验（对应规格 §9.2 Step1 / 基线错误码 C400001）。"""
        if not isinstance(self.name, str) or not (1 <= len(self.name) <= 100):
            raise ValueError("name 必须为 1~100 字符的非空字符串")
        if not isinstance(self.goal, str) or not (1 <= len(self.goal) <= 500):
            raise ValueError("goal 必须为 1~500 字符的非空字符串")
        if self.initial_belief is not None:
            if not isinstance(self.initial_belief, str) or len(self.initial_belief) > 2000:
                raise ValueError("initial_belief 必须为 ≤2000 字符的字符串")


@dataclass
class ProjectVO:
    id: str
    name: str
    goal: str
    initial_belief: Optional[str]
    db_path: str  # 展示用，不暴露目录遍历（safe_path 约束）
    created_at: str  # ISO-8601 UTC

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConfigVO:
    """项目级配置（阈值集合），默认值逐字对齐规格 §9.3。"""

    pass_threshold: float = 0.7
    pass_hi: float = 0.80
    pass_lo: float = 0.60
    l1_gap_limit: int = 5
    session_minutes: int = 25
    zero_gap_max: float = 0.20
    forced_close_max: float = 0.10
    skipped_produce_max: float = 0.0
    review_intervals: tuple = (2, 7, 21)
    budget_cap_usd: float = 10.0
    # M38：费用展示用的人民币汇率（1 USD = ? CNY）。只影响界面折算，不改记账口径。
    usd_cny_rate: float = 7.2
    extra: dict = field(default_factory=dict)

    # ConfigVO 已知字段（用于从 project.config JSON 合并时只读取这些键）
    KNOWN_FIELDS = (
        "pass_threshold", "pass_hi", "pass_lo", "l1_gap_limit", "session_minutes",
        "zero_gap_max", "forced_close_max", "skipped_produce_max",
        "review_intervals", "budget_cap_usd", "usd_cny_rate", "extra",
    )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ConfigVO":
        """从 project.config JSON 合并（仅读取 KNOWN_FIELDS，忽略 name 等非配置键）。"""
        data = data or {}
        kwargs: dict = {}
        for key in cls.KNOWN_FIELDS:
            if key in data:
                kwargs[key] = data[key]
        # 阈值元组经 JSON 往返会变成 list，这里归一化回 tuple（规格 §9.3 review_intervals=(2,7,21)）
        if isinstance(kwargs.get("review_intervals"), list):
            kwargs["review_intervals"] = tuple(kwargs["review_intervals"])
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# M7 四本账与审计 DTO / VO（§3.2.M7.3）
# ---------------------------------------------------------------------------
@dataclass
class AuditEntryVO:
    """审计条目视图对象（撤销 / 账本总览消费）。"""

    id: int
    ts: str
    actor: str
    table_name: str
    row_id: str
    change: dict
    prev: Optional[dict]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GapConfirmRequest:
    """候选缺口批量确认入参项（§3.2.M7.3）。"""

    candidate_id: str
    action: str                      # confirm（入账）/ discard（废弃）
    level: Optional[int] = None      # confirm 时等级 ∈ {1, 2, 3}

    def to_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "action": self.action, "level": self.level}


@dataclass
class UndoRequest:
    """撤销入参（§3.2.M7.3）。"""

    audit_id: int
    # 旧值指纹：当前行或 prev 行任一指纹匹配即接受（防止对已变更行误回滚）。
    expected_prev_hash: Optional[str] = None

    def to_dict(self) -> dict:
        return {"audit_id": self.audit_id, "expected_prev_hash": self.expected_prev_hash}


@dataclass
class CardCreateRequest:
    """建卡片入参（§3.2.M7.3；实际写入经 db.write，M10 消费）。"""

    concept_id: str
    my_words: str                   # 学习者自己的话；≤ 500 字符
    links: Optional[dict] = None     # 关联标注 JSON

    def validate(self) -> None:
        if not isinstance(self.concept_id, str) or not self.concept_id:
            raise ValueError("concept_id 必填")
        if not isinstance(self.my_words, str) or not (1 <= len(self.my_words) <= 500):
            raise ValueError("my_words 必须为 1~500 字符的非空字符串")

    def to_dict(self) -> dict:
        return {"concept_id": self.concept_id, "my_words": self.my_words, "links": self.links}


@dataclass
class GapListVO:
    """缺口账本条目视图对象（list_gaps 返回）。"""

    id: str
    unit_id: Optional[str]
    session_id: Optional[str]
    level: int
    text: str
    status: str
    context: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# M8 成本估算与预算 DTO / VO（§3.2.M8.3）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CostEstimateVO:
    """会话花费估算视图对象（session_cost / record_call 返回，§3.2.M8.3）。"""

    session_id: str
    tokens_in: int              # 会话输入 tokens 合计
    tokens_out: int             # 会话输出 tokens 合计
    cost_usd: float             # 会话花费（按 pricing.yaml 计算）
    cumulative_usd: float       # 当月累计（跨会话）
    budget_cap_usd: float       # 预算上限（项目级配置）
    status: str                 # ok / warn / blocked
    confirmations: int          # 本会话超限确认次数
    computed_at: str            # 估算时间（ISO-8601 UTC）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CostConfirmRequest:
    """超限确认入参（REST / M10 消费；acknowledged 恒 true 防误触，§3.2.M8.3）。"""

    acknowledged: bool = True

    def to_dict(self) -> dict:
        return {"acknowledged": self.acknowledged}


# ---------------------------------------------------------------------------
# M9 治理度量 DTO / VO（§3.2.M9.3）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CountsVO:
    """治理度量计数视图对象（compute_counts 返回，§3.2.M9.3）。

    指标是账本读时重算的视图（非账本），13 项四平面计数：
    - 强行闭环单元不计入 pass_rate（污染升降判断）；
    - zero_gap_ratio 分母只算 gaps_confirmed=1 会话（防不作为操纵）；
    - 分母为 0 的指标返回 0.0（「数据积累中」由 M10 展示层标注）。
    """

    # ===== 质量平面 =====
    pass_rate: float               # 重述 + 边界题通过率（强行闭环单元不计入；复测加权更高）
    review_pass_rate: float        # 延迟复测通过率（仅 attempt.kind=复测）
    calibration_gap: float         # 自评与确认得分平均差；正值 = 高估自己
    # ===== 节奏平面 =====
    l1_gap_sessions: int           # 一级缺口超上限（默认 5）的会话数
    zero_gap_ratio: float          # 零缺口会话占比（分母只算已确认会话）
    # ===== 进度平面 =====
    unit_completion: float         # 单元完成率（只计已闭环且非强行闭环）
    gap_clear_ratio: float         # 缺口清账率（已补 + 已验证 / 总缺口）
    l2_gap_backlog: int            # 二级缺口积压量（level=2 且 status=待补）
    # ===== 风险平面 =====
    forced_close_ratio: float      # 强行闭环单元占比（上限 0.10）
    skipped_produce_ratio: float   # 跳过产出单元占比（上限 0）
    unresolved_disagreements: int  # 未裁决实质分歧数（claim_check，已排除同源降级告警）
    isolated_node_ratio: float     # 孤岛概念节点占比（无出入边的概念）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SoftPromptVO:
    """治理软提示视图对象（plane_signals 返回，§3.2.M9.3）。

    日常语言、可一键处理（继续 / 补前置 / 降级）、可关闭；v0 仅输出，
    升降难度建议（suggest_band）留 v1（O3）。
    """

    id: str                        # 稳定标识（按 平面-信号类型 生成，幂等关闭用）
    plane: str                     # 风险 / 质量 / 节奏 / 进度
    level: str                     # info / warn
    message: str                   # 人读文案（不暴露内部术语）
    dismissible: bool = True       # 是否可一键关闭

    def to_dict(self) -> dict:
        return asdict(self)
