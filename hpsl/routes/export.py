"""M11 批次3：Markdown 导出（本地单机 Web 导出功能）。

- `GET  /export`（read_security）：渲染导出页；列出 5 个导出入口
  （project / units / ledger / concepts / conversations），无激活项目时显示「尚未选择项目」提示。
- `POST /api/v1/export/{kind}`（write_security）：按 kind 读取库数据，组装干净、人读的
  Markdown，写入 `HPSL_HOME/projects/{pid}/exports/{kind}-{utc}.md`，权限 0600。

纪律约束（M11 批次3 实现规格）：
- 零 DDL：仅向项目目录外部的 exports/ 写文件；只读 db.query（绝不 db.write）；
  `db.count_user_objects(pid)` 须保持 16。
- 无新错误码：仅复用 `C400001`（未知 kind）、`A080002`（未激活项目），
  存在性检查可用 `A010001`，不注册新码。
- 安全分层：页 read_security；导出 POST write_security（依赖已处理 Origin+CSRF+限流）。
- 密钥红线：导出内容不含 `hpsl.secrets` 或任何 API key；导出前对 Markdown 做显式
  scrub，剔除 `sk-` 类密钥字串、Fernet token、Cookie 等敏感片段（红线 D1 §6.5）。
- 不改 engine / state / judging / ledgers；不动 db.py 之外的任何模块。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import db, projects as proj, config as cfg
from ..common.errors import BizError
from ..common.result import ok
from .security import read_security, write_security

page_router = APIRouter()
api_router = APIRouter(prefix="/api/v1")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_EXPORT_KINDS = ("project", "units", "ledger", "concepts", "conversations")

# 对话记录：turn.role → 中文称呼（未知角色原样输出）
_ROLE_LABELS = {
    "user": "我",
    "answer_machine": "答案机",
    "questioner": "提问者",
    "boundary_setter": "出题者",
    "critic": "挑错者",
    "clerk": "书记员",
    "topic_picker": "选题助手",
}

# ---------------------------------------------------------------------------
# 密钥红线：导出前对整段 Markdown 做显式脱敏扫描（D1 §6.5）。
# 既防 config/任意字段意外夹带密钥，也满足测试「sk- not in content」的硬断言。
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),                                   # OpenAI 类 sk- 密钥
    re.compile(r"gAAAAA[A-Za-z0-9_-]{16,}"),                               # Fernet token
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{10,}"),                 # Bearer token
    re.compile(r"(?i)\b(Cookie|Set-Cookie)\s*:\s*[A-Za-z0-9=_;.\-]{8,}"),  # Cookie
]


def _scrub(text: str) -> str:
    """剔除 Markdown 中可能夹带的密钥 / token / cookie 片段，替换为 [REDACTED]。"""
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _cell(value) -> str:
    """表格单元格转义：None→占位；管道符/换行破坏表格结构则替换。"""
    if value is None:
        return "—"
    s = str(value)
    s = s.replace("|", "／").replace("\n", " ").replace("\r", " ").strip()
    return s if s else "—"


def _trunc(value, n: int = 120) -> str:
    """长文本截断预览。"""
    s = "" if value is None else str(value)
    s = s.replace("\n", " ").replace("\r", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def _role_label(role) -> str:
    """turn.role → 中文称呼；未知角色原样返回。"""
    r = "" if role is None else str(role)
    return _ROLE_LABELS.get(r, r)


def _quote_block(text) -> list[str]:
    """把一段内容格式化为 Markdown 引用块：逐行加 `> ` 前缀，避免破坏外层表格。"""
    s = "" if text is None else str(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = s.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return ["> （空）"]
    return [("> " + ln) if ln else ">" for ln in lines]


def _utc_stamp() -> str:
    """UTC 时间戳文件名分量：YYYYMMDDTHHMMSSZ。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# 各 kind 的 Markdown 组装（纯读 db.query，输出干净人读文本）
