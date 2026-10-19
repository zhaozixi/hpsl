"""M11 批次1：密钥 REST 暴露（消费 M4 secrets 模块 + M4 test_connection）。

- `GET  /settings/keys`：列出已知 Provider 密钥状态（仅 has_key / stored_in / key_tail）。
- `PUT  /settings/keys/{provider}`：写入 provider 密钥（落 HPSL_HOME/secrets 文件保险库）。
- `DELETE /settings/keys/{provider}`：清除某厂商密钥（M39；幂等，锁定态亦可用）。
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
from ..templating import make_templates

api_router = APIRouter(prefix="/api/v1")
page_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = make_templates(str(_TEMPLATES_DIR))

# 密钥存储目录：HPSL_HOME/secrets（D-M11-1，避免污染源码包目录）。
# 注意：HPSL_HOME 在测试中被 autouse fixture 按测试重写为临时目录，故**不可**在模块
# 导入期缓存路径/后端；必须按请求动态解析。M25 起不再缓存后端实例，避免
# `HPSL_MASTER_PASSPHRASE` 在进程内变更后仍用旧口令。
def _secrets_dir() -> Path:
    return cfg.home() / "secrets"


def _backend() -> FileBackend:
    return FileBackend(_secrets_dir())


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
    state = secrets_mod.lock_state(_secrets_dir())
    items = [secrets_mod.key_status(p, backend=_backend()) for p in registry.list_providers()]
    return ok(
        {
            "secrets_dir": str(_secrets_dir()),
            "locked": state["locked"],
            "lock_reason": state["reason"],
            "providers": items,
        }
    )


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
# REST：删除密钥（M39）
# ---------------------------------------------------------------------------
@api_router.delete("/settings/keys/{provider}", dependencies=[Depends(write_security)])
def delete_key_api(provider: str):
    """清除某厂商密钥（幂等）。

    - 不受密钥库锁定影响：删除只需 unlink，不需要口令解密；
    - 遍历 keyring 与文件保险库，避免"钥匙串删了、文件还留一份"；
    - 本来就没有 → 仍返回 200，`removed=False`，前端据此显示"本来就没配置"。
    """
    _validate_provider(provider)
    removed = secrets_mod.delete_api_key(provider, secrets_dir=_secrets_dir())
    return ok(
        {
            "provider": provider,
            "removed": removed,
            "status": secrets_mod.key_status(provider, backend=_backend()),
        }
    )


# ---------------------------------------------------------------------------
# REST：测试连接
# ---------------------------------------------------------------------------
@api_router.post("/providers/{name}/test", dependencies=[Depends(write_security)])
async def test_provider_api(name: str):
    _validate_provider(name)
    result = connectivity.test_connection(name, secrets_dir=_secrets_dir())
    return ok(result)


# ---------------------------------------------------------------------------
# 页面：独立的密钥管理入口（M38）
# ---------------------------------------------------------------------------
@page_router.get("/keys", dependencies=[Depends(read_security)])
def keys_page(req: Request):
    """密钥管理页：不需要先有项目——密钥是全局的，换了项目也要能改。

    只暴露 has_key / stored_in / key_tail（D1 §6.5）；锁定状态下禁用手填，
    并把原因原样告诉用户（而不是等提交后弹一个 A040003）。
    """
    state = secrets_mod.lock_state(_secrets_dir())
    items = []
    for provider in registry.list_providers():
        st = secrets_mod.key_status(provider, backend=_backend())
        st["display"] = registry.provider_display(provider)
        items.append(st)
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="keys.html",
        context={
            "providers": items,
            "locked": state["locked"],
            "lock_reason": state["reason"],
            "stored_in": state["stored_in"],
            "secrets_dir": str(_secrets_dir()),
        },
    )
