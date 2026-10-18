"""验证状态衰减（P2-06 / F14.1）：只读计算，不写库、零 DDL。

背景：`concept.last_verified_at` 在复测通过时写入，但验证状态（已验证 / 待验证 / 未验证）
此前只由写路径决定，时间过去后不会「自动变旧」。本模块按复测间隔（2 / 7 / 21 天）在**读时**
重算有效状态，供页面 / REST 展示：

- effective_status(last_verified_at, now=None)：无时间 → 未验证；≤2 天 → 已验证；
  ≤7 天 → 待验证；>7 天 → 未验证（21 天为复测链最后一档，超过 7 天即已失活）。
- decayed_concepts(pid, now=None)：读取 concept 表并追加 effective_status / days 派生字段。

纪律（冻结）：本模块仅 SELECT；不调用 db.write、不改 concept.verify_status 落库值，
因此 16 个库对象与既有数据均不变（零 DDL）；不依赖 routes/app，保持 headless。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

from .. import db

# 复测间隔（天），与 review_queue.REVIEW_INTERVALS 同源口径
REVIEW_INTERVALS = (2, 7, 21)

# 有效验证状态（与 concept.verify_status 枚举同字面量）
STATUS_VERIFIED = "已验证"
STATUS_PENDING = "待验证"
STATUS_UNVERIFIED = "未验证"


def _parse_ts(value: Any) -> Optional[datetime]:
    """解析 ISO-8601 时间文本 / datetime；无法解析或为空 → None。

    - 兼容 db._now_utc 生成的 `...Z` 结尾文本；
    - 无时区信息按 UTC 处理（库内时间统一为 UTC）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now(now: Any = None) -> datetime:
    """归一化「当前时间」：缺省 / 解析失败 → 当前 UTC；允许注入固定时间便于测试。"""
    dt = _parse_ts(now) if now is not None else None
    return dt if dt is not None else datetime.now(timezone.utc)


def elapsed_days(last_verified_at: Any, now: Any = None) -> Optional[int]:
    """距上次验证的整天数（向上取整，使边界判定与展示一致）；无时间 → None。

    向上取整的取舍：2 天 1 小时 → 3 天（待验证），避免出现「显示 2 天但状态是待验证」的
    自相矛盾展示；未来时间（负数）按 ≤2 天处理，视为刚验证过。
    """
    then = _parse_ts(last_verified_at)
    if then is None:
        return None
    delta_days = (_now(now) - then).total_seconds() / 86400.0
    return int(math.ceil(delta_days))


def effective_status(last_verified_at: Any, now: Any = None) -> str:
    """读时计算有效验证状态（只读，不写库）。

    - 无 `last_verified_at`（或不可解析）→ 未验证；
    - ≤ REVIEW_INTERVALS[0]（2 天）→ 已验证；
    - ≤ REVIEW_INTERVALS[1]（7 天）→ 待验证；
    - 超过 7 天 → 未验证。
    """
    days = elapsed_days(last_verified_at, now)
    if days is None:
        return STATUS_UNVERIFIED
    if days <= REVIEW_INTERVALS[0]:
        return STATUS_VERIFIED
    if days <= REVIEW_INTERVALS[1]:
        return STATUS_PENDING
    return STATUS_UNVERIFIED


def decayed_concepts(pid: str, now: Any = None) -> list[dict]:
    """列出项目全部概念及其「读时」衰减状态（只读，零 DDL）。

    返回 list[dict]，每项：
    - id / name / verify_status / last_verified_at：concept 原始列（verify_status 为落库值）；
    - effective_status：按 REVIEW_INTERVALS 重算的有效状态；
    - days：距上次验证的整天数（向上取整；无时间 → None）。
    """
    rows = db.query(
        pid,
        "SELECT id, name, verify_status, last_verified_at FROM concept "
        "ORDER BY name ASC, id ASC",
    )
    now_dt = _now(now)
    out: list[dict] = []
    for r in rows:
        last = r.get("last_verified_at")
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "verify_status": r.get("verify_status"),
                "last_verified_at": last,
                "effective_status": effective_status(last, now_dt),
                "days": elapsed_days(last, now_dt),
            }
        )
    return out
