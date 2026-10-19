"""P0-02：六角色 Web 接线（首次让 `RoleLLM` 进入生产路径）。

- `POST /api/v1/roles/{role}/call`：按角色契约组装 → 适配器调用 → 记账（`llm_call`）→ 返回文本。
- 隔离：上下文只经 `assembler.assemble`（`RoleLLM.build_system_prompt` 内部完成），
  `answer_machine_output` 永不出现在发送给模型的 messages 中。
- 空白上下文（P2-02 / F14.7）：请求体 `blank_context: true` 时**不读任何账本/单元字段**
  （不查 unit、不查 concept、不采纳 body.context），context 仅含学习者输入
  （`learner_input` 及按角色映射的输入字段；answer_machine 仍映射为 `question`），
  用于检查既有记忆是否造成偏向。`input-preview` 同样支持 `?blank_context=true`。
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
from ..common import money as _money
from ..engine import cost as cost_engine
from ..models import registry
from ..roles import assembler
from ..roles.contracts import CONTRACTS
from .security import rate_limiter, read_security, write_security

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


def _as_bool(value) -> bool:
    """宽容解析布尔开关（JSON true / 表单 "true" / "1" / "on"）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _context(pid: str, role: Role, unit_id: Optional[str], body: dict) -> dict:
    """组装修饰前的原始上下文（真实裁剪由 assembler 按 allow 执行）。

    `blank_context` 为真时进入空白上下文模式：**不读任何账本/单元字段**
    （不查 unit / concept，也不采纳调用方传入的 context），只保留学习者输入。
    """
    blank = _as_bool(body.get("blank_context"))
    ctx: dict = {}
    if not blank:
        raw_ctx = body.get("context")
        if isinstance(raw_ctx, dict):
            ctx.update(raw_ctx)
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


@api_router.get("/roles/contracts", dependencies=[Depends(read_security)])
def role_contracts_api():
    """六角色契约（allow 白名单 + 描述）；供"输入预览/审计"消费。"""
    return ok(
        [
            {
                "role": c.role.value,
                "allow": list(c.allow),
                "description": c.description,
            }
            for c in CONTRACTS.values()
        ]
    )


@api_router.get("/units/{uid}/input-preview", dependencies=[Depends(read_security)])
def input_preview_api(
    uid: str,
    role: str,
    blank_context: bool = False,
    learner_input: str = "",
):
    """F3.3 输入预览：返回该角色**实际可见**的字段与值（不含 allow 之外的内容）。

    只读：不调用模型、不写库。可视化"本次会发给模型的内容"，用于隔离审计。
    `?blank_context=true` 时预览空白上下文模式（不读 unit/concept，仅学习者输入；
    可用 `learner_input` 查询参数模拟输入），字段集合仍受角色契约 allow 约束。
    """
    pid = _require_pid()
    target = _role_of(role)
    body: dict = {}
    if blank_context:
        body["blank_context"] = True
    if learner_input:
        body["learner_input"] = learner_input
    ctx = _context(pid, target, uid, body)
    return ok(assembler.preview(target, ctx))


@api_router.post("/roles/{role}/call", dependencies=[Depends(write_security)])
async def role_call_api(req: Request, role: str):
    """调用一个角色并记账。

    请求体：`{unit_id?, session_id?, learner_input?, context?, blank_context?}`
    `blank_context: true` → 空白上下文模式（不读账本/单元字段，context 仅含学习者输入）。
    响应：`{text, provider, model, tokens_in, tokens_out, cost_usd, cumulative_usd,
            cost_status, session_id}`
    """
    pid = _require_pid()
    target = _role_of(role)
    body = await _parse_body(req)
    blank = _as_bool(body.get("blank_context"))
    unit_id = body.get("unit_id") or None
    if unit_id is not None and not isinstance(unit_id, str):
        raise BizError("C400001", msg="unit_id 须为字符串")
    # 空白上下文不组装单元字段，但 unit_id 仍会写入 session/llm_call（外键），故仅做存在性校验
    if unit_id is not None and blank and not db.query(pid, "SELECT id FROM unit WHERE id=?", (unit_id,)):
        raise BizError("A020001", msg=f"单元 {unit_id} 不存在")
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

    # M36/A1：预算闸门（blocked → A080001）；此前只在预算页展示、从未拦截调用
    cost_engine.ensure_budget(session_id)

    # 五维限流的第 3 维：LLM 频次（默认 10 次/分钟/会话）
    if not rate_limiter.allow("llm_freq", session_id):
        raise BizError("C429001", msg="模型调用过于频繁（每会话 10 次/分钟），请稍后再试")

    reply, cost_vo = call_role(
        target, ctx, adapter=adapter, session_id=session_id, unit_id=unit_id
    )
    # M38：费用双币展示（美元记账 + 人民币折算），汇率取项目配置
    rate = proj.get_effective_config(pid).usd_cny_rate
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
            f"本次 {_money.fmt_both(cost_vo.cost_usd, rate)} · "
            f"累计 {_money.fmt_both(cost_vo.cumulative_usd, rate)} · "
            f"预算 {cost_vo.status}</p></div>"
            '<p><small><a href="">刷新页面查看完整会话记录</a></small></p>'
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
