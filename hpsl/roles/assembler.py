"""白名单组装器（BC-3）。

assemble(role, context, *, requested=None)：
- 仅返回 CONTRACTS[role].allow 与 context 交集内的字段，按 allow 固定顺序。
- 若 requested 给定且其中任一字段不在 allow → 越权，抛 A030001（契约越界）。
- 纯函数，不修改 context；业务代码不直写 audit_log（本模块无写操作）。

preview(role, context)：
- 返回角色可见内容的稳定预览（F3.3 输入预览，默认收起由 M10 控制），
  不泄露 allow 之外的字段。

纪律（冻结）：不引入 ORM；绝不依赖 models/ / routes / app（保持 headless）。
"""

from __future__ import annotations

from typing import Optional

from ..common.enums import Role
from ..common.errors import BizError
from .contracts import CONTRACTS, RoleContract


def _contract(role) -> RoleContract:
    """接受 Role 枚举或字符串（"clerk" / "CRITIC" 均可），归一为 Role；未知 → A030001。"""
    if isinstance(role, Role):
        r = role
    elif isinstance(role, str):
        try:
            r = Role[role]
        except KeyError:
            try:
                r = Role(role)
            except ValueError:
                r = None
    else:
        r = None
    if r is None or r not in CONTRACTS:
        raise BizError("A030001", msg=f"未知角色契约：{role!r}")
    return CONTRACTS[r]


def assemble(role, context: dict, *, requested=None) -> dict:
    """按角色契约白名单组装输入。

    参数：
    - role：角色（Role 或字符串）。
    - context：可用数据湖（dict）。
    - requested：显式请求的字段列表（可选）。任一不在 allow → A030001（越权），无副作用。
                 不给定则返回 allow ∩ context（按 allow 顺序）。

    返回：组装后的有序 dict（仅含 allow 内且 context 中存在的字段）。

    异常：A030001（越权 / 未知角色），无副作用（不修改 context）。
    """
    contract = _contract(role)

    if requested is not None:
        bad = [f for f in requested if f not in contract.allow]
        if bad:
            raise BizError(
                "A030001",
                msg=f"角色 {contract.role.value} 越权读取未授权字段：{bad}",
            )
        fields = tuple(requested)
    else:
        fields = contract.allow

    return {f: context[f] for f in fields if f in context}


def preview(role, context: dict) -> dict:
    """角色可见内容预览（F3.3 输入预览）。

    返回稳定结构：role / description / allowed_fields / visible_fields / visible_values。
    visible_fields 必为 allowed_fields 子集，绝不含隔离外字段。
    """
    contract = _contract(role)
    visible = assemble(role, context)
    return {
        "role": contract.role.value,
        "description": contract.description,
        "allowed_fields": list(contract.allow),
        "visible_fields": list(visible.keys()),
        "visible_values": visible,
    }
