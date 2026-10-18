"""M4 六角色 LLM 接线测试（AC-M4-7 + 隔离断言）。

核心：RoleLLM.complete 经 assembler.assemble 组装角色可见字段，answer_machine_output
等隔离对象即便存在于 context，也**绝不**出现在发送给模型的 messages 中（复用 M3 哨兵精神）。
全部用 fake adapter 记录 messages，不联网。
"""

import pytest

from hpsl.common.enums import Role
from hpsl.models.base import Msg, Reply
from hpsl.roles import assembler
from hpsl.roles.contracts import (
    CTX_ANSWER,
    CTX_CONCEPT_SUMMARY,
    CTX_PRODUCTION,
    CTX_RUBRIC,
    CTX_TOPIC,
)
from hpsl.engine.role_llm import RoleLLM


class _RecordingAdapter:
    """记录最近一次 complete 收到的 messages。"""

    name = "fake"

    def __init__(self):
        self.last_messages = None

    def complete(self, messages, *, json_schema=None, stream=False) -> Reply:
        self.last_messages = messages
        return Reply(text="ok", finish_reason="stop", model="fake")


def _full_context():
    return {
        CTX_TOPIC: "梦种的形成机制",
        CTX_PRODUCTION: "我的产出全文……",
        CTX_CONCEPT_SUMMARY: "概念账本摘要……",
        CTX_RUBRIC: "要点：边界、成因、反例",
        CTX_ANSWER: "【答案机输出】标准答案……",  # 隔离保护对象
        "unit_title": "唐代梦事",
        "difficulty_band": "hard",
        "prior_qa": [],
        "question": "什么是梦种？",
        "learner_input": "我对梦种的理解是……",
    }


# AC-M4-7：各角色发送的 messages 仅含 allow 字段，answer_machine_output 绝不出现
@pytest.mark.parametrize(
    "role",
    [Role.TOPIC_PICKER, Role.ANSWER_MACHINE, Role.QUESTIONER, Role.BOUNDARY_SETTER, Role.CRITIC, Role.CLERK],
)
def test_role_llm_messages_exclude_answer_machine_output(role):
    adapter = _RecordingAdapter()
    rllm = RoleLLM(adapter)
    ctx = _full_context()
    rllm.complete(role, ctx)
    assert adapter.last_messages is not None
    blob = " ".join(m.content for m in adapter.last_messages)
    # 隔离校验：答案机输出的「实际内容」绝不可出现在任何角色的消息中
    # （答案机自身提示骨架提及字段名 answer_machine_output 属正常文档，不计入泄漏）
    assert "【答案机输出】" not in blob, f"{role.value} 泄漏了答案机输出内容"
    assert CTX_ANSWER not in adapter.last_messages[1].content, f"{role.value} 用户消息混入隔离字段"


def test_role_llm_injects_only_allowed_fields_for_critic():
    adapter = _RecordingAdapter()
    rllm = RoleLLM(adapter)
    ctx = _full_context()
    rllm.complete(Role.CRITIC, ctx)
    system = adapter.last_messages[0].content
    # CRITIC allow: topic / learner_production / concept_summary
    assert CTX_TOPIC in system
    assert CTX_PRODUCTION in system
    assert CTX_CONCEPT_SUMMARY in system
    # CRITIC allow 不含 rubric → rubric 与答案机输出内容均不得出现
    assert CTX_RUBRIC not in system
    assert "【答案机输出】" not in system


def test_role_llm_reads_prompt_template():
    adapter = _RecordingAdapter()
    rllm = RoleLLM(adapter)
    rllm.complete(Role.TOPIC_PICKER, _full_context())
    system = adapter.last_messages[0].content
    assert "选题角色" in system, "应拼装 topic_picker.md 提示词骨架"


def test_role_llm_assemble_uses_contract_whitelist():
    # 即便 context 含隔离外字段，assemble 仍只返回 allow 字段（按 BOUNDARY_SETTER 契约：
    # topic / learner_restatement / rubric，且 context 未提供 restatement → 仅 topic 命中）
    rllm = RoleLLM(_RecordingAdapter())
    fields = rllm.assemble_fields(Role.BOUNDARY_SETTER, _full_context())
    assert CTX_ANSWER not in fields
    assert set(fields.keys()) <= {CTX_TOPIC, "learner_restatement", CTX_RUBRIC}
    assert CTX_TOPIC in fields
