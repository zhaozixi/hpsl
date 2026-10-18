"""治理度量（BC-7 支撑域，L3 治理层规则层；M9）。

职责（§3.2.M9）：
- compute_counts(project_id) -> CountsVO：从账本只读聚合重算 13 项四平面计数指标。
  - 指标是可重算视图（非账本）；确定性规则，LLM 不参与（不打分、不裁决）。
  - 口径（§3.2.M9.5 Step 1）：强行闭环单元不计入通过率；复测权重高于当场作答；
    零缺口占比分母只算 gaps_confirmed=1 会话；孤岛 / 分歧读 concept_edge / claim_check。
- plane_signals(counts, cfg) -> list[SoftPromptVO]：四平面阈值比对生成软提示，
  优先级 风险 > 质量 > 节奏 > 进度；每会话提示上限可配；样本不足（分母为 0）不触发升降建议。
- counts_context(project_id)：返回样本基数（attempts / confirmed_sessions 等），供 plane_signals
  与 suggest_band 的样本不足守卫使用（避免「无数据却误报偏低」）。
- suggest_band(counts, cfg, ctx) -> dict：按「风险 > 质量 > 节奏 > 进度」优先级裁决难度带
  （P2-05 / O3），返回 {"band": up|hold|down, "reason", "plane"}；仅为建议，用户可忽略。

纪律（冻结）：全部只读，零 DDL，不依赖 routes/app（保持 headless，便于 M10 接入与测试）；
复用 db.write 唯一写入入口（本模块无写入）；M0 锁定的 16 库对象不被新增 / 改动。
"""

from __future__ import annotations

import json
from typing import Optional

from .. import db
from ..common.dto import ConfigVO, CountsVO, SoftPromptVO
from ..common.enums import AttemptKind
from ..projects import active_project

# 四平面优先级（裁决顺序，与 §3.2.M9.5 状态机迁移表一致）
_PLANES = ("风险", "质量", "节奏", "进度")

# gap 清账口径：已补 / 已验证 视为已清账
_GAP_CLEARED = ("已补", "已验证")

# 默认每会话软提示上限（可在 plane_signals 经 max_prompts 覆盖；M10 展示层可按会话再收敛）
_DEFAULT_MAX_PROMPTS = 8


def _providers_of(row: dict) -> dict:
    """claim_check.providers 为 JSON 文本列（db.query 不自动解析），按需解码。"""
    p = row.get("providers")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except json.JSONDecodeError:
            return {}
    return p or {}


def _resolve_pid(project_id: Optional[str]) -> str:
    """内部 API 解析项目 id（§3.2.M9.2 签名含 project_id；无参时回退激活项目上下文）。

    纪律：始终在「激活项目」上下文中运行（与 M8 对齐）。缺失且未激活 → ValueError（不新增错误码）。
    """
    if project_id:
        return project_id
    active = active_project()
    if active is None:
        raise ValueError("compute_counts 需要 project_id 或已激活项目（projects.set_active）")
    return active.id


def _load_cfg(pid: str) -> ConfigVO:
    """读取项目配置归一为 ConfigVO（仅读 project.config，零 DDL）。"""
    rows = db.query(pid, "SELECT config FROM project WHERE id=?", (pid,))
    raw = rows[0]["config"] if rows else None
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return ConfigVO.from_dict(data)


