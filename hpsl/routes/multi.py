"""P2-03 / P2-04：多模型交叉验证 + 多挑错 + 评分要点核对（Web 接线）。

三个端点（全部 write_security）：
- `POST /api/v1/units/{uid}/critic/multi`：多模型挑错。对每个挑错者模型各写一条
  `claim_check`（同源行走 BD-03 同源降级警告），并返回两两同源矩阵 `pairs`。
- `POST /api/v1/units/{uid}/answers/cross-check`：多答案机交叉验证。每个模型经
  `call_role(Role.ANSWER_MACHINE, ...)` 调用一次（各自记 `llm_call` + 写 `turn`），
  返回各模型答案与本次调用成本。真实调用由 fake adapter 在测试中替代。
- `POST /api/v1/attempts/{aid}/rubric/verify`：评分要点核对。读 attempt 的 rubric，
  交指定模型核对，核对意见写入 `claim_check`（claim="评分要点核对"）。

纪律：零 DDL（只用 claim_check / attempt / session / llm_call / turn 既有表列）；
不新增错误码（复用 C400001 / A040001 / A040003 / A010001）；不编辑 app.py，
router 以模块级 `api_router`（prefix=/api/v1）暴露，由 app 装配方挂载。
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Request, Depends

from .. import db, projects as proj
from ..common.enums import Role
from ..common.errors import BizError
from ..common.result import ok
from ..engine import claim_check as cc
from ..engine.role_llm import call_role
from ..models import registry
from ..models.base import Msg
from .security import write_security

api_router = APIRouter(prefix="/api/v1")

# 评分要点核对：核对者 system 提示（逐字对齐 P2-04 口径）
_RUBRIC_VERIFY_SYSTEM = "核对评分要点是否有误，列出疑点"

# 空评分要点判定（None / 空串 / 空容器皆视为无要点）
_EMPTY_RUBRIC = (None, "", [], {})


def _require_pid() -> str:
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目")
    return active.id


async def _parse_body(req: Request) -> dict:
    """兼容 JSON 与表单（与 unit.py / roles.py 同构）。"""
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


def _unit(pid: str, uid: str) -> dict:
    """取单元（不存在 → C400001；写入 claim_check / session 前先校验，避免 FK 失败）。"""
    rows = db.query(pid, "SELECT id, title FROM unit WHERE id=?", (uid,))
    if not rows:
        raise BizError("C400001", msg=f"单元 {uid} 不存在")
    return rows[0]


def _model_ids(raw, default_role: str) -> list[str]:
    """解析 model_ids：缺省取该角色默认模型；显式提供须为非空字符串数组。"""
    if raw is None:
        return [registry.pick_default_model(default_role)]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise BizError("C400001", msg="model_ids 须为非空字符串数组")
    out: list[str] = []
    for m in raw:
        if not isinstance(m, str) or not m.strip():
            raise BizError("C400001", msg="model_ids 须为非空字符串数组")
        if m not in out:  # 去重，避免同一模型重复挑错/重复计费
            out.append(m)
    return out


def _ensure_registered(model_ids: list[str]) -> None:
    """模型须在 models.yaml 注册；未注册 → A040003（与无可用密钥同一出口语义）。"""
    for m in model_ids:
        try:
            registry.get_model(m)
        except KeyError:
            raise BizError("A040003", msg=f"模型不可用：{m!r}（未在 models.yaml 注册或缺少密钥）")


def _rubric_of(att: dict):
    """attempt.rubric 为 JSON 文本列（db.query 不自动解析），按需解码。"""
    raw = att.get("rubric")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


def _last_call_cost(pid: str, session_id: str) -> tuple[int, int, float]:
    """取本会话最近一次 llm_call 的单次 tokens / 成本（call_role 已各自记账）。"""
    rows = db.query(
        pid,
        "SELECT tokens_in, tokens_out, cost FROM llm_call WHERE session_id=? "
        "ORDER BY rowid DESC LIMIT 1",
        (session_id,),
    )
    if not rows:
        return 0, 0, 0.0
    row = rows[0]
    return int(row["tokens_in"]), int(row["tokens_out"]), float(row["cost"])


# ---------------------------------------------------------------------------
# P2-04：多模型挑错
# ---------------------------------------------------------------------------
@api_router.post("/units/{uid}/critic/multi", dependencies=[Depends(write_security)])
async def critic_multi_api(req: Request, uid: str):
    """多模型挑错：每个挑错者一条 claim_check + 两两同源矩阵。

    请求体：`{claim, model_ids?, answer_model_id?, is_keypoint?}`
    响应：`{created: [{id, model, same_source}], pairs: [{a, b, same_source}]}`
    """
    pid = _require_pid()
    _unit(pid, uid)
    body = await _parse_body(req)
    claim = body.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        raise BizError("C400001", msg="claim 必填")
    model_ids = _model_ids(body.get("model_ids"), "critic")
    _ensure_registered(model_ids)
    answer_model_id = body.get("answer_model_id") or None
    if answer_model_id is not None:
        if not isinstance(answer_model_id, str):
            raise BizError("C400001", msg="answer_model_id 须为字符串")
        _ensure_registered([answer_model_id])
    session_id = body.get("session_id") or None
    if session_id and not db.query(pid, "SELECT id FROM session WHERE id=?", (session_id,)):
        raise BizError("C400001", msg="session_id 不存在")

    created = cc.trigger_critics_multi(
        pid,
        unit_id=uid,
        claim=claim,
        model_ids=model_ids,
        is_keypoint=bool(body.get("is_keypoint", False)),
        session_id=session_id,
        actor="system",
        answer_model_id=answer_model_id,
    )
    return ok({"created": created, "pairs": cc.same_source_pairs(model_ids)})


# ---------------------------------------------------------------------------
# P2-03：多答案机交叉验证
# ---------------------------------------------------------------------------
@api_router.post("/units/{uid}/answers/cross-check", dependencies=[Depends(write_security)])
async def cross_check_api(req: Request, uid: str):
    """多答案机交叉验证：每模型一次调用、各自记账（llm_call）+ 各自写 turn。

    请求体：`{question, model_ids?, session_id?}`
    响应：`{answers: [{model, text, tokens_in, tokens_out, cost_usd}], session_id}`
    """
    pid = _require_pid()
    unit = _unit(pid, uid)
    body = await _parse_body(req)
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise BizError("C400001", msg="question 必填")

    # 先解析模型清单（未知模型在创建 session / 记费之前失败，避免孤立会话与半截记账）
    model_ids = _model_ids(body.get("model_ids"), "answer_machine")
    _ensure_registered(model_ids)

    session_id = body.get("session_id") or None
    if session_id and not db.query(pid, "SELECT id FROM session WHERE id=?", (session_id,)):
        raise BizError("C400001", msg="session_id 不存在")
    if not session_id:
        session_id = uuid.uuid4().hex
        db.write(
            pid,
            "session",
            {"id": session_id, "unit_id": uid, "started_at": db._now_utc()},
            "user",
        )

    # 答案机可见字段仅 unit_title / topic / question（assembler 白名单裁剪）
    topic = body.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        topic = unit["title"]
    ctx = {
        "unit_title": unit["title"],
        "topic": topic,
        "question": question,
        # call_role 以 learner_input 作为 user 消息（答案机的问题即学习者提问）
        "learner_input": question,
    }

    answers: list[dict] = []
    for model_id in model_ids:
        adapter = registry.build_adapter(model_id)
        reply, _cost_vo = call_role(
            Role.ANSWER_MACHINE,
            ctx,
            adapter=adapter,
            session_id=session_id,
            unit_id=uid,
        )
        tokens_in, tokens_out, call_cost = _last_call_cost(pid, session_id)
        answers.append(
            {
                "model": model_id,
                "text": reply.text,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": call_cost,
            }
        )
    return ok({"answers": answers, "session_id": session_id})


# ---------------------------------------------------------------------------
# P2-04：评分要点核对（二厂商核对）
# ---------------------------------------------------------------------------
@api_router.post("/attempts/{aid}/rubric/verify", dependencies=[Depends(write_security)])
async def rubric_verify_api(req: Request, aid: str):
    """评分要点核对：模型核对 rubric 是否有误，意见写入 claim_check。

    请求体：`{model_id?}`
    响应：`{claim_id, feedback}`
    """
    pid = _require_pid()
    rows = db.query(pid, "SELECT * FROM attempt WHERE id=?", (aid,))
    if not rows:
        raise BizError("C400001", msg=f"尝试 {aid} 不存在")
    att = rows[0]
    rubric = _rubric_of(att)
    if rubric in _EMPTY_RUBRIC:
        raise BizError("C400001", msg="该尝试没有评分要点，无法核对")

    body = await _parse_body(req)
    model_id = body.get("model_id") or registry.pick_default_model("critic")
    if not isinstance(model_id, str):
        raise BizError("C400001", msg="model_id 须为字符串")
    _ensure_registered([model_id])

    adapter = registry.build_adapter(model_id)
    messages = [
        Msg("system", _RUBRIC_VERIFY_SYSTEM),
        Msg("user", json.dumps(rubric, ensure_ascii=False)),
    ]
    reply = adapter.complete(messages)
    feedback = reply.text

    claim_id = cc.write_claim_check(
        pid,
        claim="评分要点核对",
        unit_id=att.get("unit_id"),
        providers={"verifier": model_id, "attempt_id": aid},
        session_id=None,
        agree=None,
        diff=feedback,
        residual=0,
    )
    return ok({"claim_id": claim_id, "feedback": feedback})
