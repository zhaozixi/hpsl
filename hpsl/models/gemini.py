"""Gemini 适配器占位（O7：本期不实现，BC-7 防腐层预留）。

M4 仅落实 OpenAI 兼容（E-01）与 Anthropic（E-02）双 Provider 适配；
Gemini（v1）属后续里程碑，此处仅占位，调用即抛 NotImplementedError，
保证 Provider 工厂在未知 provider 时行为明确。
"""

from __future__ import annotations

from ..common.errors import BizError
from .base import Msg, ProviderAdapter, Reply


class GeminiAdapter:
    """Gemini 适配占位（O7）。"""

    name = "gemini"

    def __init__(self, *args, **kwargs):
        self.model_id = kwargs.get("model_id", "gemini-placeholder")

    def complete(
        self,
        messages: list[Msg],
        *,
        json_schema=None,
        stream: bool = False,
    ) -> Reply:
        raise BizError(
            "A040002",
            msg="Gemini 适配（O7）本期未实现，请切换至已支持的 Provider 或配置备选模型",
        )