def _aggregate(project_id: Optional[str]):
    """只读聚合：返回 (CountsVO, ctx)。ctx 携带样本基数供 plane_signals 守卫。

    纯 SELECT，不开事务；多次 db.query 各自走只读连接（WAL 读不阻塞写）。
    """
    pid = _resolve_pid(project_id)
    cfg = _load_cfg(pid)

    units = db.query(pid, "SELECT id, forced_close, skipped_produce, closed_at FROM unit")
    attempts = db.query(
        pid, "SELECT unit_id, kind, self_score, confirmed_score FROM attempt"
    )
    gaps = db.query(pid, "SELECT unit_id, session_id, level, status FROM gap")
    sessions = db.query(pid, "SELECT id, unit_id, gaps_confirmed FROM session")
    concepts = db.query(pid, "SELECT id FROM concept")
    edges = db.query(pid, "SELECT src, dst FROM concept_edge")
    claim_checks = db.query(pid, "SELECT * FROM claim_check")

    forced_unit_ids = {u["id"] for u in units if u.get("forced_close") == 1}
    total_units = len(units)

    # ---- 质量平面：通过率 / 复测通过率 / 校准差距（剔除强行闭环单元） ----
    pass_w = 0.0
    pass_w_pass = 0.0
    review_w = 0.0
    review_w_pass = 0.0
    calib_diffs: list[float] = []
    attempts_scored = 0

    for a in attempts:
        if a["unit_id"] in forced_unit_ids:
            continue
        kind = a["kind"]
        cs = a["confirmed_score"]
        if cs is None:
            continue
        attempts_scored += 1
        # pass_rate：重述 + 边界 + 复测（复测权重 2，高于当场作答）
        if kind in (
            AttemptKind.RESTATE.value,
            AttemptKind.BOUNDARY.value,
            AttemptKind.RETEST.value,
        ):
            w = 2.0 if kind == AttemptKind.RETEST.value else 1.0
            passed = 1.0 if cs >= cfg.pass_threshold else 0.0
            pass_w += w
            pass_w_pass += w * passed
        # review_pass_rate：仅复测题
        if kind == AttemptKind.RETEST.value:
            review_w += 1.0
            review_w_pass += 1.0 if cs >= cfg.pass_threshold else 0.0
        # calibration_gap：两类分数齐全即可（含全部尝试，含复测）
        ss = a["self_score"]
        if ss is not None and cs is not None:
            calib_diffs.append(float(ss) - float(cs))

    pass_rate = (pass_w_pass / pass_w) if pass_w > 0 else 0.0
    review_pass_rate = (review_w_pass / review_w) if review_w > 0 else 0.0
    calibration_gap = (sum(calib_diffs) / len(calib_diffs)) if calib_diffs else 0.0

    # ---- 节奏平面：一级缺口超上限会话数 / 零缺口会话占比 ----
    l1_by_session: dict[str, int] = {}
    unit_gap_count: dict[str, int] = {}
    for g in gaps:
        if g["level"] == 1 and g["session_id"]:
            sid = g["session_id"]
            l1_by_session[sid] = l1_by_session.get(sid, 0) + 1
        uid = g["unit_id"]
        unit_gap_count[uid] = unit_gap_count.get(uid, 0) + 1

    l1_gap_sessions = sum(1 for c in l1_by_session.values() if c > cfg.l1_gap_limit)

    # zero_gap_ratio：分母只算 gaps_confirmed=1 会话；分子 = 其归属单元零缺口
    confirmed_sessions = [s for s in sessions if s.get("gaps_confirmed") == 1]
    zero_gap_num = 0
    for s in confirmed_sessions:
        if unit_gap_count.get(s["unit_id"], 0) == 0:
            zero_gap_num += 1
    zero_gap_ratio = (
        (zero_gap_num / len(confirmed_sessions)) if confirmed_sessions else 0.0
    )

    # ---- 进度平面：单元完成率 / 缺口清账率 / 二级缺口积压 ----
    closed_ok = sum(
        1 for u in units
        if u.get("closed_at") is not None and u.get("forced_close") != 1
    )
    unit_completion = (closed_ok / total_units) if total_units > 0 else 0.0

    total_gaps = len(gaps)
    cleared = sum(1 for g in gaps if g["status"] in _GAP_CLEARED)
    gap_clear_ratio = (cleared / total_gaps) if total_gaps > 0 else 0.0
    l2_gap_backlog = sum(
        1 for g in gaps if g["level"] == 2 and g["status"] == "待补"
    )

    # ---- 风险平面：强行闭环 / 跳过产出 / 未裁决分歧 / 孤岛节点 ----
    forced_count = sum(1 for u in units if u.get("forced_close") == 1)
    forced_close_ratio = (forced_count / total_units) if total_units > 0 else 0.0
    skipped_count = sum(1 for u in units if u.get("skipped_produce") == 1)
    skipped_produce_ratio = (skipped_count / total_units) if total_units > 0 else 0.0

    # 未裁决实质分歧：真实挑错（非同源降级告警）且 user_ruling 为空
    unresolved_disagreements = 0
    for cc in claim_checks:
        if cc.get("user_ruling") is not None:
            continue
        if _providers_of(cc).get("same_source"):
            continue
        unresolved_disagreements += 1

    # 孤岛节点：无出入边（src / dst）的概念
    concept_ids = {c["id"] for c in concepts}
    neighbors: set[str] = set()
    for e in edges:
        neighbors.add(e["src"])
        neighbors.add(e["dst"])
    isolated = sum(1 for cid in concept_ids if cid not in neighbors)
    isolated_node_ratio = (isolated / len(concept_ids)) if concept_ids else 0.0

    counts = CountsVO(
        pass_rate=pass_rate,
        review_pass_rate=review_pass_rate,
        calibration_gap=calibration_gap,
        l1_gap_sessions=l1_gap_sessions,
        zero_gap_ratio=zero_gap_ratio,
        unit_completion=unit_completion,
        gap_clear_ratio=gap_clear_ratio,
        l2_gap_backlog=l2_gap_backlog,
        forced_close_ratio=forced_close_ratio,
        skipped_produce_ratio=skipped_produce_ratio,
        unresolved_disagreements=unresolved_disagreements,
        isolated_node_ratio=isolated_node_ratio,
    )
    ctx = {
        "attempts": attempts_scored,
        "calibration_samples": len(calib_diffs),
        "confirmed_sessions": len(confirmed_sessions),
        "total_units": total_units,
        "total_concepts": len(concept_ids),
        "total_gaps": total_gaps,
    }
    return counts, ctx