# ---------------------------------------------------------------------------
def _build_project_md(pid: str) -> str:
    rows = db.query(pid, "SELECT id, goal, initial_belief, created_at, config FROM project WHERE id=?", (pid,))
    if not rows:
        raise BizError("A010001")
    row = rows[0]
    cfg_raw = row.get("config")
    config_dict = json.loads(cfg_raw) if isinstance(cfg_raw, str) else (cfg_raw or {})
    name = config_dict.get("name", row["id"])
    goal = row.get("goal") or "—"
    belief = row.get("initial_belief") or "（未填写）"
    created = row.get("created_at") or "—"

    lines = [
        f"# 项目导出：{name}",
        "",
        f"- 项目 ID：`{row['id']}`",
        f"- 目标：{goal}",
        f"- 初始认识：{belief}",
        f"- 创建时间：{created}",
        "",
        "## 配置摘要",
        "",
        "| 配置项 | 值 |",
        "| --- | --- |",
    ]
    # 配置项逐行列出（不含密钥：config 落库的是 ConfigVO 合并字典，无 API key）。
    for k in sorted(config_dict.keys()):
        lines.append(f"| {_cell(k)} | {_cell(config_dict[k])} |")
    lines += [
        "",
        "## 导出信息",
        "",
        f"- 导出类型：project",
        f"- 导出时间（UTC）：{_now_iso()}",
        "",
    ]
    return _scrub("\n".join(lines))


def _build_units_md(pid: str) -> str:
    units = db.query(
        pid,
        "SELECT id, title, status, difficulty_band, forced_close, degrade_count, "
        "skipped_produce, close_reason, degraded_from, production, produced_at, "
        "chosen_from, closed_at FROM unit ORDER BY id ASC",
    )
    lines = [
        "# 单元导出",
        "",
        f"共 {len(units)} 个单元。",
        "",
        "| ID | 标题 | 状态 | 难度档 | 强制关闭 | 降级次数 | 跳过产出 | 关闭时间 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for u in units:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(u.get("id")),
                    _cell(u.get("title")),
                    _cell(u.get("status")),
                    _cell(u.get("difficulty_band")),
                    _cell(u.get("forced_close")),
                    _cell(u.get("degrade_count")),
                    _cell(u.get("skipped_produce")),
                    _cell(u.get("closed_at")),
                ]
            )
            + " |"
        )
    # 状态分布汇总
    status_counts: dict[str, int] = {}
    for u in units:
        st = str(u.get("status") or "未知")
        status_counts[st] = status_counts.get(st, 0) + 1
    if status_counts:
        lines += ["", "## 状态分布", ""]
        for st in sorted(status_counts):
            lines.append(f"- {st}：{status_counts[st]} 个")
    # 产出预览（逐单元，避免长文本压垮表格）
    with_prod = [u for u in units if u.get("production")]
    if with_prod:
        lines += ["", "## 产出预览", ""]
        for u in with_prod:
            lines.append(f"### {_cell(u.get('id'))} · {_cell(u.get('title'))}")
            lines.append(f"- 产出：{_trunc(u.get('production'), 200)}")
            lines.append(f"- 关闭原因：{_cell(u.get('close_reason'))}")
            lines.append(f"- 降级来源：{_cell(u.get('degraded_from'))}")
            lines.append("")
    lines += [
        "## 导出信息",
        "",
        f"- 导出类型：units",
        f"- 导出时间（UTC）：{_now_iso()}",
        "",
    ]
    return _scrub("\n".join(lines))


