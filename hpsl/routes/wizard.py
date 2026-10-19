"""M11 批次1：首次启动向导 /wizard（UC-04：建项目 → 填 key → 测连接 → 完成）。

- `GET /wizard`（read_security）：渲染四步引导页；Provider 下拉取自 registry。
- 写入均走既有写接口（PUT /settings/keys/{provider} / POST /providers/{name}/test /
  POST /api/v1/projects + switch），由接口自身 write_security 校验。

纪律约束（M11 批次1 实现规格 §3 / §5 / D-M11-2）：
- 页面只读（read_security）；不新增后端写逻辑；零 DDL；无新错误码。
- 向导四步：建项目 / 填 key / 测连接 / 完成（不做「分配角色」，留后续）。
- 无激活项目时显示「尚未选择项目」提示（与 /ledger / /settings 一致）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from ..templating import make_templates

from .. import projects as proj
from ..models import registry
from .security import read_security

page_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = make_templates(str(_TEMPLATES_DIR))


@page_router.get("/wizard", dependencies=[Depends(read_security)])
def wizard_page(req: Request):
    active = proj.active_project()
    providers = [
        {"provider": p, "display": registry.provider_display(p)}
        for p in registry.list_providers()
    ]
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="wizard.html",
        context={
            "active": active.to_dict() if active is not None else None,
            "providers": providers,
        },
    )