def compute_counts(project_id: Optional[str] = None) -> CountsVO:
    """从账本实时重算全部四平面计数指标（§3.2.M9.2 内部 API）。

    只读视图：不写任何业务表 / 审计；零 DDL。
    """
    counts, _ = _aggregate(project_id)
    return counts


def counts_context(project_id: Optional[str] = None) -> dict:
    """样本基数上下文（供 plane_signals 守卫「无数据不误报」）。"""
    _, ctx = _aggregate(project_id)
    return ctx


def plane_signals(
    counts: CountsVO,
    cfg: ConfigVO,
    *,
    ctx: Optional[dict] = None,
    max_prompts: Optional[int] = _DEFAULT_MAX_PROMPTS,
) -> list[SoftPromptVO]:
    """四平面阈值比对，生成治理软提示（§3.2.M9.5 Step 2）。

    优先级：风险 > 质量 > 节奏 > 进度（按此顺序追加，截断时保留高优先级）。
    - 全部软提示 dismissible=True（可一键关闭）。
    - 样本不足守卫（ctx 提供的基数）：避免「无数据却误报偏低」。
    - 升降难度建议（suggest_band）属 v1，本期待留；故不输出 up / down 文案。

    返回 list[SoftPromptVO]（天然幂等：id 按「平面-信号类型」稳定生成）。
    """
    ctx = ctx or {}
    prompts: list[SoftPromptVO] = []

    # ===== 风险平面（优先级 1） =====
    if counts.unresolved_disagreements > 0:
        prompts.append(SoftPromptVO(
            id="risk-unresolved-disagreements",
            plane="风险",
            level="warn",
            message=(
                f"有 {counts.unresolved_disagreements} 处挑错结论尚未裁决，"
                f"建议抽空逐条确认「采纳」或「驳回」。"
            ),
        ))
    if counts.forced_close_ratio > cfg.forced_close_max:
        pct = round(counts.forced_close_ratio * 100, 1)
        prompts.append(SoftPromptVO(
            id="risk-forced-close",
            plane="风险",
            level="warn",
            message=(
                f"强行闭环的单元占比 {pct}%，已超过安全线 {int(cfg.forced_close_max * 100)}%，"
                f"请确认这些单元确已达标。"
            ),
        ))
    if counts.skipped_produce_ratio > cfg.skipped_produce_max:
        prompts.append(SoftPromptVO(
            id="risk-skipped-produce",
            plane="风险",
            level="warn",
            message=(
                "存在跳过产出的单元；跳过只应在确有理由时使用，请复核是否滥用。"
            ),
        ))
    if counts.isolated_node_ratio > 0.0:
        pct = round(counts.isolated_node_ratio * 100, 1)
        lvl = "warn" if counts.isolated_node_ratio > 0.2 else "info"
        prompts.append(SoftPromptVO(
            id="risk-isolated-nodes",
            plane="风险",
            level=lvl,
            message=(
                f"概念图谱中约 {pct}% 的节点尚无任何关联，建议补充连线或合并孤立概念。"
            ),
        ))

    # ===== 质量平面（优先级 2） =====
    if ctx.get("attempts", 0) > 0:
        if counts.pass_rate > cfg.pass_hi:
            pct = round(counts.pass_rate * 100, 1)
            prompts.append(SoftPromptVO(
                id="quality-pass-high",
                plane="质量",
                level="info",
                message=(
                    f"当前通过率 {pct}%，偏高；可适当上调难度，给自己更大挑战。"
                ),
            ))
        elif counts.pass_rate < cfg.pass_lo:
            pct = round(counts.pass_rate * 100, 1)
            prompts.append(SoftPromptVO(
                id="quality-pass-low",
                plane="质量",
                level="warn",
                message=(
                    f"当前通过率 {pct}%，偏低；建议先巩固前置概念再推进。"
                ),
            ))
    if ctx.get("calibration_samples", 0) > 0 and abs(counts.calibration_gap) >= 0.2:
        direction = "高估" if counts.calibration_gap > 0 else "低估"
        prompts.append(SoftPromptVO(
            id="quality-calibration",
            plane="质量",
            level="info",
            message=(
                f"你倾向于{direction}自己的掌握程度"
                f"（平均差 {counts.calibration_gap:.2f} 分），判分时尽量客观。"
            ),
        ))

    # ===== 节奏平面（优先级 3） =====
    if counts.l1_gap_sessions > 0:
        prompts.append(SoftPromptVO(
            id="pace-l1-gap-sessions",
            plane="节奏",
            level="warn",
            message=(
                f"有 {counts.l1_gap_sessions} 个学习会话的一级缺口超过上限"
                f"（{cfg.l1_gap_limit}），建议降低单节强度或拆分主题。"
            ),
        ))
    if ctx.get("confirmed_sessions", 0) > 0 and counts.zero_gap_ratio > cfg.zero_gap_max:
        pct = round(counts.zero_gap_ratio * 100, 1)
        prompts.append(SoftPromptVO(
            id="pace-zero-gap",
            plane="节奏",
            level="info",
            message=(
                f"零缺口会话占比 {pct}%，若长期偏高，可能遗漏了真实缺口"
                f"（从未确认缺口）。"
            ),
        ))

    # ===== 进度平面（优先级 4，最低） =====
    if counts.l2_gap_backlog > 0 and counts.gap_clear_ratio < 0.5:
        pct = round(counts.gap_clear_ratio * 100, 1)
        prompts.append(SoftPromptVO(
            id="progress-gap-backlog",
            plane="进度",
            level="info",
            message=(
                f"缺口清账率 {pct}%，仍有 {counts.l2_gap_backlog} 条二级缺口待补，"
                f"建议安排时间集中补齐。"
            ),
        ))

    if max_prompts is not None and len(prompts) > max_prompts:
        prompts = prompts[:max_prompts]
    return prompts


