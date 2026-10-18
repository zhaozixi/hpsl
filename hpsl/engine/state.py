"""单元状态机引擎（规格 §4.1 / M2 实现规格）。

职责：
- TRANSITIONS：严格对齐基线 §3.2.M2.5 迁移表（规格 §4.1）。
- move()：① 冲突/乐观锁校验（cur 与库内 status 不符 → A020001，无写账）；
         ② 转移合法性（不在 TRANSITIONS[cur] → A020001）；
         ③ PRODUCE 闸门（进入 VERIFY 须 production 非空，否则须 reason → 置 skipped_produce=1 放行，
            无 reason → A020003；status 不变、无审计）；
         ④ 状态落账经 db.write（同事务审计）；
         ⑤ 副作用 LEARN→PICK 记 degraded_from=difficulty_band + degrade_count+=1；
         ⑥ 进入 VERIFY 且 production 非空时写 produced_at。
- 所有写只走 db.write；业务代码不直写 audit_log。
- 不依赖 routes/app，保持 headless。

纪律（冻结）：不引入 ORM；值一律参数绑定；表名/列名只允许来自 db.TABLES 白名单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..common.enums import UnitState
from ..common.errors import BizError
from .. import db


# ---------------------------------------------------------------------------
# 状态机迁移表（逐字对齐基线 §3.2.M2.5 / 规格 §4.1）
# 用有序元组而非 set，以严格保留规格迁移表的先后顺序（供 allowed_next 渲染顺序稳定）。
# ---------------------------------------------------------------------------
TRANSITIONS: dict[UnitState, tuple[UnitState, ...]] = {
    UnitState.INIT: (UnitState.PICK,),
    UnitState.PICK: (UnitState.LEARN,),
    UnitState.LEARN: (UnitState.PRODUCE, UnitState.VERIFY, UnitState.PICK),
    UnitState.PRODUCE: (UnitState.VERIFY, UnitState.LEARN),
    UnitState.VERIFY: (UnitState.JUDGE, UnitState.LEARN),
    UnitState.JUDGE: (UnitState.INTEGRATE, UnitState.LEARN),
    UnitState.INTEGRATE: (UnitState.PICK, UnitState.CLOSED),
    UnitState.CLOSED: (),  # terminal：无出边
}


def _as_state(value) -> UnitState:
    """接受 UnitState 枚举或字符串（"PICK" / "INIT" 均可），归一为 UnitState。"""
    if isinstance(value, UnitState):
        return value
    if isinstance(value, str):
        try:
            return UnitState[value]
        except KeyError:
            return UnitState(value)
    raise TypeError(f"无法识别的单元状态：{value!r}")


@dataclass
class UnitStateVO:
    """单元状态视图对象（含 allowed_next，供后续 Web 渲染）。"""

    status: str
    allowed_next: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "allowed_next": list(self.allowed_next)}


def move(
    pid: str,
    uid: str,
    cur,
    nxt,
    *,
    reason: Optional[str] = None,
    actor: str = "user",
    expected_version: Optional[str] = None,
) -> UnitStateVO:
    """推进单元状态机一步。

    参数：
    - pid / uid：项目与单元标识。
    - cur：调用方认为的当前状态（UnitState 或字符串）；与库内实际 status 不一致视为冲突。
    - nxt：目标状态（UnitState 或字符串）。
    - reason：进入 VERIFY 但 production 为空时的跳过理由；不填且 production 为空 → A020003。
    - expected_version：乐观锁哨兵（期望的当前 status 字符串）；
                       给定且与库内实际 status 不符 → 冲突，拒绝写账（无审计）。
    - actor：审计 actor（user/clerk/system）。

    返回：UnitStateVO（status + allowed_next）。

    异常（均无写账、无审计）：
    - A020001：状态冲突 / 非法转移。
    - A020003：进入 VERIFY 但 production 为空且无 reason。
    """
    cur_state = _as_state(cur)
    nxt_state = _as_state(nxt)

    # 读取当前 unit（只读连接）
    rows = db.query(pid, "SELECT * FROM unit WHERE id=?", (uid,))
    if not rows:
        raise BizError("A020001", msg=f"单元 {uid} 不存在，无法转移")
    unit = rows[0]
    actual_status = unit["status"]

    # 乐观锁 / 冲突校验：不一致即视为冲突，拒绝写账（status 与审计均无该转移记录）
    if expected_version is not None and str(expected_version) != actual_status:
        raise BizError(
            "A020001",
            msg=f"乐观锁冲突：期望状态 {expected_version}，实际状态 {actual_status}",
        )
    if cur_state.value != actual_status:
        raise BizError(
            "A020001",
            msg=f"状态冲突：期望 {cur_state.value}，实际 {actual_status}",
        )

    # 转移合法性
    if nxt_state not in TRANSITIONS[cur_state]:
        raise BizError("A020001", msg=f"非法状态转移：{cur_state.value} → {nxt_state.value}")

    # 组装待写入字段（id 必须，status 必改）
    update: dict = {"id": uid, "status": nxt_state.value}

    # PRODUCE 闸门：进入 VERIFY 必须 production 非空
    if nxt_state == UnitState.VERIFY:
        production = unit.get("production")
        if not production:  # 空串或 NULL 均视为未产出
            if reason:
                # 带理由跳过产出：置 skipped_produce=1 后放行（不写 produced_at）
                update["skipped_produce"] = 1
            else:
                # 未产出且无理由进入验证 → 拒绝，status 不变、无审计
                # 说明：规格 §4.1 / 任务书 §c 明确“无 reason → A020003”；
                # AC 表(§5)对 AC-M2-2 标注 A020002，二者冲突，本实现以详细接口契约为准用 A020003。
                raise BizError("A020003", msg="跳过产出需要写一句理由")
        else:
            # 正常产出：写 produced_at（ISO-8601 UTC）
            update["produced_at"] = db._now_utc()

    # 副作用：LEARN→PICK 熔断降级记账
    if cur_state == UnitState.LEARN and nxt_state == UnitState.PICK:
        update["degraded_from"] = unit.get("difficulty_band")
        update["degrade_count"] = (unit.get("degrade_count") or 0) + 1

    # 落账：经 db.write 同事务审计（审计自动附随，业务代码不直写 audit_log）
    db.write(pid, "unit", update, actor)

    return UnitStateVO(
        status=nxt_state.value,
        allowed_next=[s.value for s in TRANSITIONS[nxt_state]],
    )
