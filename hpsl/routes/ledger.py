"""M10 账本总览页面（四本账 + 四平面 + 单元详情；§3.2.M10.2 `/ledger`）。

服务端渲染（Jinja2 + HTMX），消费 M7 ledger_summary 与 M9 compute_counts / plane_signals。
软提示区支持一键关闭（htmx POST → 返回剩余活跃项；页面按会话记忆隐藏）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates

from .. import projects as proj
from ..engine import ledger, governance
from .security import read_security

page_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@page_router.get("/ledger", dependencies=[Depends(read_security)])
def ledger_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req, name="ledger.html", context={"error": "尚未选择项目，请先在首页选择或新建项目"}
        )
    pid = active.id
    summary = ledger.ledger_summary(pid)
    counts = governance.compute_counts(pid)
    cfg = proj.get_effective_config(pid)
    ctx = governance.counts_context(pid)
    signals = governance.plane_signals(counts, cfg, ctx=ctx)
    # 软提示关闭按项目持久化：读取 project.config.dismissed_prompts（零 DDL）。
    dismissed = set(proj.get_raw_config(pid).get("dismissed_prompts", []))
    visible = [s for s in signals if s.id not in dismissed]
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="ledger.html",
        context={
            "project": active.to_dict(),
            "summary": summary,
            "counts": counts.to_dict(),
            "signals": [s.to_dict() for s in visible],
        },
    )
