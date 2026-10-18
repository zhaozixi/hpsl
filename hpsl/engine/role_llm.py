"""六角色 LLM 接线（F8 模型接入 + M3 C-04 隔离复用，headless）。

RoleLLM.complete(role, context)：
1. fields = assembler.assemble(role, context) —— **隔离强制**：仅 allow 白名单字段；
   answer_machine_output 等隔离对象绝不可见（契约层 assert + 组装层白名单双重保障）。
2. system_prompt = 角色提示词骨架（roles/prompts/{role}.md）+ 注入可见字段（绝不超范围）。
3. 学习者输入单独取自 context.learner_input（学习者自身陈述，非隔离外字段）。
4. 调 adapter.complete([system, user]) → Reply。

隔离纪律（O13）：本模块**必须**经由 assembler.assemble，绝不直取 context；
答案机输出不回流任何消费角色。测试须验证 answer_machine_output 即便在 context 中存在，
也绝不出现在发送给模型的 messages 中。

纪律（冻结）：仅依赖 roles.assembler + models.base；不引入 ORM；不依赖 routes/app。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..common.enums import Role
from ..models.base import Msg, Reply
from ..roles import assembler
from ..roles.assembler import assemble


class RoleLLM:
    """将六角色契约接线到 Provider 适配器（隔离边界 = assembler）。"""

    def __init__(
        self,
        adapter,
        *,
        assembler_mod=None,
        prompts_dir: Optional[str | Path] = None,
    ):
        self.adapter = adapter
        self._assembler = assembler_mod or assembler
        default_prompts = Path(__file__).parent.parent / "roles" / "prompts"
        self._prompts_dir = Path(prompts_dir) if prompts_dir else default_prompts

    def assemble_fields(self, role, context: dict) -> dict:
        """经契约白名单组装角色可见字段（隔离强制，供测试断言）。"""
        return self._assembler.assemble(role, context)

    def build_system_prompt(self, role, context: dict) -> str:
        """拼装 system 提示词：骨架 + 仅可见字段（绝不注入 allow 外字段）。"""
        role_val = role.value if isinstance(role, Role) else str(role)
        tmpl = self._prompts_dir / f"{role_val}.md"
        header = tmpl.read_text(encoding="utf-8") if tmpl.exists() else f"# {role_val} 角色"
        fields = self.assemble_fields(role, context)
        ctx_lines = [f"- {k}：{v}" for k, v in fields.items()]
        ctx_block = "\n".join(ctx_lines) if ctx_lines else "（无可见上下文）"
        return f"{header}\n\n## 可见上下文（经契约白名单组装，绝不超范围）\n{ctx_block}"

    def build_messages(self, role, context: dict) -> list[Msg]:
        """构造发送给适配器的消息序列（隔离已在前置 assemble 保证）。"""
        system_prompt = self.build_system_prompt(role, context)
        # 学习者自身输入单独取，不混入隔离外字段
        user_text = context.get("learner_input") or context.get("learner_production") or ""
        return [Msg("system", system_prompt), Msg("user", user_text)]

    def complete(self, role, context: dict, *, stream: bool = False) -> Reply:
        messages = self.build_messages(role, context)
        return self.adapter.complete(messages, stream=stream)