# ---------------------------------------------------------------------------
# 难度建议（P2-05 / O3）：严格按 风险 > 质量 > 节奏 > 进度 的论文优先级裁决
# ---------------------------------------------------------------------------
def _band(band: str, plane: str, reason: str) -> dict:
    """统一构造建议结果（band 仅 up / hold / down；plane 仅四平面之一）。"""
    return {"band": band, "reason": reason, "plane": plane}


def suggest_band(
    counts: CountsVO,
    cfg: ConfigVO,
    ctx: Optional[dict] = None,
) -> dict:
    """难度升降建议（§3.2.M9；P2-05）。

    判定顺序严格按治理平面优先级：**风险 > 质量 > 节奏 > 进度**（论文 §4 冲突裁决表）。
    每个平面内按「先否决、后建议」排列；首个命中的规则即返回，不做加权打分。

    规则（阈值取自 ConfigVO，默认值见 §9.3）：
    - 风险：未裁决分歧 > 0 → down（回退一手文献）；跳过产出占比 > skipped_produce_max → down；
      孤岛率 > 0.2 → down。
    - 质量：pass_rate > pass_hi → up；pass_rate < pass_lo → down（样本不足守卫）。
    - 节奏：l1_gap_sessions > 0 → down；zero_gap_ratio > zero_gap_max → up（已确认会话守卫）。
    - 进度：gap_clear_ratio < 0.5 且 l2_gap_backlog > 0 → hold；否则 hold「处于目标区间」。

    样本不足守卫（§3.2.M9.5）：`ctx` 为 counts_context 返回的样本基数；
    - 显式传入 ctx 时：attempts == 0 不判质量、confirmed_sessions == 0 不判节奏，避免
      「无数据却建议降难度」；
    - ctx 缺省（None）时：质量三项指标全为 0 视为「无数据」（空项目）同样不判质量；
      其余情况视为调用方已自行确保样本充分（便于纯函数复用）。

    返回 dict：{"band": "up" | "hold" | "down", "reason": 人读文案, "plane": 四平面之一}。
    纯函数、只读：不写库、不依赖 routes/app，零 DDL。
    """
    explicit_ctx = ctx is not None
    base = ctx or {}
    if explicit_ctx:
        attempts_ok = base.get("attempts", 0) > 0
        sessions_ok = base.get("confirmed_sessions", 0) > 0
    else:
        # 无 ctx：把「质量三项全 0」视同空项目（无样本），避免空账本误报 down
        has_quality_data = (
            counts.pass_rate != 0.0
            or counts.review_pass_rate != 0.0
            or counts.calibration_gap != 0.0
        )
        attempts_ok = has_quality_data
        sessions_ok = True

    # ===== 风险平面（优先级 1，最先判定：先保安全，再谈升降） =====
    if counts.unresolved_disagreements > 0:
        return _band(
            "down",
            "风险",
            f"有 {counts.unresolved_disagreements} 处挑错结论尚未裁决，先回退一手文献核对",
        )
    if counts.skipped_produce_ratio > cfg.skipped_produce_max:
        return _band("down", "风险", "存在跳过产出的单元，先补齐单元产出再谈难度")
    if counts.isolated_node_ratio > 0.2:
        pct = round(counts.isolated_node_ratio * 100, 1)
        return _band(
            "down", "风险", f"孤岛概念占比 {pct}%，超过 20% 安全线，先安排概念整合"
        )

    # ===== 质量平面（优先级 2） =====
    if attempts_ok and counts.pass_rate > cfg.pass_hi:
        pct = round(counts.pass_rate * 100, 1)
        hi = int(cfg.pass_hi * 100)
        return _band(
            "up", "质量", f"通过率 {pct}%，高于目标区间上限 {hi}%，可以上调难度"
        )
    if attempts_ok and counts.pass_rate < cfg.pass_lo:
        pct = round(counts.pass_rate * 100, 1)
        lo = int(cfg.pass_lo * 100)
        return _band(
            "down", "质量", f"通过率 {pct}%，低于目标区间下限 {lo}%，先巩固前置概念"
        )

    # ===== 节奏平面（优先级 3） =====
    if counts.l1_gap_sessions > 0:
        return _band(
            "down",
            "节奏",
            f"有 {counts.l1_gap_sessions} 个会话的一级缺口超过上限"
            f"（{cfg.l1_gap_limit}），先降低单节强度",
        )
    if sessions_ok and counts.zero_gap_ratio > cfg.zero_gap_max:
        pct = round(counts.zero_gap_ratio * 100, 1)
        return _band(
            "up",
            "节奏",
            f"零缺口会话占比 {pct}%，说明练习强度可能偏轻，可以上调难度",
        )

    # ===== 进度平面（优先级 4，最低：仅在前三平面无信号时参考） =====
    if counts.gap_clear_ratio < 0.5 and counts.l2_gap_backlog > 0:
        pct = round(counts.gap_clear_ratio * 100, 1)
        return _band(
            "hold",
            "进度",
            f"缺口清账率 {pct}%，仍有 {counts.l2_gap_backlog} 条二级缺口待补，先清账再谈升降",
        )
    return _band("hold", "进度", "处于目标区间")
