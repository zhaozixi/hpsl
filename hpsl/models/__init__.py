"""模型适配层（BC-7，ACL 防腐层实现）。

对外暴露：
- base：Msg / Reply / ProviderAdapter 协议 / _parse_structured。
- openai_compat / anthropic / gemini：三类接入协议（kind）对应的适配器（E-01 / E-02 / O7 占位）。
- registry：模型注册表、异构判定 heterogeneous、适配器工厂 build_adapter。

依赖方向：models -> common（仅 errors）；不得被 common 反向依赖。
"""

from .base import Msg, ProviderAdapter, Reply, _parse_structured
from .anthropic import AnthropicAdapter
from .gemini import GeminiAdapter
from .openai_compat import OpenAICompatAdapter
from .registry import (
    ModelConfig,
    REGISTRY,
    build_adapter,
    default_base_url,
    get_model,
    heterogeneous,
    list_models,
    pricing,
)

__all__ = [
    "Msg",
    "Reply",
    "ProviderAdapter",
    "_parse_structured",
    "OpenAICompatAdapter",
    "AnthropicAdapter",
    "GeminiAdapter",
    "ModelConfig",
    "REGISTRY",
    "get_model",
    "list_models",
    "heterogeneous",
    "default_base_url",
    "pricing",
    "build_adapter",
]
