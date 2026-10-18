"""最小延迟复测队列（规格 §4.2 / M2 实现规格）。

职责：
- REVIEW_INTERVALS = (2, 7, 21) 天（2→7→21→21 循环）。
- enqueue_reviews：为每个概念插 review(kind='到期', due=closed_at+2d)，幂等跳过活跃行；actor=system。
- due_reviews：读时计算，返回 result IS NULL AND due <= now 按 due 升序。
- submit_review：写 attempt(kind=复测) + concept 写回(verify_status/last_verified_at) + review 保持活跃并推进 due（result 维持 NULL，复测结果以 attempt 为准）。
- 纪律不变量：submit_review 绝不写 unit/gap/任何当前单元账本；ReviewResultVO.wrote_to_current_unit 恒 False。
- 所有写只走 db.write；不直写 audit_log。

依赖方向：engine -> common, db（禁止反向；不依赖 routes/app，保持 headless）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..common.enums import AttemptKind
from ..common.errors import BizError
from .. import db


REVIEW_INTERVALS = (2, 7, 21)


# ---------------------------------------------------------------------------
# 时间助手（ISO-8601 UTC）
# ---------------------------------------------------------------------------
def _parse_iso(s: str) -> datetime:
    """解析 ISO-8601 UTC 文本（如 2026-10-15T08:30:00.123Z）。"""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _fmt_iso(dt: datetime) -> str:
    """格式化为 ISO-8601 UTC（与 db._now_utc() 同款：字典序=时间序）。"""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _add_days(iso_utc: str, days: int) -> str:
    """在 ISO-8601 日期上加减天数（按 UTC 计算）。"""
    return _fmt_iso(_parse_iso(iso_utc) + timedelta(days=days))


def _pass_threshold(pid: str) -> float:
    """读取项目有效 pass_threshold（默认 0.7，对齐 ConfigVO）。不改动 config.py。"""
    rows = db.query(pid, "SELECT config FROM project WHERE id=?", (pid,))
    if rows:
        raw = rows[0].get("config")
        cfg = raw if isinstance(raw, dict) else (json.loads(raw) if isinstance(raw, str) else {})
        cfg = cfg or {}
        if isinstance(cfg.get("pass_threshold"), (int, float)):
            return float(cfg["pass_threshold"])
    return 0.7


def _interval_days(due_str: Optional[str], lva_str: Optional[str]) -> int:
    """由 review.due 与 concept.last_verified_at 推算当前间隔档（天，四舍五入）。"""
    if not (due_str and lva_str):
        return REVIEW_INTERVALS[0]
    try:
        d = (_parse_iso(due_str) - _parse_iso(lva_str)).total_seconds() / 86400.0
        return max(1, round(d))
    except Exception:
        return REVIEW_INTERVALS[0]


def _next_interval(cur: int) -> int:
    """间隔推进：2→7→21→21 循环。"""
    if cur <= REVIEW_INTERVALS[0]:
        return REVIEW_INTERVALS[1]
    return REVIEW_INTERVALS[2]


# ---------------------------------------------------------------------------
# VO
# ---------------------------------------------------------------------------
@dataclass
class ReviewItemVO:
    """到期复测条目视图对象（due_reviews 返回）。"""

    review_id: str
    concept_id: str
    concept_name: Optional[str]
    kind: str
    interval_days: int
    due: str
    last_verified_at: Optional[str]

    def to_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "concept_id": self.concept_id,
            "concept_name": self.concept_name,
            "kind": self.kind,
            "interval_days": self.interval_days,
            "due": self.due,
            "last_verified_at": self.last_verified_at,
        }


@dataclass
class ReviewResultVO:
    """复测提交结果视图对象（submit_review 返回）。"""

    concept_id: str
    score: float
    passed: bool
    review_id: str
    attempt_id: str
    next_due: str
    wrote_to_current_unit: bool = False  # 纪律不变量：复测绝不写当前单元账本

    def to_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "score": self.score,
            "passed": self.passed,
            "review_id": self.review_id,
            "attempt_id": self.attempt_id,
            "next_due": self.next_due,
            "wrote_to_current_unit": self.wrote_to_current_unit,
        }


@dataclass
class ReviewSubmitRequest:
    """复测提交入参（可选 VO，便于后续 Web 层复用）。"""

    concept_id: str
    score: float
    seconds: Optional[int] = None
    actor: str = "user"


@dataclass
class ReviewStartVO:
    """开始复测（生成复测卷）的视图对象（start_review 返回）。"""

    review_id: str
    concept_id: str
    concept_name: Optional[str]
    question: Optional[str]      # 出题者生成的复测卷（边界题）；M10 经出题者形态填充，引擎不直连 LLM
    due: str
    interval_days: int
    attempt_id: str


# ---------------------------------------------------------------------------
# 队列操作
# ---------------------------------------------------------------------------
def enqueue_reviews(pid: str, concept_ids: list[str], closed_at: str) -> list[str]:
    """为每个概念入队一条到期复测（kind='到期', due=closed_at+2d）。

    - 幂等：已有 result IS NULL 的活跃行则跳过（可重复调用）。依赖先查后写，避免唯一索引冲突异常。
    - actor='system'。

    返回：本次实际新插入的 review id 列表（被跳过的活跃行不计入）。
    """
    due0 = _add_days(closed_at, REVIEW_INTERVALS[0])
    inserted: list[str] = []
    for cid in concept_ids:
        active = db.query(
            pid, "SELECT id FROM review WHERE concept_id=? AND result IS NULL", (cid,)
        )
        if active:
            continue  # 幂等：已有未完成活跃行则跳过
        rid = uuid.uuid4().hex
        db.write(
            pid,
            "review",
            {
                "id": rid,
                "concept_id": cid,
                "kind": "到期",
                "due": due0,
                "result": None,
            },
            "system",
        )
        inserted.append(rid)
    return inserted


def due_reviews(pid: str, now: Optional[str] = None) -> list[ReviewItemVO]:
    """返回 result IS NULL AND due <= now 的到期复测，按 due 升序（首页消费）。"""
    if now is None:
        now = db._now_utc()
    rows = db.query(
        pid,
        """
        SELECT r.id AS review_id, r.concept_id, c.name AS concept_name,
               r.kind, r.due, c.last_verified_at
        FROM review r
        LEFT JOIN concept c ON c.id = r.concept_id
        WHERE r.result IS NULL AND r.due <= ?
        ORDER BY r.due ASC
        """,
        (now,),
    )
    out: list[ReviewItemVO] = []
    for r in rows:
        out.append(
            ReviewItemVO(
                review_id=r["review_id"],
                concept_id=r["concept_id"],
                concept_name=r["concept_name"],
                kind=r["kind"],
                interval_days=_interval_days(r["due"], r["last_verified_at"]),
                due=r["due"],
                last_verified_at=r["last_verified_at"],
            )
        )
    return out


def submit_review(
    pid: str,
    concept_id: str,
    score: float,
    seconds: Optional[int] = None,
    actor: str = "user",
) -> ReviewResultVO:
    """提交一次复测（按概念定位活跃 review 后委托 submit_review_by_id）。

    保留 M2 既有概念级入口（向后兼容 + 测试）；内部分派到 review_id 精确路径。
    纪律不变量：绝不写 unit/gap/任何当前单元账本（wrote_to_current_unit 恒 False）。
    """
    rev_rows = db.query(
        pid,
        "SELECT id FROM review WHERE concept_id=? AND result IS NULL ORDER BY due ASC LIMIT 1",
        (concept_id,),
    )
    if not rev_rows:
        raise BizError("A060001", msg="复测条目不存在或已完成")
    return submit_review_by_id(pid, rev_rows[0]["id"], score, seconds=seconds, actor=actor)


def _attempt_scored(pid: str, attempt_id: str) -> bool:
    """判断草稿复测 attempt 是否已被评分（confirmed_score 非 NULL）。"""
    rows = db.query(pid, "SELECT confirmed_score FROM attempt WHERE id=?", (attempt_id,))
    if not rows:
        return False
    return rows[0].get("confirmed_score") is not None


def submit_review_by_id(
    pid: str,
    review_id: str,
    score: float,
    seconds: Optional[int] = None,
    actor: str = "user",
) -> ReviewResultVO:
    """按 review_id 提交复测（写回隔离，对齐基线 §3.2.M6.2 内部 API）。

    写 attempt（kind='复测'，复用 start_review 的草稿卷或直接新建）+ concept（verify_status /
    last_verified_at）+ review 保持活跃并推进 due（result 维持 NULL，复测结果以 attempt 为准；
    attempt_id 回写 review）。

    纪律不变量：绝不写 unit/gap/任何当前单元账本（wrote_to_current_unit 恒 False）。
    pass_threshold 默认 0.7（取自项目配置，失败回退常量）。

    异常：review 不存在或已完成（result 非 NULL）→ A060001。
    """
    now = db._now_utc()
    threshold = _pass_threshold(pid)
    passed = score >= threshold

    rev_rows = db.query(
        pid, "SELECT * FROM review WHERE id=? AND result IS NULL", (review_id,)
    )
    if not rev_rows:
        raise BizError("A060001", msg="复测条目不存在或已完成")
    review = rev_rows[0]
    cid = review["concept_id"]

    # 草稿卷复用：若 start_review 已建 attempt 且未评分，则更新之；否则新建
    draft_id = review.get("attempt_id")
    attempt_id = draft_id if (draft_id and not _attempt_scored(pid, draft_id)) else uuid.uuid4().hex

    # 1) 写 attempt（kind=复测）；不绑定 unit（纪律不变量）
    db.write(
        pid,
        "attempt",
        {
            "id": attempt_id,
            "concept_id": cid,
            "kind": AttemptKind.RETEST.value,  # '复测'
            "confirmed_score": score,
            "seconds": seconds,
            "ts": now,
        },
        actor,
    )

    # 2) 先读旧 last_verified_at 并算当前档：必须在写回 concept **之前**，
    #    否则读到的已是 now，due-now≈0 → cur_int 恒 1 → 下一档恒 7，21 天档永远到不了。
    crows = db.query(pid, "SELECT last_verified_at FROM concept WHERE id=?", (cid,))
    old_lva = crows[0]["last_verified_at"] if crows else None
    cur_int = _interval_days(review["due"], old_lva)

    # 3) 写回 concept（verify_status / last_verified_at）
    db.write(
        pid,
        "concept",
        {
            "id": cid,
            "verify_status": "已验证" if passed else "待验证",
            "last_verified_at": now,
        },
        actor,
    )

    # 4) 推进 review 的 due 到下一档（2→7→21→21），并回写 attempt_id；保持 review 活跃
    #    （result 维持 NULL，与 uk_review_active 唯一活跃行约束一致），复测结果以 attempt 为准。
    new_due = _add_days(now, _next_interval(cur_int))
    db.write(
        pid,
        "review",
        {
            "id": review["id"],
            "concept_id": cid,
            "attempt_id": attempt_id,
            "kind": review["kind"],
            "due": new_due,
        },
        actor,
    )

    return ReviewResultVO(
        concept_id=cid,
        score=score,
        passed=passed,
        review_id=review["id"],
        attempt_id=attempt_id,
        next_due=new_due,
        wrote_to_current_unit=False,
    )


def start_review(
    pid: str,
    review_id: str,
    *,
    question: Optional[str] = None,
) -> ReviewStartVO:
    """开始一次复测（生成复测卷），返回 ReviewStartVO（对齐基线 §3.2.M6.2）。

    - 校验 review 存在且活跃（result IS NULL）；否则 A060001。
    - 幂等（单活跃卷）：若 review.attempt_id 已设置（已开始的草稿卷），返回既有 VO，不重复建 attempt。
    - 否则建 attempt（kind='复测'，concept_id）草稿，并回写 review.attempt_id（标记「复测中」）。
    - question 由 M10 经出题者（BoundarySetter，M3/M4）生成后传入；引擎层保持 headless（不直连 LLM）。
    - 纪律不变量：绝不写 unit/gap/任何当前单元账本。
    """
    rows = db.query(pid, "SELECT * FROM review WHERE id=? AND result IS NULL", (review_id,))
    if not rows:
        raise BizError("A060001", msg="复测条目不存在或已完成")
    review = rows[0]
    cid = review["concept_id"]
    crows = db.query(pid, "SELECT name, last_verified_at FROM concept WHERE id=?", (cid,))
    cname = crows[0]["name"] if crows else None
    now = db._now_utc()

    attempt_id = review.get("attempt_id")
    if not attempt_id:
        # 建草稿卷并回写 review.attempt_id（标记「复测中」）
        attempt_id = uuid.uuid4().hex
        db.write(
            pid,
            "attempt",
            {
                "id": attempt_id,
                "concept_id": cid,
                "kind": AttemptKind.RETEST.value,  # '复测'
                "ts": now,
            },
            "system",
        )
        db.write(
            pid,
            "review",
            {
                "id": review["id"],
                "concept_id": cid,
                "attempt_id": attempt_id,
                "kind": review["kind"],
                "due": review["due"],
            },
            "system",
        )

    return ReviewStartVO(
        review_id=review["id"],
        concept_id=cid,
        concept_name=cname,
        question=question,
        due=review["due"],
        interval_days=_interval_days(review["due"], crows[0]["last_verified_at"] if crows else None),
        attempt_id=attempt_id,
    )
