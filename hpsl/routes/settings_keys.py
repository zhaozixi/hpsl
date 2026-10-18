"""M11 批次1：密钥 REST 暴露（消费 M4 secrets 模块 + M4 test_connection）。

- `GET  /settings/keys`：列出已知 Provider 密钥状态（仅 has_key / stored_in / key_tail）。
- `PUT  /settings/keys/{provider}`：写入 provider 密钥（落 HPSL_HOME/secrets 文件保险库）。
- `POST /providers/{name}/test`：测试连接（connectivity.test_connection）。

纪律约束（M11 批次1 实现规格 §1 / §5 / D-M11-1）：
- 零 DDL：密钥不进 project.db，落 HPSL_HOME/secrets/ 文件（经 FileBackend，绝不污染源码树）。
- 读接口 read_security；写接口 write_security（Origin + CSRF + 限流）。
- 密钥红线（D1 §6.5）：GET 绝不返回完整密钥；错误码仅复用 C400001 / A040003；无新码。
- 安全分层：密钥写经 secrets.set_api_key（密钥红线 D1 §6.5，不经 db.write）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Depends

from .. import config as cfg
from .. import secrets as secrets_mod
from ..common.errors import BizError
from ..common.result import ok
from ..models import connectivity, registry
from ..secrets.keystore import FileBackend
from .security import write_security, read_security

api_router = APIRouter(prefix="/api/v1")

# 密钥存储目录：HPSL_HOME/secrets（D-M11-1，避免污染源码包目录）。
# 注意：HPSL_HOME 在测试中被 autouse fixture 按测试重写为临时目录，故**不可**在模块
# 导入期缓存路径/后端；必须按请求动态解析，并按路径缓存后端（避免每次请求重复告警）。
_BACKENDS: dict[str, FileBackend] = {}


def _secrets_dir() -> Path:
    return cfg.home() / "secrets"


def _backend() -> FileBackend:
    d = _secrets_dir()
    key = str(d)
    b = _BACKENDS.get(key)
    if b is None:
        b = FileBackend(d)
        _BACKENDS[key] = b
    return b


async def _parse_body(req: Request) -> dict:
    """兼容 JSON 与表单（HTMX 默认 form 提交），统一成 dict。"""
    ct = req.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            import json as _json
            return await req.json()  # type: ignore[unreachable]
        except Exception:
            raise BizError("C400001", msg="请求体需为 JSON")
    try:
        form = await req.form()
        return {k: v for k, v in form.items()}
    except Exception:
        return {}


def _validate_provider(provider: str) -> None:
    if provider not in registry.list_providers():
        raise BizError(
            "C400001",
            msg=f"未知 Provider：{provider}（可用：{', '.join(registry.list_providers())}）",
        )


# ---------------------------------------------------------------------------
# REST：列出密钥状态（仅脱敏字段）
# ---------------------------------------------------------------------------
@api_router.get("/settings/keys", dependencies=[Depends(read_security)])
def list_keys_api():
    items = [secrets_mod.key_status(p, backend=_backend()) for p in registry.list_providers()]
    return ok({"secrets_dir": str(_secrets_dir()), "providers": items})


# ---------------------------------------------------------------------------
# REST：写入密钥
# ---------------------------------------------------------------------------
@api_router.put("/settings/keys/{provider}", dependencies=[Depends(write_security)])
async def set_key_api(provider: str, req: Request):
    _validate_provider(provider)
    data = await _parse_body(req)
    key = data.get("key")
    if not isinstance(key, str) or not key.strip():
        raise BizError("C400001", msg="缺少密钥（key 字段，非空字符串）")
    secrets_mod.set_api_key(provider, key.strip(), backend=_backend())
    return ok(secrets_mod.key_status(provider, backend=_backend()))


# ---------------------------------------------------------------------------
# REST：测试连接
# ---------------------------------------------------------------------------
@api_router.post("/providers/{name}/test", dependencies=[Depends(write_security)])
async def test_provider_api(name: str):
    _validate_provider(name)
    result = connectivity.test_connection(name, secrets_dir=_secrets_dir())
    return ok(result)
