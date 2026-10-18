"""P1-03：Provider 直连对话（非流式 + 真流式 SSE）。

- `POST /api/v1/providers/{name}/chat`：统一消息数组直连模型，记账 + 写 turn。
- `POST /api/v1/providers/{name}/chat/stream`：SSE 流式；占用 `sse_concurrency` 并发槽，
  受 `llm_freq` 限流；结束后用累计文本 + usage 记账。
- 与"角色调用"的区别：本接口不做角色契约裁剪，供高级/调试用途（输入预览仍是角色层的）。

纪律：零 DDL；写经 db.write / record_call；复用 A040001/A040003/A020001/C400001/C429001/A010001。
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import StreamingResponse

from .. import db, projects as proj
from ..common.errors import BizError
from ..common.result import ok
from ..engine.cost import ensure_budget, record_call
from ..models import registry
from ..models.base import Msg, Reply
from .security import hold_sse_slot, rate_limiter, write_security

api_router = APIRouter(prefix="/api/v1")


def _require_pid() -> str:
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目")
    return active.id


async def _parse_body(req: Request) -> dict:
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


def _validate_provider(name: str) -> None:
    if name not in registry.list_providers():
        raise BizError(
            "C400001",
            msg=f"未知 Provider：{name}（可用：{', '.join(registry.list_providers())}）",
        )


def _default_model(provider: str) -> str:
    for cfg in registry.list_models():
        if cfg.provider == provider:
            return cfg.id
    raise BizError("C400001", msg=f"Provider {provider} 没有已注册模型（检查 models.yaml）")


def _messages(body: dict) -> list[Msg]:
    raw = body.get("messages")
    if not isinstance(raw, list) or not raw:
        raise BizError("C400001", msg="messages 必须是非空数组")
    out: list[Msg] = []
    for item in raw:
        if not isinstance(item, dict):
            raise BizError("C400001", msg="messages 每项需为 {role, content}")
        role = item.get("role")
        content = item.get("content")
        if role not in ("system", "user", "assistant"):
            raise BizError("C400001", msg="role 仅支持 system/user/assistant")
        if isinstance(content, str):
            pass
        elif isinstance(content, list) and content and all(
            isinstance(part, dict) and isinstance(part.get("type"), str) for part in content
        ):
            # 多模态：原样透传给 OpenAI 兼容端点（image_url / text 等）
            pass
        else:
            raise BizError("C400001", msg="content 需为字符串或非空的多模态数组")
        out.append(Msg(role, content))
    return out


def _ensure_session(pid: str, session_id: Optional[str], unit_id: Optional[str]) -> str:
    if session_id:
        if not db.query(pid, "SELECT id FROM session WHERE id=?", (session_id,)):
            raise BizError("C400001", msg="session_id 不存在")
        return session_id
    sid = uuid.uuid4().hex
    db.write(
        pid, "session", {"id": sid, "unit_id": unit_id, "started_at": db._now_utc()}, "user"
    )
    return sid


def _validate_unit(pid: str, unit_id: Optional[str]) -> None:
    """校验请求携带的 unit_id 真实存在（不存在 → A020001，复用已注册错误码）。

    必须在 `_ensure_session` 写入 session 之前调用：`session.unit_id` 有外键约束，
    若先写 session 再校验，伪造 unit_id 会触发 sqlite3.IntegrityError 裸 500。
    """
    if not unit_id:
        return
    if not db.query(pid, "SELECT id FROM unit WHERE id=?", (unit_id,)):
        raise BizError("A020001", msg=f"unit_id 不存在：{unit_id}")


def _build_adapter(model_id: str):
    """构造适配器；未知 model 的 KeyError（registry.get_model）→ A040003。

    `registry.build_adapter` 对未注册模型直接抛 KeyError，路由层捕获后转成已注册的业务
    错误码，避免裸 500（错误码注册表无新增项）。
    """
    try:
        return registry.build_adapter(model_id)
    except KeyError as exc:
        raise BizError(
            "A040003", msg=f"未知模型：{model_id}（检查 models.yaml 是否已注册）"
        ) from exc


def _append_turns(pid: str, session_id: str, user_text: str, reply: Reply, provider: str) -> None:
    db.append_turn(pid, session_id, "user", user_text, actor="user")
    db.append_turn(
        pid,
        session_id,
        "assistant",
        reply.text,
        provider=provider,
        model=reply.model,
        actor="system",
    )


def _turn_text(content) -> str:
    """把消息内容转成可落库的文本：多模态数组 → 文本 + 图片 URL 摘要。"""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            parts.append(str(part.get("text") or ""))
        elif kind == "image_url":
            url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) else None
            parts.append(f"[图片] {url or ''}".strip())
        else:
            parts.append(f"[{kind}]")
    return "\n".join(p for p in parts if p)


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@api_router.post("/providers/{name}/chat", dependencies=[Depends(write_security)])
async def chat_api(req: Request, name: str):
    pid = _require_pid()
    _validate_provider(name)
    body = await _parse_body(req)
    msgs = _messages(body)
    model_id = body.get("model") or _default_model(name)
    unit_id = body.get("unit_id") or None
    _validate_unit(pid, unit_id)  # 先校验外键目标，再写 session（避免 IntegrityError 裸 500）
    session_id = _ensure_session(pid, body.get("session_id") or None, unit_id)
    ensure_budget(session_id)
    if not rate_limiter.allow("llm_freq", session_id):
        raise BizError("C429001", msg="模型调用过于频繁（每会话 10 次/分钟），请稍后再试")

    adapter = _build_adapter(model_id)
    reply = adapter.complete(msgs, json_schema=body.get("json_schema"))
    cost_vo = record_call("chat", reply, session_id, unit_id)
    _append_turns(pid, session_id, _turn_text(msgs[-1].content), reply, name)
    return ok(
        {
            "text": reply.text,
            "provider": name,
            "model": reply.model,
            "tokens_in": cost_vo.tokens_in,
            "tokens_out": cost_vo.tokens_out,
            "cost_usd": cost_vo.cost_usd,
            "cost_status": cost_vo.status,
            "session_id": session_id,
        }
    )


@api_router.post(
    "/providers/{name}/chat/stream",
    dependencies=[Depends(write_security), Depends(hold_sse_slot)],
)
async def chat_stream_api(req: Request, name: str):
    """SSE 流式对话。事件：`{"delta": ...}` → `{"done": true, ...}`；失败 `{"error": {...}}`。"""
    pid = _require_pid()
    _validate_provider(name)
    body = await _parse_body(req)
    msgs = _messages(body)
    model_id = body.get("model") or _default_model(name)
    unit_id = body.get("unit_id") or None
    _validate_unit(pid, unit_id)  # 先校验外键目标，再写 session（避免 IntegrityError 裸 500）
    session_id = _ensure_session(pid, body.get("session_id") or None, unit_id)
    ensure_budget(session_id)
    if not rate_limiter.allow("llm_freq", session_id):
        raise BizError("C429001", msg="模型调用过于频繁（每会话 10 次/分钟），请稍后再试")

    adapter = _build_adapter(model_id)
    user_text = _turn_text(msgs[-1].content)

    def stream_fallback():
        """不支持流式的适配器：一次性推送完整回答（仍走 SSE 协议）。"""
        try:
            reply = adapter.complete(msgs)
            cost_vo = record_call("chat", reply, session_id, unit_id)
            _append_turns(pid, session_id, user_text, reply, name)
            yield _sse({"delta": reply.text})
            yield _sse(
                {
                    "done": True,
                    "model": reply.model,
                    "tokens_in": cost_vo.tokens_in,
                    "tokens_out": cost_vo.tokens_out,
                    "cost_usd": cost_vo.cost_usd,
                    "session_id": session_id,
                }
            )
        except BizError as exc:
            yield _sse({"error": {"code": exc.code, "msg": exc.msg}})

    if not hasattr(adapter, "stream_complete"):
        return StreamingResponse(stream_fallback(), media_type="text/event-stream")

    def stream_events():
        parts: list[str] = []
        usage: dict = {}
        try:
            for ev in adapter.stream_complete(msgs):
                if "delta" in ev:
                    parts.append(ev["delta"])
                    yield _sse({"delta": ev["delta"]})
                elif "usage" in ev:
                    usage = ev["usage"]
            reply = Reply(
                text="".join(parts),
                finish_reason="stop",
                model=model_id,
                raw={"usage": usage},
            )
            cost_vo = record_call("chat", reply, session_id, unit_id)
            _append_turns(pid, session_id, user_text, reply, name)
            yield _sse(
                {
                    "done": True,
                    "model": model_id,
                    "tokens_in": cost_vo.tokens_in,
                    "tokens_out": cost_vo.tokens_out,
                    "cost_usd": cost_vo.cost_usd,
                    "session_id": session_id,
                }
            )
        except BizError as exc:
            yield _sse({"error": {"code": exc.code, "msg": exc.msg}})
        except Exception as exc:  # 网络中断等：给出可读错误，不抛裸栈
            yield _sse({"error": {"code": "B999001", "msg": f"流式中断：{exc}"}})

    return StreamingResponse(stream_events(), media_type="text/event-stream")