def _build_ledger_md(pid: str) -> str:
    """四本账摘要：audit_log + 缺口类账本（gap / gap_candidate / claim_check / review / llm_call）。"""
    audit = db.query(
        pid,
        "SELECT ts, actor, table_name, row_id, change FROM audit_log ORDER BY id DESC LIMIT 50",
    )
    audit_total = db.query(pid, "SELECT count(*) AS c FROM audit_log")
    audit_total_n = int(audit_total[0]["c"]) if audit_total else 0

    gap = db.query(pid, "SELECT status, count(*) AS c FROM gap GROUP BY status")
    gap_total = db.query(pid, "SELECT count(*) AS c FROM gap")
    gap_total_n = int(gap_total[0]["c"]) if gap_total else 0

    gap_cand = db.query(pid, "SELECT status, count(*) AS c FROM gap_candidate GROUP BY status")
    claim = db.query(
        pid,
        "SELECT "
        "SUM(CASE WHEN agree=1 THEN 1 ELSE 0 END) AS agree, "
        "SUM(CASE WHEN agree=0 THEN 1 ELSE 0 END) AS disagree, "
        "SUM(residual) AS residual, count(*) AS c FROM claim_check",
    )
    review = db.query(pid, "SELECT result, count(*) AS c FROM review GROUP BY result")
    llm = db.query(
        pid,
        "SELECT count(*) AS c, COALESCE(SUM(tokens_in),0) AS tin, "
        "COALESCE(SUM(tokens_out),0) AS tout, COALESCE(SUM(cost),0) AS cost FROM llm_call",
    )

    lines = [
        "# 四本账导出",
        "",
        "## 审计日志（audit_log）",
        "",
        f"共 {audit_total_n} 条，以下为最近 {len(audit)} 条：",
        "",
        "| 时间 | 操作者 | 表 | 行ID |",
        "| --- | --- | --- | --- |",
    ]
    for a in audit:
        lines.append(
            "| "
            + " | ".join(
                [_cell(a.get("ts")), _cell(a.get("actor")), _cell(a.get("table_name")), _cell(a.get("row_id"))]
            )
            + " |"
        )

    lines += ["", "## 缺口账本（gap）", ""]
    lines.append(f"共 {gap_total_n} 条。")
    if gap:
        lines.append("")
        lines.append("| 状态 | 数量 |")
        lines.append("| --- | --- |")
        for g in gap:
            lines.append(f"| {_cell(g.get('status'))} | {_cell(g.get('c'))} |")

    lines += ["", "## 缺口候选（gap_candidate）", ""]
    if gap_cand:
        lines.append("| 状态 | 数量 |")
        lines.append("| --- | --- |")
        for g in gap_cand:
            lines.append(f"| {_cell(g.get('status'))} | {_cell(g.get('c'))} |")
    else:
        lines.append("（无记录）")

    lines += ["", "## 主张核验（claim_check）", ""]
    if claim and claim[0].get("c"):
        c0 = claim[0]
        lines.append(f"- 共 {_cell(c0.get('c'))} 条；同意 {_cell(c0.get('agree'))} / 分歧 {_cell(c0.get('disagree'))} / 残留 {_cell(c0.get('residual'))}")
    else:
        lines.append("（无记录）")

    lines += ["", "## 复测（review）", ""]
    if review:
        lines.append("| 结果 | 数量 |")
        lines.append("| --- | --- |")
        for r in review:
            lines.append(f"| {_cell(r.get('result'))} | {_cell(r.get('c'))} |")
    else:
        lines.append("（无记录）")

    lines += ["", "## 模型调用（llm_call）", ""]
    if llm and llm[0].get("c"):
        l0 = llm[0]
        lines.append(
            f"- 共 {_cell(l0.get('c'))} 次；tokens_in 合计 {_cell(l0.get('tin'))} / "
            f"tokens_out 合计 {_cell(l0.get('tout'))} / 花费合计 {_cell(l0.get('cost'))}"
        )
    else:
        lines.append("（无记录）")

    lines += [
        "",
        "## 导出信息",
        "",
        f"- 导出类型：ledger",
        f"- 导出时间（UTC）：{_now_iso()}",
        "",
    ]
    return _scrub("\n".join(lines))


def _build_concepts_md(pid: str) -> str:
    concepts = db.query(pid, "SELECT id, name, verify_status, last_verified_at FROM concept ORDER BY id ASC")
    edges = db.query(pid, "SELECT src, dst, kind FROM concept_edge ORDER BY id ASC")

    lines = [
        "# 概念导出",
        "",
        f"## 概念（共 {len(concepts)} 个）",
        "",
        "| ID | 名称 | 验证状态 | 最近验证时间 |",
        "| --- | --- | --- | --- |",
    ]
    for c in concepts:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(c.get("id")),
                    _cell(c.get("name")),
                    _cell(c.get("verify_status")),
                    _cell(c.get("last_verified_at")),
                ]
            )
            + " |"
        )

    lines += [
        "",
        f"## 概念关系（concept_edge，共 {len(edges)} 条）",
        "",
        "| 源 | 目标 | 类型 |",
        "| --- | --- | --- |",
    ]
    for e in edges:
        lines.append(
            "| " + " | ".join([_cell(e.get("src")), _cell(e.get("dst")), _cell(e.get("kind"))]) + " |"
        )

    lines += [
        "",
        "## 导出信息",
        "",
        f"- 导出类型：concepts",
        f"- 导出时间（UTC）：{_now_iso()}",
        "",
    ]
    return _scrub("\n".join(lines))


