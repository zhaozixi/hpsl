"""OpenAI 兼容适配器（E-01：OpenAI / DeepSeek 等，BC-7 防腐层）。

- httpx 直连 `POST {base_url}/chat/completions`。
- supports_json=True：原生 `response_format={"type":"json_object"}`（或给定 json_schema 走 json_schema 模式）。
- 网络错误 / 5xx / 429：指数退避重试（默认 1s / 2s，最多 max_retries）；重试逐次记账（on_call 钩子）；
  耗尽 → A040001。
- 结构化解析失败 → A040004。
- 安全：Authorization 头由 key_resolver() 提供；密钥绝不进入请求体 / 日志 / VOC。
- 测试性：key_resolver 与底层 _post 均可注入；backoff 可注入为 0 以避免真实休眠。

纪律（冻结）：仅依赖 models.base / common.errors；不引入 ORM。
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

import httpx

from ..common.errors import BizError
from .base import ERR_KEY_MISSING, ERR_PROVIDER_EXHAUSTED, ERR_STRUCTURED, Msg, ProviderAdapter, Reply, _parse_structured


_HTTP_HINTS = {
    400: "请求被拒绝（多半是模型名或参数不适用）",
    401: "密钥无效或已失效，请到「密钥」页重新填写",
    403: "密钥没有访问该模型的权限，请检查账号权限",
    404: "模型或接口地址不存在，请检查模型名是否正确",
    413: "请求内容过大，请缩短输入或减少资料挂载",
    422: "参数不被接受，请检查模型名与请求设置",
}


def _http_hint(status: int) -> str:
    """把上游 HTTP 状态码转成中文结论（上游英文原文另走 BizError.detail）。"""
    return _HTTP_HINTS.get(status, "请求被拒绝，请检查模型设置")


def _default_key_resolver(provider: str) -> Callable[[], str]:
    def _resolve() -> str:
        from ..secrets import keystore

        return keystore.get_api_key(provider)

    return _resolve


class OpenAICompatAdapter:
    """OpenAI 兼容 Chat Completions 适配器（防腐层实现）。"""

    def __init__(
        self,
        name: str,
        base_url: str,
        model_id: str,
        *,
        key_resolver: Optional[Callable[[], str]] = None,
        provider: Optional[str] = None,
        supports_json: bool = True,
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
        self.supports_json = supports_json
        self.timeout = timeout
        self.max_retries = max_retries
        self._backoff = backoff if backoff is not None else (lambda n: 2 ** n)  # 1s, 2s, ...
        self._on_call = on_call
        self._key_resolver = key_resolver or _default_key_resolver(self._provider)
        # 默认走真实 httpx.post（url 为位置参数）；测试可注入 fake
        self._post = _post or (lambda *args, **kw: httpx.post(*args, **kw))
        # 流式：httpx.stream 返回上下文管理器；测试可注入 fake
        self._stream = _stream or (lambda *args, **kw: httpx.stream(*args, **kw))

    # -- 内部：请求构造 ---------------------------------------------------
    def _build_headers(self) -> dict:
        key = self._key_resolver()
        if not key:
            raise BizError(ERR_KEY_MISSING, msg=f"Provider {self._provider} 密钥缺失或无效")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _build_body(self, messages: list[Msg], *, json_schema: Optional[dict]) -> dict:
        body: dict = {
            "model": self.model_id,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if self.supports_json:
            if json_schema is not None:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "structured_output", "schema": json_schema},
                }
            else:
                body["response_format"] = {"type": "json_object"}
        return body

    # -- 内部：响应解析 ---------------------------------------------------
    def _extract(self, resp_json: dict) -> tuple[str, str]:
        try:
            choice = resp_json["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason", "stop")
        except (KeyError, IndexError, TypeError) as exc:
            raise BizError(ERR_STRUCTURED, msg=f"响应结构异常：{exc}") from exc
        return content, finish

    # -- 对外：补全 -------------------------------------------------------
    def complete(
        self,
        messages: list[Msg],
        *,
        json_schema: Optional[dict] = None,
        stream: bool = False,
    ) -> Reply:
        if stream:
            # M4 仅实现非流式；流式位置预留（钩子不阻塞主路径）
            pass
        url = f"{self.base_url}/chat/completions"
        headers = self._build_headers()
        body = self._build_body(messages, json_schema=json_schema)
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            # 重试逐次记账（防预算失真；M4 仅钩子，完整成本治理属 M8）
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
                if status >= 400:
                    if status == 429 or status >= 500:
                        last_exc = RuntimeError(f"上游返回 {status}")
                        if attempt < self.max_retries:
                            time.sleep(self._backoff(attempt))
                            continue
                        hint = (
                            "调用太频繁，请稍后再试"
                            if status == 429
                            else "服务端暂时不可用，请稍后再试"
                        )
                        raise BizError(
                            ERR_PROVIDER_EXHAUSTED,
                            msg=f"{self._provider} {hint}（已重试 {self.max_retries} 次，末次 {status}）",
                        )
                    # 其它 4xx（参数/图片格式/鉴权等）：给出中文结论，上游原文降级为 detail，
                    # 不要误报"结构异常"，也不要把英文原文直接怼到用户面前。
                    detail = ""
                    try:
                        payload = resp.json()
                        err = payload.get("error") if isinstance(payload, dict) else None
                        if isinstance(err, dict):
                            detail = str(err.get("message") or err.get("code") or "")
                        elif err:
                            detail = str(err)
                    except Exception:
                        detail = (getattr(resp, "text", "") or "")[:200]
                    raise BizError(
                        ERR_PROVIDER_EXHAUSTED,
                        msg=f"{self._provider} 返回 {status}：{_http_hint(status)}",
                        detail=detail or None,
                    )
                data = resp.json()
                content, finish = self._extract(data)
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

            # 结构化校验：失败时抛 A040004（与上游状态码无关）
            if json_schema is not None:
                _parse_structured(content, json_schema)
            return Reply(text=content, finish_reason=finish, model=self.model_id, raw=data)

        # 不应到达；保险兜底
        raise BizError(
            ERR_PROVIDER_EXHAUSTED,
            msg=f"Provider {self._provider} 调用失败（重试耗尽）：{last_exc}",
        )

    # -- 对外：流式补全 ---------------------------------------------------
    def stream_complete(self, messages: list[Msg], *, json_schema: Optional[dict] = None):
        """流式补全（OpenAI 兼容 SSE）。

        产出事件 dict：
        - `{"delta": "<文本增量>"}`
        - `{"usage": {...}}`（若上游在最后一块返回 usage；请求已带 stream_options）
        调用方负责把 delta 推给前端，并在结束时用累计文本 + usage 记账。
        上游 4xx/5xx → A040001；JSON 解析失败 → A040004。
        """
        url = f"{self.base_url}/chat/completions"
        headers = self._build_headers()
        body = self._build_body(messages, json_schema=json_schema)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

        status = 200
        with self._stream("POST", url, headers=headers, json=body, timeout=self.timeout) as resp:
            status = getattr(resp, "status_code", 200)
            if status >= 400:
                raise BizError(
                    ERR_PROVIDER_EXHAUSTED,
                    msg=f"Provider {self._provider} 流式调用失败（HTTP {status}）",
                )
            for raw_line in resp.iter_lines():
                line = (raw_line or "").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise BizError(ERR_STRUCTURED, msg=f"流式响应解析失败：{exc}") from exc
                usage = chunk.get("usage")
                if isinstance(usage, dict) and usage:
                    yield {"usage": usage}
                for choice in chunk.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        yield {"delta": delta}
        return
