"""M4 能力暴露：test_connection（Provider 最小探活，离线安全）。

- `test_connection(provider, *, secrets_dir=None) -> dict`：
    `{"ok": bool, "detail": str}`；密钥缺失时附加 `"code": "A040003"`。
- 离线安全：任何网络异常 / 超时 → `{"ok": False, ...}`（**绝不抛未捕获异常**）。
- Provider 最低探活（短超时 5s）：取密钥 → 按 provider 构造最小 `/models` 探测请求。

设计（M11 批次1 实现规格 §2 / D-M11-3）：
- 密钥取自 `secrets.get_api_key`（经 `secrets_dir` 透传，落 HPSL_HOME/secrets）。
- `200 <= status < 500 且非 401/403` → ok=True；`401/403` → ok=False（密钥无效）；
  5xx → ok=False（服务异常）；任何异常/超时 → ok=False（网络不可达）。
- 纪律：仅依赖 models.registry / secrets / common.errors；零 DDL；无新错误码。
"""

from __future__ import annotations

import httpx

from .. import secrets as secrets_mod
from ..common.errors import BizError
from ..models import registry

_PROBE_TIMEOUT = 5.0


def test_connection(provider: str, *, secrets_dir=None) -> dict:
    """探测 Provider 连通性与密钥有效性（离线安全，绝不抛未捕获异常）。

    返回示例：
        {"ok": True, "detail": "连接成功（HTTP 200）"}
        {"ok": False, "detail": "密钥缺失或无效", "code": "A040003"}
        {"ok": False, "detail": "网络不可达或超时"}
    """
    # 1. 取密钥；缺失 → A040003（明确信号，非崩溃）
    try:
        key = secrets_mod.get_api_key(provider, secrets_dir=secrets_dir)
    except BizError as exc:
        if exc.code == "A040003":
            return {"ok": False, "detail": "密钥缺失或无效", "code": "A040003"}
        return {"ok": False, "detail": exc.msg or "密钥读取失败"}

    if provider not in registry.list_providers():
        return {"ok": False, "detail": f"不支持的 Provider：{provider}"}

    base = registry.default_base_url(provider).rstrip("/")
    kind = registry.provider_kind(provider)
    try:
        if kind == "openai_compat":
            # OpenAI 兼容协议（OpenAI、DeepSeek、通义、智谱、Kimi、豆包等统一走 Bearer）。
            resp = httpx.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=_PROBE_TIMEOUT,
            )
        elif kind == "anthropic":
            resp = httpx.get(
                f"{base}/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=_PROBE_TIMEOUT,
            )
        elif kind == "gemini":
            resp = httpx.get(
                f"{base}/models",
                params={"key": key},
                timeout=_PROBE_TIMEOUT,
            )
        else:
            return {"ok": False, "detail": f"不支持的 Provider：{provider}"}
    except Exception:
        # 离线 / DNS / 超时 / TLS：一律视为不可达，不抛异常
        return {"ok": False, "detail": "网络不可达或超时"}

    status = resp.status_code
    if status in (401, 403):
        return {"ok": False, "detail": f"密钥无效（HTTP {status}）"}
    if 200 <= status < 500:
        return {"ok": True, "detail": f"连接成功（HTTP {status}）"}
    return {"ok": False, "detail": f"服务异常（HTTP {status}）"}
