"""挑错与异构（F7 / BD-03，M5 落实）。

M4 已落实 F6 判分协议与闭环门控（evaluate_close / close_unit / has_unresolved_keypoint）。
本模块落实 M5 NEW 工作：
- evaluate_triggers / should_trigger_critic：挑错触发四规则（F7.1）。
  ① 关键卡点；② 代码/数字/引用断言；③ 答案机分歧（v1 多答案机后启用，v0 恒 false，规则保留）；
  ④ 用户手动点击。满足任一即触发 claim_check 评审。
- same_source：复用 models.registry.heterogeneous —— 同厂商 / 谱系相交 / 蒸馏关系 → 同源（True）。
- write_claim_check / record_same_source_warning：claim_check 经 db.write 落账
  （零 DDL，表已 M0 锁定：id/claim/providers/unit_id/session_id/agree/diff/user_ruling/residual）。
- arbitrate：用户仲裁 user_ruling ∈ {采纳/驳回/待验证}；「待验证」不阻断闭环（仅入质量度量）。
- BD-03 同源降级：同源时写 claim_check 同源警告（agree=NULL）+ 关键卡点单元不自动闭环
  （经 grading.evaluate_close 的 same_source_keypoint_block 决策实现，需 explicit_confirm 才能闭环并留痕）。
- count_same_source_keypoint_auto_close：治理计数（AC-5.4），同源关键卡点自动闭环次数恒为 0。

纪律（冻结）：所有写经 db.write；不引入 ORM；不依赖 routes/app（保持 headless）；零 DDL。
"""

from __future__ import annotations

import json
import uuid
from enum import Enum

from .. import db
from ..common.errors import BizError
from ..models import get_model, heterogeneous


def _providers_of(row: dict) -> dict:
    """claim_check.providers 为 JSON 文本列（db.query 不自动解析），按需解码为 dict。"""
    p = row.get("providers")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except json.JSONDecodeError:
            return {}
    return p or {}


class TriggerRule(Enum):
    """挑错触发四规则（F7.1，D1 §3.4 冻结）。

    ③ 答案机分歧在 v0 单答案机下恒不触发（规则保留，参数占位但永不命中）。
    """

    KEYPOINT = "关键卡点"
    CODE_NUM_REF = "代码数字引用"
    ANSWER_DISAGREEMENT = "答案机分歧"
    MANUAL = "手动"


# 用户仲裁取值（claim_check.user_ruling）
RULING_ADOPT = "采纳"        # 接受挑错者判定：确为实质错误（残留，阻断闭环）
RULING_REJECT = "驳回"       # 驳回挑错者判定：非错误（清零，不阻断）
RULING_PENDING = "待验证"    # 存疑，暂挂起（残留但不阻断，入质量度量）
RULING_SS_CONFIRM = "同源显式确认"  # BD-03：同源关键卡点经学习者显式确认闭环（留痕标记）

_RULINGS = (RULING_ADOPT, RULING_REJECT, RULING_PENDING)

# v0 单答案机：规则 ③ 禁用（基线冻结）；M6+ 多答案机后翻为 True 即启用。
_ENABLE_ANSWER_DISAGREEMENT_V0 = False


def evaluate_triggers(
    *,
    is_keypoint: bool,
    has_code_num_ref: bool,
    manual: bool,
    answer_disagreement: bool = False,
    _enable_answer_disagreement: bool = _ENABLE_ANSWER_DISAGREEMENT_V0,
) -> list[TriggerRule]:
    """评估挑错触发四规则，返回命中的规则列表（空 = 不触发）。

    规则 ③ 答案机分歧在 v0 恒 false（_enable_answer_disagreement=False），预留参数待 v1 启用。
    """
    rules: list[TriggerRule] = []
    if is_keypoint:
        rules.append(TriggerRule.KEYPOINT)
    if has_code_num_ref:
        rules.append(TriggerRule.CODE_NUM_REF)
    if _enable_answer_disagreement and answer_disagreement:
        rules.append(TriggerRule.ANSWER_DISAGREEMENT)
    if manual:
        rules.append(TriggerRule.MANUAL)
    return rules


