"""Anthropic 适配器（E-02：Claude 系列，BC-7 防腐层）。

- httpx 直连 `POST {base_url}/messages`（Messages API）。
- supports_json=False：结构化退化为 tool-use 等价路径——
  以单一工具 `return_structured`（input_schema=json_schema）+ tool_choice 强制调用，
  解析 tool_use 的 input 作为结构化结果；解析失败 → A040004。
- 网络错误 / 5xx / 429：指数退避重试（默认 1s / 2s，最多 max_retries）；
  重试逐次记账（on_call）；耗尽 → A040001。
- 安全：x-api-key 由 key_resolver() 提供；密钥绝不进入请求体 / 日志 / VOC。
- 测试性：key_resolver 与底层 _post 均可注入；backoff 可注入为 0。

纪律（冻结）：仅依赖 models.base / common.errors；不引入 ORM。
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

import httpx

from ..common.errors import BizError
from .base import ERR_KEY_MISSING, ERR_PROVIDER_EXHAUSTED, ERR_STRUCTURED, Msg, ProviderAdapter, Reply, _parse_structured


def _default_key_resolver(provider: str) -> Callable[[], str]:
    def _resolve() -> str:
        from ..secrets import keystore

        return keystore.get_api_key(provider)

    return _resolve


class AnthropicAdapter:
    """Anthropic Messages API 适配器（防腐层实现，结构化走 tool-use 等价）。"""

    def __init__(
        self,
        name: str,
        base_url: str,
        model_id: str,
        *,
        key_resolver: Optional[Callable[[], str]] = None,
        provider: Optional[str] = None,
        max_tokens: int = 2048,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff: Optional[Callable[[int], float]] = None,
        on_call: Optional[Callable[[dict], None]] = None,
        _post: Optional[Callable[..., "httpx.Response"]] = None,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self._provider = provider or name
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self._backoff = backoff if backoff is not None else (lambda n: 2 ** n)
        self._on_call = on_call
        self._key_resolver = key_resolver or _default_key_resolver(self._provider)
        self._post = _post or (lambda *args, **kw: httpx.post(*args, **kw))

    def _build_headers(self) -> dict:
        key = self._key_resolver()
        if not key:
            raise BizError(ERR_KEY_MISSING, msg=f"Provider {self._provider} 密钥缺失或无效")
        return {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _build_body(self, messages: list[Msg], *, json_schema: Optional[dict]) -> dict:
        system_parts = [m.content for m in messages if m.role == "system"]
        conv = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        body: dict = {
            "model": self.model_id,
            "max_tokens": self.max_tokens,
            "messages": conv,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if json_schema is not None:
            body["tools"] = [
                {
                    "name": "return_structured",
                    "description": "以结构化对象返回结果",
                    "input_schema": json_schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": "return_structured"}
        return body

    def _extract(self, resp_json: dict, *, json_schema: Optional[dict]) -> tuple[str, str]:
        try:
            content_blocks = resp_json["content"]
        except (KeyError, TypeError) as exc:
            raise BizError(ERR_STRUCTURED, msg=f"响应结构异常：{exc}") from exc
        texts: list[str] = []
        structured: Optional[dict] = None
        for block in content_blocks:
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "tool_use" and block.get("name") == "return_structured":
                structured = block.get("input")
        if json_schema is not None:
            # 结构化模式必须从 tool_use 抽到对象
            if not isinstance(structured, dict):
                raise BizError(ERR_STRUCTURED, msg="未从 tool_use 解析到结构化对象")
            return json.dumps(structured, ensure_ascii=False), "stop"
        return "\n".join(texts), "stop"

    def complete(
        self,
        messages: list[Msg],
        *,
        json_schema: Optional[dict] = None,
        stream: bool = False,
    ) -> Reply:
        if stream:
            pass  # M4 仅非流式；流式钩子预留
        url = f"{self.base_url}/messages"
        headers = self._build_headers()
        body = self._build_body(messages, json_schema=json_schema)
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            if self._on_call is not None:
                self._on_call(
                    {
                        "provider": self._provider,
                        "model": self.model_id,
                        "attempt": attempt,
                        "json": bool(json_schema is not None),
                    }
                )
            try:
                resp = self._post(url, headers=headers, json=body, timeout=self.timeout)
                status = getattr(resp, "status_code", 200)
                if status >= 500 or status == 429:
                    last_exc = RuntimeError(f"上游返回 {status}")
                    if attempt < self.max_retries:
                        time.sleep(self._backoff(attempt))
                        continue
                    raise BizError(
                        ERR_PROVIDER_EXHAUSTED,
                        msg=f"Provider {self._provider} 调用失败（重试耗尽，末次 {status}）",
                    )
                data = resp.json()
                content, finish = self._extract(data, json_schema=json_schema)
            except (httpx.HTTPError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self._backoff(attempt))
                    continue
                raise BizError(
                    ERR_PROVIDER_EXHAUSTED,
                    msg=f"Provider {self._provider} 调用失败（重试耗尽）：{exc}",
                ) from exc

            if json_schema is not None:
                # 二次校验：结构化结果可解析为对象
                _parse_structured(content, json_schema)
            return Reply(text=content, finish_reason=finish, model=self.model_id, raw=data)

        raise BizError(
            ERR_PROVIDER_EXHAUSTED,
            msg=f"Provider {self._provider} 调用失败（重试耗尽）：{last_exc}",
        )
