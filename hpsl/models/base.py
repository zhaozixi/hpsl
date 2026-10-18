"""Provider 适配防腐层（BC-7）基础类型。

定义：
- Msg / Reply：跨 Provider 的统一消息与响应数据类。
- ProviderAdapter：双 Provider（OpenAI 兼容 / Anthropic）的统一协议。
- _parse_structured：结构化响应解析助手（失败统一抛 A040004）。

纪律（冻结）：本模块不得被 common 反向依赖；不引入 ORM；仅依赖 common.errors。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from ..common.errors import BizError


@dataclass(frozen=True)
class Msg:
    """单条对话消息。role ∈ {system, user, assistant}。"""

    role: str
    content: str


@dataclass(frozen=True)
class Reply:
    """Provider 统一响应。

    - text：模型输出文本（结构化模式下为 JSON 字符串或已抽取内容）。
    - finish_reason：stop / length / error。
    - model：实际命中的模型 ID。
    - raw：原始响应体（防御性保留，不得含密钥；严禁入日志/VOC）。
    """

    text: str
    finish_reason: str
    model: str
    raw: Optional[dict] = None


@runtime_checkable
class ProviderAdapter(Protocol):
    """双 Provider 适配器协议（防腐层对外唯一契约）。"""

    name: str

    def complete(
        self,
        messages: list[Msg],
        *,
        json_schema: Optional[dict] = None,
        stream: bool = False,
    ) -> Reply:
        """完成一次对话补全。

        - messages：已组装好的消息序列（角色接线层已施加隔离）。
        - json_schema：给定则用结构化解析（supports_json 时走原生 response_format；
          否则走 tool-use 等价路径）。解析失败统一抛 A040004。
        - stream：流式开关（M4 仅实现非流式；流式位置预留）。
        """
        ...


def _parse_structured(content: str, json_schema: Optional[dict]) -> dict:
    """将模型输出的 content 解析为结构化 dict。

    仅当 json_schema 给定才做 JSON 解析（结构化模式）；
    解析失败（非法 JSON / 非对象）→ A040004（模型响应结构化解析失败）。

    返回解析后的 dict（可能为空 dict 表示无 schema 要求）。
    """
    if json_schema is None:
        return {}
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BizError("A040004", msg=f"结构化解析失败：{exc}") from exc
    if not isinstance(parsed, dict):
        raise BizError("A040004", msg="结构化解析结果非对象")
    return parsed


def _jsonable(value: Any) -> Any:
    """尽力转为可 JSON 序列化结构（用于告警/断言，绝不序列化密钥）。"""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


# 缩写：结构化解析失败码常量（避免散落字符串）
ERR_STRUCTURED = "A040004"
ERR_PROVIDER_EXHAUSTED = "A040001"
ERR_KEY_MISSING = "A040003"
