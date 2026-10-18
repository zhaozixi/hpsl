"""P1-02：验证判分组端点（把"自评 → 看要点 → 确认得分 → 强行闭环 → 挑错"拆成显式动作）。

- `POST /units/{uid}/attempts/{aid}/self-score`：写自评（作答后）
- `POST /units/{uid}/attempts/{aid}/confirm-score`：写确认得分（对照评分要点后）
- `GET  /units/{uid}/attempts/{aid}/rubric`：**A050001 守卫**——自评前不可见
- `POST /units/{uid}/force-close`：强行闭环（理由必填）
- `POST /units/{uid}/critic`：按四规则触发挑错，写 claim_check

纪律：零 DDL；全部写入经 db.write / 引擎；不新增错误码（复用 A020001/A050001~4/
A030001/C400001/A010001）；引擎保持 headless。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request, Depends

from .. import db, projects as proj
from ..common.errors import BizError
from ..common.result import ok
from ..engine import claim_check as cc
from ..engine import grading as gr
from ..engine.grading import rubric_reveal_allowed
from ..models import registry
from .security import read_security, write_security

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


def _score(value, field: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise BizError("C400001", msg=f"{field} 须为 0~1 的数字")
    if not (0.0 <= score <= 1.0):
        raise BizError("C400001", msg=f"{field} 须在 [0,1] 区间")
    return score


def _attempt(pid: str, uid: str, aid: str) -> dict:
    rows = db.query(pid, "SELECT * FROM attempt WHERE id=? AND unit_id=?", (aid, uid))
    if not rows:
        raise BizError("C400001", msg=f"尝试 {aid} 不存在或不属于单元 {uid}")
    return rows[0]


def _rubric_of(att: dict):
    raw = att.get("rubric")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


@api_router.post(
    "/units/{uid}/attempts/{aid}/self-score", dependencies=[Depends(write_security)]
)
async def self_score_api(req: Request, uid: str, aid: str):
    pid = _require_pid()
    _attempt(pid, uid, aid)
    data = await _parse_body(req)
    score = _score(data.get("self_score", data.get("score")), "self_score")
    db.write(pid, "attempt", {"id": aid, "self_score": score}, "user")
    return ok({"attempt_id": aid, "self_score": score})


@api_router.post(
    "/units/{uid}/attempts/{aid}/confirm-score", dependencies=[Depends(write_security)]
)
async def confirm_score_api(req: Request, uid: str, aid: str):
    pid = _require_pid()
    _attempt(pid, uid, aid)
    data = await _parse_body(req)
    score = _score(data.get("confirmed_score", data.get("score")), "confirmed_score")
    db.write(pid, "attempt", {"id": aid, "confirmed_score": score}, "user")
    return ok({"attempt_id": aid, "confirmed_score": score})


@api_router.get(
    "/units/{uid}/attempts/{aid}/rubric", dependencies=[Depends(read_security)]
)
def rubric_api(uid: str, aid: str):
    """评分要点（A050001）：仅当学习者已作答并自评后才返回。"""
    pid = _require_pid()
    att = _attempt(pid, uid, aid)
    self_scored = att.get("self_score") is not None
    if not rubric_reveal_allowed(answered=True, self_scored=self_scored):
        raise BizError("A050001", msg="先作答并自评，之后才能看评分要点")
    return ok({"attempt_id": aid, "rubric": _rubric_of(att)})


@api_router.post("/units/{uid}/force-close", dependencies=[Depends(write_security)])
async def force_close_api(req: Request, uid: str):
    pid = _require_pid()
    data = await _parse_body(req)
    score = _score(data.get("confirmed_score"), "confirmed_score")
    reason = data.get("close_reason") or data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise BizError("A050003", msg="强行闭环必须填写理由")
    vo = gr.close_unit(
        pid, uid, score, force_close=True, close_reason=reason.strip()
    )
    enqueued = gr.enqueue_unit_reviews(pid, uid)
    payload = vo.to_dict()
    payload["reviews_enqueued"] = len(enqueued)
    return ok(payload)


@api_router.post("/units/{uid}/critic", dependencies=[Depends(write_security)])
async def critic_api(req: Request, uid: str):
    """按四规则触发挑错；同源时写同源降级警告（agree=NULL）。"""
    pid = _require_pid()
    data = await _parse_body(req)
    claim = data.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        raise BizError("C400001", msg="claim 必填（待检断言）")
    if len(claim) > 2000:
        raise BizError("C400001", msg="claim 过长（≤2000 字符）")
    try:
        critic_model = data.get("critic_model_id") or registry.pick_default_model("critic")
        answer_model = data.get("answer_model_id") or registry.pick_default_model("answer_machine")
    except BizError:
        raise
    created = cc.trigger_critic_if_needed(
        pid,
        unit_id=uid,
        is_keypoint=bool(data.get("is_keypoint")),
        has_code_num_ref=bool(data.get("has_code_num_ref")),
        manual=bool(data.get("manual")),
        answer_disagreement=False,
        critic_model_id=critic_model,
        answer_model_id=answer_model,
        claim=claim.strip(),
        session_id=data.get("session_id") or None,
        actor="system",
    )
    return ok(
        {
            "created": created,
            "same_source": cc.same_source(critic_model, answer_model),
            "critic_model": critic_model,
            "answer_model": answer_model,
        }
    )
