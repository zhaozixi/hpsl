"""HTMX 写成功后的统一「提示 + 刷新」片段（M36/P1-B2）。

背景：此前 8 个写端点成功后直接返回 `HX-Refresh: true`，页面立即整页刷新，
提示片段还没被用户看见就被新页面冲掉（「点了没反应」的另一面：反应了但看不见）。
现改为：

- 响应体仍是 `<p class="ok">…</p>`（供局部容器的兜底展示）；
- 响应头 `HX-Trigger` 携带 `{"hpsl:refresh": {"msg": "…"}}`；
- `static/app.js` 监听 `hpsl:refresh` → `hpslToast(msg)` → 550ms 后 `location.reload()`，
  让提示先被看到，再刷新拉取最新数据。

只做展示层收敛，不改任何业务语义、不新增错误码、零 DDL。
"""

from __future__ import annotations

import html
import json

from fastapi.responses import HTMLResponse


def refresh_with_toast(msg: str) -> HTMLResponse:
    """返回「提示片段 + HX-Trigger 刷新事件」的 HTMX 成功响应。

    `msg` 同时进入 HTML 与响应头：HTML 内做转义；头部用 `ensure_ascii=True`
    序列化（HTTP 头按 latin-1 编码，中文必须转成 `\\uXXXX`，HTMX 端 JSON.parse
    后会还原为原文）。
    """
    trigger = json.dumps({"hpsl:refresh": {"msg": msg}}, ensure_ascii=True)
    return HTMLResponse(
        content=f'<p class="ok">{html.escape(msg)}</p>',
        headers={"HX-Trigger": trigger},
    )
