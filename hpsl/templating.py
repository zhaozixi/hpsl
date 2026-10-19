"""Jinja2 模板环境装配（M38）。

此前 12 个路由模块各自 `Jinja2Templates(directory=...)`，要给模板加过滤器就得改 12 处，
且容易漏。这里把「创建 + 注册过滤器」收成一个函数，各模块改为
`_TEMPLATES = make_templates(str(_TEMPLATES_DIR))`，行为不变、口径统一。
"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from .common import money


def make_templates(directory: str) -> Jinja2Templates:
    """创建模板环境并注册货币过滤器（usd / cny / both / rate）。"""
    templates = Jinja2Templates(directory=directory)
    templates.env.filters["usd"] = money.fmt_usd
    templates.env.filters["cny"] = money.fmt_cny
    templates.env.filters["both"] = money.fmt_both
    templates.env.filters["rate"] = money.fmt_rate
    return templates
