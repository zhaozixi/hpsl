"""角色隔离哨兵测试（AC-M3-1~7，关键模块）。

用临时 HPSL_HOME（conftest 已支持），不污染真实 ~/.hpsl。
覆盖：契约完整性 / 白名单组装 / 越权抛错 / 隔离不变量 / 输入预览 / 共享内核无越权依赖。
（注：DB/写入隔离纪律另见 tests/test_isolation.py，二者命名区分。）
"""

import ast

import pytest

from hpsl.common.enums import Role
from hpsl.common.errors import BizError
from hpsl.roles import assembler
from hpsl.roles.assembler import assemble, preview
from hpsl.roles.contracts import (
    CONTRACTS,
    CTX_ANSWER,
    CTX_CONCEPT_SUMMARY,
    CTX_PRODUCTION,
    CTX_RUBRIC,
    CTX_RESTATEMENT,
    CTX_TOPIC,
    CTX_TRANSCRIPT,
    _invariant_answer_isolated,
)


# 六角色（不含 USER 自然人）
SIX_ROLES = [
    Role.TOPIC_PICKER,
    Role.ANSWER_MACHINE,
    Role.QUESTIONER,
    Role.BOUNDARY_SETTER,
    Role.CRITIC,
    Role.CLERK,
]


# AC-M3-1：CONTRACTS 含全部六角色；allow 为元组（非 set/list），顺序固定
def test_contracts_have_six_roles():
    for r in SIX_ROLES:
        assert r in CONTRACTS, f"缺失契约：{r.value}"
    assert set(CONTRACTS.keys()) == set(SIX_ROLES)


def test_allow_is_ordered_tuple():
    for r in SIX_ROLES:
        allow = CONTRACTS[r].allow
        assert isinstance(allow, tuple), f"{r.value} 的 allow 必须为元组（保证顺序稳定）"
        # 无重复键
        assert len(allow) == len(set(allow)), f"{r.value} 的 allow 含重复字段"


# AC-M3-2：assemble 仅返回 allow∩context 且按 allow 顺序；answer_machine_output 绝不出现
def _full_context():
    return {
        CTX_TOPIC: "梦种的形成机制",
        CTX_RESTATEMENT: "我理解梦种是……",
        CTX_RUBRIC: "要点：边界、成因、反例",
        CTX_PRODUCTION: "我的产出全文……",
        CTX_CONCEPT_SUMMARY: "概念账本摘要……",
        CTX_TRANSCRIPT: "对话转录……",
        CTX_ANSWER: "【答案机输出】标准答案……",  # 隔离保护对象
        "unit_title": "唐代梦事",
        "difficulty_band": "hard",
        "prior_qa": [],
        "question": "什么是梦种？",
    }


def test_assemble_returns_only_allowed_in_fixed_order():
    ctx = _full_context()
    out = assemble(Role.BOUNDARY_SETTER, ctx)
    assert list(out.keys()) == [CTX_TOPIC, CTX_RESTATEMENT, CTX_RUBRIC]
    # context 中的答案机输出不得出现
    assert CTX_ANSWER not in out


def test_assemble_no_side_effect_on_context():
    ctx = _full_context()
    snapshot = dict(ctx)
    try:
        assemble(Role.CRITIC, ctx)
    except Exception:
        pass
    assert ctx == snapshot, "assemble 不得修改入参 context"


def test_assemble_respects_context_presence():
    # 缺失允许字段时该键不出现（宽松：仅缺失不报错）
    out = assemble(Role.QUESTIONER, {CTX_TOPIC: "t"})
    assert CTX_TOPIC in out
    assert CTX_RESTATEMENT not in out


# AC-M3-3：越权抛错 A030001，无副作用
def test_overprivilege_requested_rejected_a030001():
    ctx = _full_context()
    snapshot = dict(ctx)
    with pytest.raises(BizError) as exc:
        # BOUNDARY_SETTER allow 不含 answer_machine_output；显式请求即越权
        assemble(Role.BOUNDARY_SETTER, ctx, requested=[CTX_RUBRIC, CTX_ANSWER])
    assert exc.value.code == "A030001"
    assert ctx == snapshot, "越权路径不得修改 context"


def test_unknown_role_rejected_a030001():
    with pytest.raises(BizError) as exc:
        assemble("not_a_role", {})
    assert exc.value.code == "A030001"


def test_string_role_accepted():
    out = assemble("clerk", {CTX_TRANSCRIPT: "x", "unit_title": "u"})
    assert out == {CTX_TRANSCRIPT: "x", "unit_title": "u"}


# AC-M3-4：隔离不变量（契约层哨兵）—— answer_machine_output 不在任何角色 allow
def test_invariant_answer_isolated():
    assert _invariant_answer_isolated() is True
    for r in SIX_ROLES:
        assert CTX_ANSWER not in CONTRACTS[r].allow, f"{r.value} 不应包含 {CTX_ANSWER}"


def test_consumer_roles_exclude_answer_machine_output():
    # 显式断言各消费角色在含答案机输出的上下文中组装结果不含该键
    ctx = _full_context()
    for r in (Role.TOPIC_PICKER, Role.QUESTIONER, Role.BOUNDARY_SETTER, Role.CRITIC, Role.CLERK):
        assert CTX_ANSWER not in assemble(r, ctx), f"{r.value} 泄漏了答案机输出"


# AC-M3-5：preview 的 visible_fields ⊂ allowed_fields，不泄露隔离外字段
def test_preview_visible_subset_of_allowed():
    ctx = _full_context()
    pv = preview(Role.CRITIC, ctx)
    assert set(pv["visible_fields"]) <= set(pv["allowed_fields"])
    assert CTX_ANSWER not in pv["visible_fields"]
    assert pv["role"] == "critic"


# AC-M3-7：Shared Kernel——roles 不依赖 models/routes/app（AST 守护，headless）
def test_roles_package_no_forbidden_imports():
    import pathlib

    roles_dir = pathlib.Path(assembler.__file__).parent
    forbidden = {"models", "routes", "app", "hpsl.models", "hpsl.routes", "hpsl.app"}
    for py in roles_dir.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden, f"{py.name} 禁止 import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    assert root not in forbidden, f"{py.name} 禁止 from {node.module} import"
