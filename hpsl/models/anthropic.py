"""Anthropic 适配器（E-02：Claude 系列，BC-7 防腐层）。

- httpx 直连 `POST {base_url}/messages`（Messages API）。
- supports_json=False：结构化退化为 tool-use 等价路径——
  以单一工具 `return_structured`（input_schema=json_schema）+ tool_choice 强制调用，
  解析 tool_use 的 input 作为结构化结果；解析失败 → A040004。
- 网络错误 / 5xx / 429：指数退避重试（默认 1s / 2s，最多 max_retries）；
  重试逐次记账（on_call）；耗尽 → A040001。
- 安全：x-api-key 由 key_resolver() 提供；密钥绝不进入请求体 / 日志 / VOC。
- 测试性：key_resolver 与底层 _post 均可注入；backoff 可注入为 0。
- 真流式（P2-10）：`stream_complete()` 走 `POST /messages` + `stream=true` 的 Messages SSE，
  逐行解析 `data: {...}`；文本增量与 usage 分别以 `{"delta": ...}` / `{"usage": ...}` 产出。
  结构化模式（json_schema）在流式下仍按 tool-use 请求，但本方法只处理文本 delta。

纪律（冻结）：仅依赖 models.base / common.errors；不引入 ORM。
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

import httpx

from ..common.errors import BizError
from .base import ERR_KEY_MISSING, ERR_PROVIDER_EXHAUSTED, ERR_STRUCTURED, Msg, ProviderAdapter, Reply, _parse_structured


def _merge_usage(acc: dict, raw: object) -> bool:
    """把 Anthropic usage 的 input/output_tokens 归一化为 prompt/completion_tokens。

    返回是否发生了有效合并（供调用方决定是否产出 usage 事件）。
    """
    if not isinstance(raw, dict):
        return False
    changed = False
    for src, dst in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        val = raw.get(src)
        if isinstance(val, int) and not isinstance(val, bool):
            acc[dst] = val
            changed = True
    return changed


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
        _stream: Optional[Callable[..., object]] = None,
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
        # 流式：httpx.stream 返回上下文管理器；测试可注入 fake
        self._stream = _stream or (lambda *args, **kw: httpx.stream(*args, **kw))

    def _build_headers(self) -> dict:
        key = self._key_resolver()
        if not key:
            raise BizError(ERR_KEY_MISSING, msg=f"{self._provider} 的密钥缺失或无效，请到「向导 → 第 2 步」填写")
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
                        msg=f"{self._provider} 调用失败（已重试 {self.max_retries} 次，末次 {status}）",
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
                    msg=f"{self._provider} 连不上（已重试 {self.max_retries} 次），请检查网络或接口地址",
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc

            if json_schema is not None:
                # 二次校验：结构化结果可解析为对象
                _parse_structured(content, json_schema)
            return Reply(text=content, finish_reason=finish, model=self.model_id, raw=data)

        raise BizError(
            ERR_PROVIDER_EXHAUSTED,
            msg=f"{self._provider} 连不上（已重试 {self.max_retries} 次），请检查网络或接口地址",
                detail=str(last_exc) if last_exc else None,
        )

    # -- 对外：流式补全（P2-10：Messages API 真 SSE） ----------------------
    def stream_complete(self, messages: list[Msg], *, json_schema: Optional[dict] = None):
        """流式补全（Anthropic Messages SSE）。

        产出事件 dict：
        - `{"delta": "<文本增量>"}`：来自 `content_block_delta.delta.text`；
        - `{"usage": {...}}`：来自 `message_start.message.usage` 与 `message_delta.usage`，
          合并后统一为 `prompt_tokens` / `completion_tokens`（两者都到达后为完整值）。
        调用方负责拼接 delta 并在结束时用累计文本 + usage 记账。

        上游 4xx/5xx → A040001；SSE 数据块 JSON 解析失败 → A040004；
        网络异常 → A040001（不重试，由调用方决定是否重开流）。
        """
        url = f"{self.base_url}/messages"
        headers = self._build_headers()
        body = self._build_body(messages, json_schema=json_schema)
        body["stream"] = True

        usage: dict = {}
        try:
            with self._stream(
                "POST", url, headers=headers, json=body, timeout=self.timeout
            ) as resp:
                status = getattr(resp, "status_code", 200)
                if status >= 400:
                    raise BizError(
                        ERR_PROVIDER_EXHAUSTED,
                        msg=f"{self._provider} 流式调用失败（HTTP {status}），请稍后重试",
                    )
                for raw_line in resp.iter_lines():
                    line = (raw_line or "").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise BizError(ERR_STRUCTURED, msg=f"流式响应解析失败：{exc}") from exc
                    if not isinstance(chunk, dict):
                        raise BizError(ERR_STRUCTURED, msg="流式响应解析失败：数据块非对象")

                    etype = chunk.get("type")
                    if etype == "message_start":
                        message = chunk.get("message")
                        if isinstance(message, dict) and _merge_usage(
                            usage, message.get("usage")
                        ):
                            yield {"usage": dict(usage)}
                    elif etype == "message_delta":
                        if _merge_usage(usage, chunk.get("usage")):
                            yield {"usage": dict(usage)}
                    elif etype == "content_block_delta":
                        delta = chunk.get("delta")
                        text = delta.get("text") if isinstance(delta, dict) else None
                        if isinstance(text, str) and text:
                            yield {"delta": text}
        except (httpx.HTTPError, httpx.TransportError) as exc:
            raise BizError(
                ERR_PROVIDER_EXHAUSTED,
                msg=f"{self._provider} 流式连接中断，请稍后重试",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        return
