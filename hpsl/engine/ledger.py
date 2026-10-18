"""四本账与审计编排层（规格 §3.2.M7，M7 新增；BC-5）。

职责（在 M0 db.write / db.undo 基座之上增量）：
- confirm_gap_candidates：gap_candidate → gap（确认入账）/ 废弃（批量确认，幂等候选 ID 集合）。
- list_gaps：缺口账本查询（按 unit_id / level / status 过滤）。
- ledger_summary：四本账统计摘要（选题用，不暴露原始对话），供 M2 选题与 M9/M10 复用。
- 候选缺口写入（书记员 actor=clerk）由调用方经 db.write 完成；本模块只做「用户确认」编排。

依赖方向：engine.ledger -> common, db（禁止反向；不依赖 routes/app，保持 headless）。
纪律：所有写只走 db.write（唯一入口，同事务审计）；零 DDL（仅消费 M0 锁定的 gap/gap_candidate 等既有表）。
"""

from __future__ import annotations

import uuid
from typing import Optional

from .. import db
from ..common.errors import BizError
from ..common.dto import GapListVO
from ..db import _now_utc


def confirm_gap_candidates(pid: str, items: list[dict], actor: str = "user") -> dict:
    """候选缺口批量确认（gap_candidate → gap / 废弃）。

    - items：每项 {candidate_id, action, level}；action ∈ {confirm, discard}。
    - confirm：写 gap（level=确认等级，缺省取候选 suggested_level；status=待补，actor=user）
      + 候选置「已确认」（幂等：已确认/已废弃则跳过）。
    - discard：候选置「废弃」（幂等）。
    - 非法 action → C400001；confirm 时等级非 1/2/3 → C400001。
    返回：{confirmed, discarded, skipped} 计数。
    """
    confirmed = discarded = skipped = 0
    affected_units: set = set()
    for it in items:
        cid = it.get("candidate_id")
        action = it.get("action")
        if action not in ("confirm", "discard"):
            raise BizError("C400001", msg="无效的确认动作（仅支持 confirm/discard）")
        cands = db.query(pid, "SELECT * FROM gap_candidate WHERE id=?", (cid,))
        if not cands:
            skipped += 1  # 候选不存在（幂等重放安全跳过）
            continue
        cand = cands[0]
        if cand["status"] in ("已确认", "废弃"):
            skipped += 1
            continue
        if action == "discard":
            db.write(pid, "gap_candidate", {"id": cid, "status": "废弃"}, actor)
            discarded += 1
        else:  # confirm
            level = it.get("level") or cand.get("suggested_level")
            if level not in (1, 2, 3):
                raise BizError("C400001", msg="确认等级必须为 1/2/3")
            now = _now_utc()
            gid = uuid.uuid4().hex
            db.write(
                pid,
                "gap",
                {
                    "id": gid,
                    "unit_id": cand["unit_id"],
                    "session_id": None,  # 候选不直接携带 session；缺口归单元，会话留空待 M10 回填
                    "level": level,
                    "text": cand["text"],
                    "status": "待补",
                    "context": None,
                    "created_at": now,
                    "updated_at": now,
                },
                actor,
            )
            db.write(pid, "gap_candidate", {"id": cid, "status": "已确认"}, actor)
            confirmed += 1
        affected_units.add(cand["unit_id"])
    # §3.2.M7.5 Step 3：会话收尾，标记 gaps_confirmed=1（M9 零缺口占比分母口径）
    for uid in affected_units:
        for sid in _session_ids_for_unit(pid, uid):
            db.write(pid, "session", {"id": sid, "gaps_confirmed": 1}, actor)
    return {"confirmed": confirmed, "discarded": discarded, "skipped": skipped}


def _session_ids_for_unit(pid: str, unit_id: str) -> list:
    """返回与某单元关联的会话 id 列表（缺口确认归属会话，用于 gaps_confirmed 标记）。"""
    rows = db.query(pid, "SELECT id FROM session WHERE unit_id=?", (unit_id,))
    return [r["id"] for r in rows]


def list_gaps(
    pid: str,
    unit_id: Optional[str] = None,
    level: Optional[int] = None,
    status: Optional[str] = None,
) -> list[GapListVO]:
    """缺口账本查询（按 unit_id / level / status 可选过滤）。"""
    sql = "SELECT * FROM gap WHERE 1=1"
    params: list = []
    if unit_id is not None:
        sql += " AND unit_id=?"
        params.append(unit_id)
    if level is not None:
        sql += " AND level=?"
        params.append(level)
    if status is not None:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at ASC, id ASC"
    rows = db.query(pid, sql, tuple(params))
    out: list[GapListVO] = []
    for r in rows:
        out.append(
            GapListVO(
                id=r["id"],
                unit_id=r["unit_id"],
                session_id=r["session_id"],
                level=r["level"],
                text=r["text"],
                status=r["status"],
                context=r["context"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
        )
    return out


def ledger_summary(pid: str) -> dict:
    """四本账统计摘要（选题用，不暴露原始对话；供 M2 选题 / M9 / M10 复用）。

    返回 dict：
    - gaps：{total, 待补, 已补, 已验证, 废弃}
    - units：{total, 按 status 计数}
    - cards：{total, 已验证, 未验证}
    - concept_edges：{total}
    - concepts：{total, 未验证, 已验证, 待验证}
    - reviews_active：当前活跃复测数（result IS NULL）
    """
    gaps = _count_by_status(pid, "gap", "status")
    units = _count_by_status(pid, "unit", "status")
    cards = _count_by_status(pid, "card", "verify_status")
    concepts = _count_by_status(pid, "concept", "verify_status")
    edges = db.query(pid, "SELECT count(*) c FROM concept_edge")[0]["c"]
    reviews_active = db.query(
        pid, "SELECT count(*) c FROM review WHERE result IS NULL"
    )[0]["c"]
    return {
        "gaps": {
            "total": sum(gaps.values()),
            "待补": gaps.get("待补", 0),
            "已补": gaps.get("已补", 0),
            "已验证": gaps.get("已验证", 0),
            "废弃": gaps.get("废弃", 0),
        },
        "units": {"total": sum(units.values()), **units},
        "cards": {
            "total": sum(cards.values()),
            "已验证": cards.get("已验证", 0),
            "未验证": cards.get("未验证", 0),
        },
        "concept_edges": {"total": edges},
        "concepts": {
            "total": sum(concepts.values()),
            "未验证": concepts.get("未验证", 0),
            "已验证": concepts.get("已验证", 0),
            "待验证": concepts.get("待验证", 0),
        },
        "reviews_active": reviews_active,
    }


def _count_by_status(pid: str, table: str, col: str) -> dict:
    """通用：按某列分组计数（表/列名来自代码常量，非外部输入，强制白名单校验）。"""
    if table not in db.TABLES or col not in db.TABLES[table]:
        raise KeyError(f"非白名单表/列：{table}.{col}（仅允许 M0 锁定库对象）")
    rows = db.query(pid, f"SELECT {col} AS k, count(*) c FROM {table} GROUP BY {col}")
    return {r["k"]: r["c"] for r in rows}
