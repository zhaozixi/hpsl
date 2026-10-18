"""契约守护测试（基线冻结项 #9 的第七类：test_contracts）。

与 `test_role_isolation.py` 的分工：
- `test_role_isolation.py` 验证**运行时行为**（组装、预览、越权、无副作用、headless）；
- 本文件验证**契约常数本身**（六角色 allow 的固定顺序与内容、隔离不变量、
  clerk 的共享上下文唯一性、越权错误码、以及导入期 assert 真实存在且能阻断）。

补这一类的起因：冻结决策 #9 承诺七类测试守护，实测 `tests/` 缺 `test_contracts`
（REF-2 P1-07 / REF-3-C3）。断言面向"契约冻结值"——改动 allow 必须同步改本文件，
从而在 CI 层面强制"改契约 = 显式决策"。
"""

import pathlib

import pytest

from hpsl.common.enums import Role
from hpsl.common.errors import BizError
from hpsl.roles import contracts as contracts_mod
from hpsl.roles.assembler import assemble
from hpsl.roles.contracts import (
    CONTRACTS,
    CTX_ANSWER,
    CTX_CONCEPT_SUMMARY,
    CTX_DIFFICULTY,
    CTX_PRODUCTION,
    CTX_PRIOR_QA,
    CTX_QUESTION,
    CTX_RESTATEMENT,
    CTX_RUBRIC,
    CTX_TOPIC,
    CTX_TRANSCRIPT,
    CTX_UNIT_TITLE,
    RoleContract,
    _invariant_answer_isolated,
)

SIX_ROLES = (
    Role.TOPIC_PICKER,
    Role.ANSWER_MACHINE,
    Role.QUESTIONER,
    Role.BOUNDARY_SETTER,
    Role.CRITIC,
    Role.CLERK,
)

# 冻结值：需求基线 C-04 / 冻结决策 #9。顺序即组装顺序，不可随意调整。
EXPECTED_ALLOW = {
    Role.TOPIC_PICKER: (CTX_UNIT_TITLE, CTX_DIFFICULTY, CTX_CONCEPT_SUMMARY),
    Role.ANSWER_MACHINE: (CTX_UNIT_TITLE, CTX_TOPIC, CTX_QUESTION),
    Role.QUESTIONER: (CTX_TOPIC, CTX_RESTATEMENT, CTX_PRIOR_QA),
    Role.BOUNDARY_SETTER: (CTX_TOPIC, CTX_RESTATEMENT, CTX_RUBRIC),
    Role.CRITIC: (CTX_TOPIC, CTX_PRODUCTION, CTX_CONCEPT_SUMMARY),
    Role.CLERK: (CTX_TRANSCRIPT, CTX_UNIT_TITLE),
}


# ---------------------------------------------------------------------------
# C-04-1：六角色齐全，且 allow 为**固定顺序元组**（逐角色逐位比对，非集合比较）
# ---------------------------------------------------------------------------
def test_six_roles_allow_fixed_order():
    assert set(CONTRACTS.keys()) == set(SIX_ROLES), "契约必须且只能覆盖六个 LLM 角色"
    for role in SIX_ROLES:
        allow = CONTRACTS[role].allow
        assert isinstance(allow, tuple), f"{role.value} 的 allow 必须是元组（顺序即语义）"
        assert allow == EXPECTED_ALLOW[role], (
            f"{role.value} 的 allow 被改动：期望 {EXPECTED_ALLOW[role]}，实际 {allow}；"
            "改契约须走需求基线变更，不允许顺手调整"
        )
        assert len(allow) == len(set(allow)), f"{role.value} 的 allow 含重复字段"


# ---------------------------------------------------------------------------
# C-04-2：answer_machine_output 不在任何角色的 allow 中（基线 O13 核心卖点）
# ---------------------------------------------------------------------------
def test_answer_output_never_in_any_allow():
    for role in SIX_ROLES:
        assert CTX_ANSWER not in CONTRACTS[role].allow, f"{role.value} 泄漏了 {CTX_ANSWER}"
    # 常量名本身不得出现在任何 allow 的字面量里（防改名绕过）
    for role in SIX_ROLES:
        assert "answer_machine_output" not in CONTRACTS[role].allow


# ---------------------------------------------------------------------------
# C-04-3：clerk 唯一允许共享上下文（conversation_transcript）
# ---------------------------------------------------------------------------
def test_clerk_only_role_with_shared_transcript():
    holders = [r for r in SIX_ROLES if CTX_TRANSCRIPT in CONTRACTS[r].allow]
    assert holders == [Role.CLERK], f"共享转录只应授权给书记员，实际：{[r.value for r in holders]}"
    # 反向确认：其余五角色一个都不许读转录
    for role in SIX_ROLES:
        if role is not Role.CLERK:
            assert CTX_TRANSCRIPT not in CONTRACTS[role].allow


# ---------------------------------------------------------------------------
# C-04-4：越权请求抛 A030001，且不修改 context
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", SIX_ROLES)
def test_overprivilege_raises_a030001(role):
    ctx = {CTX_ANSWER: "标准答案", CTX_TOPIC: "主题", CTX_TRANSCRIPT: "转录"}
    snapshot = dict(ctx)
    with pytest.raises(BizError) as exc:
        assemble(role, ctx, requested=[CTX_ANSWER])
    assert exc.value.code == "A030001"
    assert ctx == snapshot, "越权路径不得产生副作用"


def test_unknown_role_raises_a030001():
    with pytest.raises(BizError) as exc:
        assemble("no_such_role", {})
    assert exc.value.code == "A030001"


# ---------------------------------------------------------------------------
# C-04-5：导入期 assert 真实存在，且能阻断违规契约加载（哨兵不是装饰）
# ---------------------------------------------------------------------------
def test_import_time_assert_present():
    src = pathlib.Path(contracts_mod.__file__).read_text(encoding="utf-8")
    assert "assert _invariant_answer_isolated()" in src, "导入期隔离断言缺失"
    assert _invariant_answer_isolated() is True


def test_import_time_assert_blocks_leak():
    """把 clerk 的 allow 改成含答案机输出，然后**重新执行模块源码**，必须 AssertionError。

    用 exec 而非 reload：reload 会先重定义 CONTRACTS 再断言，永远看不到污染；
    exec 则忠实模拟"导入一个坏契约模块"的场景。
    """
    src = pathlib.Path(contracts_mod.__file__).read_text(encoding="utf-8")
    poisoned = src.replace(
        "(CTX_TRANSCRIPT, CTX_UNIT_TITLE),",
        "(CTX_ANSWER, CTX_UNIT_TITLE),",
        1,
    )
    assert poisoned != src, "测试自身失效：未能在源码中定位 clerk 的 allow 字面量"

    import sys
    import types

    name = "hpsl.roles.contracts_poisoned"
    module = types.ModuleType(name)
    module.__package__ = "hpsl.roles"
    sys.modules[name] = module
    try:
        with pytest.raises(AssertionError, match="隔离不变量"):
            exec(compile(poisoned, str(contracts_mod.__file__), "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# C-04-6：契约对象不可变（frozen dataclass），防止运行期改写 allow
# ---------------------------------------------------------------------------
def test_contract_is_frozen():
    from dataclasses import FrozenInstanceError

    contract = CONTRACTS[Role.CLERK]
    assert isinstance(contract, RoleContract)
    with pytest.raises(FrozenInstanceError):
        contract.allow = ()  # type: ignore[misc]
