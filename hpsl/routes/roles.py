"""P0-02：六角色 Web 接线（首次让 `RoleLLM` 进入生产路径）。

- `POST /api/v1/roles/{role}/call`：按角色契约组装 → 适配器调用 → 记账（`llm_call`）→ 返回文本。
- 隔离：上下文只经 `assembler.assemble`（`RoleLLM.build_system_prompt` 内部完成），
  `answer_machine_output` 永不出现在发送给模型的 messages 中。
- 会话：未给 `session_id` 时自动创建一条 `session`（`unit_id` 可空），满足 `llm_call` 外键。
- 模型：`registry.pick_default_model()`（环境变量 `HPSL_ROLE_MODEL_<ROLE>` 可覆盖）。

纪律：零 DDL；全部写入经 `db.write`；不新增错误码（复用 A010001 / A020001 / A030001 /
A040001 / A040003 / C400001）；engine 仍保持 headless。
"""

from __future__ import annotations

import html as _html
import uuid
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse

from .. import db, projects as proj
from ..common.enums import Role
from ..common.errors import BizError
from ..common.result import ok
from ..engine.role_llm import call_role
from ..models import registry
from .security import write_security

api_router = APIRouter(prefix="/api/v1")

# 角色 → 学习者输入的落点字段（键名与 roles/contracts.py 的 allow 对齐）
_INPUT_FIELD: dict[Role, Optional[str]] = {
    Role.ANSWER_MACHINE: "question",
    Role.QUESTIONER: "learner_restatement",
    Role.BOUNDARY_SETTER: "learner_restatement",
    Role.CRITIC: "learner_production",
    Role.CLERK: "conversation_transcript",
    Role.TOPIC_PICKER: None,
}

# 需要概念账本摘要的角色（只读查询，零 DDL）
_CONCEPT_ROLES = (Role.TOPIC_PICKER, Role.CRITIC)


def _require_pid() -> str:
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目")
    return active.id


async def _parse_body(req: Request) -> dict:
    """兼容 JSON 与表单（与 unit.py 同构）。"""
    import json as _json

    ct = req.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            return await req.json()
        except Exception:
            raise BizError("C400001", msg="请求体需为 JSON")
    try:
        form = await req.form()
        return {k: v for k, v in form.items()}
    except Exception:
        return {}


def _role_of(raw: str) -> Role:
    """把路径参数解析为 Role；未知 → A030001（与 assembler 越权同一语义）。"""
    try:
        return Role(raw)
    except ValueError:
        pass
    try:
        return Role[raw]
    except KeyError:
        raise BizError("A030001", msg=f"未知角色：{raw!r}")


def _context(pid: str, role: Role, unit_id: Optional[str], body: dict) -> dict:
    """组装修饰前的原始上下文（真实裁剪由 assembler 按 allow 执行）。"""
    ctx = dict(body.get("context") or {})
    if unit_id:
        rows = db.query(pid, "SELECT id, title, difficulty_band FROM unit WHERE id=?", (unit_id,))
        if not rows:
            raise BizError("A020001", msg=f"单元 {unit_id} 不存在")
        ctx.setdefault("unit_title", rows[0]["title"])
        if rows[0].get("difficulty_band"):
            ctx.setdefault("difficulty_band", rows[0]["difficulty_band"])
    if role in _CONCEPT_ROLES:
        names = [r["name"] for r in db.query(pid, "SELECT name FROM concept ORDER BY name LIMIT 20")]
        ctx.setdefault("concept_summary", "、".join(names) if names else "（暂无概念）")
    learner_input = body.get("learner_input")
    if isinstance(learner_input, str) and learner_input:
        field = _INPUT_FIELD.get(role)
        if field:
            ctx.setdefault(field, learner_input)
        # RoleLLM.build_messages 以 learner_input 作为 user 消息（不进入 system 白名单）
        ctx["learner_input"] = learner_input
    return ctx


@api_router.post("/roles/{role}/call", dependencies=[Depends(write_security)])
async def role_call_api(req: Request, role: str):
    """调用一个角色并记账。

    请求体：`{unit_id?, session_id?, learner_input?, context?}`
    响应：`{text, provider, model, tokens_in, tokens_out, cost_usd, cumulative_usd,
            cost_status, session_id}`
    """
    pid = _require_pid()
    target = _role_of(role)
    body = await _parse_body(req)
    unit_id = body.get("unit_id") or None
    if unit_id is not None and not isinstance(unit_id, str):
        raise BizError("C400001", msg="unit_id 须为字符串")
    session_id = body.get("session_id") or None
    if session_id and not db.query(pid, "SELECT id FROM session WHERE id=?", (session_id,)):
        raise BizError("C400001", msg="session_id 不存在")

    # 先解析模型与上下文（无密钥 / 单元不存在时，在创建 session 之前失败，避免孤立会话）
    model_id = registry.pick_default_model(target.value)
    adapter = registry.build_adapter(model_id)
    ctx = _context(pid, target, unit_id, body)

    if not session_id:
        session_id = uuid.uuid4().hex
        db.write(
            pid,
            "session",
            {"id": session_id, "unit_id": unit_id, "started_at": db._now_utc()},
            "user",
        )

    reply, cost_vo = call_role(
        target, ctx, adapter=adapter, session_id=session_id, unit_id=unit_id
    )
    try:
        provider = registry.get_model(reply.model).provider
    except KeyError:
        provider = "unknown"
    if req.headers.get("hx-request") is not None:
        return HTMLResponse(
            f'<div class="card"><p><strong>{_html.escape(target.value)}</strong> · '
            f"{_html.escape(provider)}/{_html.escape(reply.model)}</p>"
            f'<pre class="pre-wrap">{_html.escape(reply.text)}</pre>'
            f'<p class="text-muted">tokens {cost_vo.tokens_in} / {cost_vo.tokens_out} · '
            f"本次 ${cost_vo.cost_usd:.6f} · 累计 ${cost_vo.cumulative_usd:.4f} · "
            f"预算 {cost_vo.status}</p></div>"
        )
    return ok(
        {
            "text": reply.text,
            "provider": provider,
            "model": reply.model,
            "tokens_in": cost_vo.tokens_in,
            "tokens_out": cost_vo.tokens_out,
            "cost_usd": cost_vo.cost_usd,
            "cumulative_usd": cost_vo.cumulative_usd,
            "cost_status": cost_vo.status,
            "session_id": session_id,
        }
    )