def should_trigger_critic(
    *,
    is_keypoint: bool,
    has_code_num_ref: bool,
    manual: bool,
    answer_disagreement: bool = False,
) -> bool:
    """四规则满足任一即触发挑错评审。"""
    return bool(
        evaluate_triggers(
            is_keypoint=is_keypoint,
            has_code_num_ref=has_code_num_ref,
            manual=manual,
            answer_disagreement=answer_disagreement,
        )
    )


def same_source(critic_model_id: str, answer_model_id: str) -> bool:
    """挑错者与答案机是否同源（True=同源，需 BD-03 降级）。

    复用 models.registry.heterogeneous：family / derived_from 相交 → 同源 → heterogeneous 返回 False。
    """
    a = get_model(critic_model_id)
    b = get_model(answer_model_id)
    return not heterogeneous(a, b)


def write_claim_check(
    pid: str,
    *,
    claim: str,
    unit_id: str,
    providers=None,
    session_id: str | None = None,
    agree=None,
    diff=None,
    user_ruling: str | None = None,
    residual: int = 0,
    actor: str = "system",
) -> str:
    """写一条挑错记录（claim_check），经 db.write 同事务审计。返回记录 id。

    - providers：参与评审的模型清单 JSON（含 family / 同源标记），默认空列表。
    - agree：0=有分歧 1=一致 NULL=同源降级未评。
    - residual：残留实质错误数（清零或逐条待验证才可闭环）。
    """
    cid = uuid.uuid4().hex
    row = {
        "id": cid,
        "claim": claim,
        "providers": providers if providers is not None else [],
        "unit_id": unit_id,
        "session_id": session_id,
        "agree": agree,
        "diff": diff,
        "user_ruling": user_ruling,
        "residual": residual,
    }
    db.write(pid, "claim_check", row, actor=actor)
    return cid


def record_same_source_warning(
    pid: str,
    *,
    unit_id: str,
    critic_model_id: str,
    answer_model_id: str,
    is_keypoint: bool = False,
    session_id: str | None = None,
    actor: str = "system",
) -> str:
    """同源降级告警（BD-03）：写 claim_check 同源警告行。

    - agree=NULL（同源降级未评）。
    - providers 含 same_source=True 与 keypoint 标记（供治理计数与闭环门控识别）。
    - diff 记录日常语言警告文案。
    - 关键卡点单元由 grading.evaluate_close 据此拒绝自动闭环（需 explicit_confirm）。
    """
    providers = {
        "critic": critic_model_id,
        "answer": answer_model_id,
        "same_source": True,
        "keypoint": bool(is_keypoint),
    }
    return write_claim_check(
        pid,
        claim="同源降级未评",
        unit_id=unit_id,
        providers=providers,
        session_id=session_id,
        agree=None,
        diff="同源降级：挑错者与答案机同源，共识幻觉风险未降低，关键卡点单元不得自动闭环",
        residual=0,
        actor=actor,
    )


def arbitrate(pid: str, *, claim_id: str, ruling: str) -> None:
    """用户对挑错清单逐条仲裁（user_ruling ∈ 采纳/驳回/待验证）。

    residual 语义：采纳 → 实质错误残留(1，阻断闭环)；驳回 → 非错误(0，不阻断)；
    待验证 → 残留但不阻断(1)，仅入质量度量（§5.5 AC-5.2/5.4）。
    """
    if ruling not in _RULINGS:
        raise BizError("C400001", msg=f"非法仲裁：{ruling!r}（仅限 采纳/驳回/待验证）")
    residual = 1 if ruling in (RULING_ADOPT, RULING_PENDING) else 0
    db.write(
        pid,
        "claim_check",
        {"id": claim_id, "user_ruling": ruling, "residual": residual},
        actor="user",
    )


