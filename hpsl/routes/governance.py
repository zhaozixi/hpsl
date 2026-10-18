"""M10 治理度量 REST 端点（消费 M9 compute_counts / plane_signals）。

- `GET /api/v1/counts`：四平面计数指标全量（可重算，天然幂等）。
- `GET /api/v1/counts/planes`：四平面度量与阈值对比（counts + 按平面分组的软提示）。
- `GET /api/v1/soft-prompts`：当前活跃软提示（可关闭）。
- `POST /api/v1/soft-prompts/{id}/dismiss`：关闭软提示（持久化于项目 config，零 DDL）。

纪律：计数型只读（无 db.write）；dismiss 经 set_config 唯一写入口；零 DDL；复用 M9 内部 API 与 M1 安全依赖。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Depends

from .. import projects as proj
from ..common.errors import BizError
from ..common.result import ok
from ..engine import governance
from ..engine.governance import compute_counts, counts_context, plane_signals
from .security import read_security, write_security

api_router = APIRouter(prefix="/api/v1")


def _active_pid(req: Request) -> str:
    """解析激活项目 id；未激活 → A010001（与 M1/M8 一致的业务语义失败）。"""
    active = proj.active_project()
    if active is None:
        raise BizError("A010001", msg="尚未选择项目，请先在首页选择或新建项目")
    return active.id


def _dismissed_set(pid: str) -> set[str]:
    """返回该项目已关闭软提示 id 集合（持久化于 project.config.dismissed_prompts）。"""
    return set(proj.get_raw_config(pid).get("dismissed_prompts", []))


def _active_signals(pid: str):
    """返回过滤掉已关闭项的活跃软提示（SoftPromptVO 列表）。"""
    counts = compute_counts(pid)
    cfg = proj.get_effective_config(pid)
    ctx = counts_context(pid)
    signals = plane_signals(counts, cfg, ctx=ctx)
    dismissed = _dismissed_set(pid)
    return [s for s in signals if s.id not in dismissed]


# ---------------------------------------------------------------------------
# 计数型指标全量（§3.2.M9.2）
# ---------------------------------------------------------------------------
@api_router.get("/counts", dependencies=[Depends(read_security)])
def counts_api(req: Request):
    pid = _active_pid(req)
    return ok(compute_counts(pid).to_dict())


# ---------------------------------------------------------------------------
# 四平面度量与阈值对比（§3.2.M9.2）
# ---------------------------------------------------------------------------
@api_router.get("/counts/planes", dependencies=[Depends(read_security)])
def planes_api(req: Request):
    pid = _active_pid(req)
    counts = compute_counts(pid)
    cfg = proj.get_effective_config(pid)
    ctx = counts_context(pid)
    signals = plane_signals(counts, cfg, ctx=ctx)
    signals_by_plane: dict[str, list] = {}
    for s in signals:
        signals_by_plane.setdefault(s.plane, []).append(s.to_dict())
    return ok({"counts": counts.to_dict(), "signals_by_plane": signals_by_plane})


# ---------------------------------------------------------------------------
# 活跃软提示（§3.2.M9.2）
# ---------------------------------------------------------------------------
@api_router.get("/soft-prompts", dependencies=[Depends(read_security)])
def soft_prompts_api(req: Request):
    pid = _active_pid(req)
    active = _active_signals(pid)
    return ok([s.to_dict() for s in active])


# ---------------------------------------------------------------------------
# 关闭软提示（§3.2.M9.2；写请求走 write_security：Origin + CSRF + 限流）
# ---------------------------------------------------------------------------
@api_router.post("/soft-prompts/{prompt_id}/dismiss", dependencies=[Depends(write_security)])
def dismiss_api(req: Request, prompt_id: str):
    pid = _active_pid(req)
    dismissed = set(proj.get_raw_config(pid).get("dismissed_prompts", []))
    dismissed.add(prompt_id)
    proj.set_config(pid, {"dismissed_prompts": sorted(dismissed)}, actor="user")
    active = _active_signals(pid)
    return ok([s.to_dict() for s in active])