def _build_conversations_md(pid: str) -> str:
    """对话记录：每个 session 一节；轮次 JOIN session 后按 turn.id 升序（时间序）。

    内容用引用块逐行承载并经 `_scrub()` 脱敏，避免长文本/管道符破坏表格。
    """
    sessions = db.query(
        pid,
        "SELECT id, unit_id, started_at, ended_at FROM session "
        "ORDER BY started_at ASC, id ASC",
    )
    turns = db.query(
        pid,
        "SELECT t.id, t.session_id, t.role, t.content, t.provider, t.model, t.ts "
        "FROM turn t JOIN session s ON t.session_id = s.id "
        "ORDER BY t.id ASC",
    )
    by_session: dict[str, list[dict]] = {}
    for t in turns:
        by_session.setdefault(str(t.get("session_id")), []).append(t)

    lines = ["# 对话记录导出", ""]
    if not sessions:
        lines += ["（无对话记录）", ""]
    else:
        lines += [f"共 {len(sessions)} 个会话、{len(turns)} 轮对话。", ""]
        for s in sessions:
            sid = str(s.get("id") or "—")
            started = s.get("started_at") or "—"
            lines += [f"## 会话 {sid} · {started}", ""]
            rows = by_session.get(sid, [])
            if not rows:
                lines += ["（本会话暂无对话轮次）", ""]
                continue
            for t in rows:
                provider = t.get("provider") or ""
                model = t.get("model") or ""
                meta = f"{provider}/{model}" if provider and model else (model or provider or "—")
                content = _scrub("" if t.get("content") is None else str(t.get("content")))
                lines.append(f"**{_role_label(t.get('role'))}**（{t.get('ts') or '—'}，{meta}）")
                lines.append("")
                lines += _quote_block(content)
                lines.append("")

    lines += [
        "## 导出信息",
        "",
        "- 导出类型：conversations",
        f"- 导出时间（UTC）：{_now_iso()}",
        "",
    ]
    return _scrub("\n".join(lines))


_BUILDERS = {
    "project": _build_project_md,
    "units": _build_units_md,
    "ledger": _build_ledger_md,
    "concepts": _build_concepts_md,
    "conversations": _build_conversations_md,
}


# ---------------------------------------------------------------------------
# 页面：导出
# ---------------------------------------------------------------------------
@page_router.get("/export", dependencies=[Depends(read_security)])
def export_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req,
            name="export.html",
            context={"active": None, "error": "尚未选择项目，请先在首页选择或新建项目"},
        )
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="export.html",
        context={"active": active.to_dict(), "kinds": list(_EXPORT_KINDS)},
    )


# ---------------------------------------------------------------------------
# API：导出为 Markdown 文件
# ---------------------------------------------------------------------------
@api_router.post("/export/{kind}", dependencies=[Depends(write_security)])
def export_md_api(kind: str):
    if kind not in _EXPORT_KINDS:
        raise BizError("C400001", msg=f"未知导出类型：{kind}（可用：{', '.join(_EXPORT_KINDS)}）")
    active = proj.active_project()
    if active is None:
        raise BizError("A080002")
    pid = active.id
    db.safe_path(pid)  # 防目录穿越（C400002）

    md = _BUILDERS[kind](pid)

    exports_dir = cfg.project_dir(pid) / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{kind}-{_utc_stamp()}.md"
    path = exports_dir / fname
    path.write_text(md, encoding="utf-8")
    # 权限 0600：仅属主可读写（Windows 下 chmod 部分生效，不强制校验）
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass

    return ok({"file": fname, "path": str(path), "bytes": len(md)})