def mark_same_source_confirmed(pid: str, *, unit_id: str, actor: str = "user") -> None:
    """BD-03 显式确认留痕：将本单元同源警告行标记为「同源显式确认」。

    仅作用于 agree=NULL 且 providers.same_source=True 的同源警告行；用于治理计数区分
    「自动闭环（违规）」与「显式确认闭环（合规）」——前者恒不发生，后者留痕后不计入违规数。
    """
    rows = db.query(
        pid,
        "SELECT * FROM claim_check WHERE unit_id=? AND agree IS NULL",
        (unit_id,),
    )
    for r in rows:
        prov = _providers_of(r)
        if prov.get("same_source"):
            if r.get("user_ruling") != RULING_SS_CONFIRM:
                db.write(
                    pid,
                    "claim_check",
                    {"id": r["id"], "user_ruling": RULING_SS_CONFIRM},
                    actor=actor,
                )


def trigger_critic_if_needed(
    pid: str,
    *,
    unit_id: str,
    is_keypoint: bool,
    has_code_num_ref: bool = False,
    manual: bool = False,
    answer_disagreement: bool = False,
    critic_model_id: str,
    answer_model_id: str,
    claim: str | None = None,
    session_id: str | None = None,
    actor: str = "system",
) -> list[str]:
    """挑错触发编排（F7.1 + BD-03）：四规则命中才产生 claim_check 记录。

    - 未命中四规则 → 返回空列表（不写任何记录）。
    - 命中且同源（same_source True）→ 写同源降级警告行（record_same_source_warning）。
    - 命中且异源 → 写待挑错占位行（M10 调挑错者返回后回填 diff/agree/user_ruling）。

    返回新建的 claim_check id 列表（供界面并排展示 / 等待仲裁）。
    """
    if not should_trigger_critic(
        is_keypoint=is_keypoint,
        has_code_num_ref=has_code_num_ref,
        manual=manual,
        answer_disagreement=answer_disagreement,
    ):
        return []
    if same_source(critic_model_id, answer_model_id):
        cid = record_same_source_warning(
            pid, unit_id=unit_id, critic_model_id=critic_model_id,
            answer_model_id=answer_model_id, is_keypoint=is_keypoint,
            session_id=session_id, actor=actor,
        )
        return [cid]
    cid = write_claim_check(
        pid, claim=claim or "待挑错", unit_id=unit_id,
        providers={"critic": critic_model_id, "answer": answer_model_id, "same_source": False},
        session_id=session_id, agree=None, residual=0, actor=actor,
    )
    return [cid]


def count_same_source_keypoint_auto_close(pid: str) -> int:
    """治理计数（AC-5.4）：同源关键卡点「自动闭环」违规次数，v0 恒为 0。

    定义：存在 agree=NULL 且 same_source=True 且 keypoint=True 的同源警告行，且其归属单元
    已闭环（closed_at 非空）且 forced_close=0，且**未**标记「同源显式确认」。
    引擎保证此类单元不会自动闭环（须 explicit_confirm），故合规工程下该计数恒为 0。
    """
    cc = db.query(pid, "SELECT * FROM claim_check WHERE agree IS NULL")
    if not cc:
        return 0
    unit_ids = {r["unit_id"] for r in cc if r.get("unit_id")}
    if not unit_ids:
        return 0
    units = db.query(
        pid,
        f"SELECT * FROM unit WHERE id IN ({','.join('?' * len(unit_ids))})",
        tuple(unit_ids),
    )
    unit_map = {u["id"]: u for u in units}
    cnt = 0
    for r in cc:
        prov = _providers_of(r)
        if not (prov.get("same_source") and prov.get("keypoint")):
            continue
        u = unit_map.get(r["unit_id"])
        if not u:
            continue
        if u.get("closed_at") is not None and u.get("forced_close") == 0:
            if r.get("user_ruling") != RULING_SS_CONFIRM:
                cnt += 1
    return cnt
