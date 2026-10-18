"""M10 Phase B/C 第五批：卡片与概念图 UI（消费 M7 card / concept / concept_edge）。

- GET /cards：卡片列表页（卡片 my_words + 所属概念名 + 验证状态）。
- GET /graph：概念图页（concept 节点 + concept_edge 连线，SVG 环形布局）。
- GET /api/v1/cards：卡片 REST（含 concept_name）。
- GET /api/v1/concepts：概念 REST。
- GET /api/v1/concept-edges：概念边 REST。

纪律约束：只读消费既有表，读接口 read_security；零 DDL；无新错误码（仅 A010001 无激活）；
本批次不改 engine（直接 db.query，与 unit 路由消费 claim_check/gap 同构）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates

from .. import db, projects as proj, config as cfg
from ..common.result import ok
from .security import read_security, write_security

page_router = APIRouter()
api_router = APIRouter(prefix="/api/v1")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _active_pid() -> Optional[str]:
    active = proj.active_project()
    return active.id if active is not None else None


# ---------------------------------------------------------------------------
# 节点布局持久化（M15）：坐标存每项目 graph_layout.json（文件级，零 DDL）。
# 与 backup 文件级 I/O 同构，不新增表/列、不触 db.write、不破 16 库对象锁。
# ---------------------------------------------------------------------------
def _layout_path(pid: str) -> Path:
    return cfg.project_dir(pid) / "graph_layout.json"


def load_graph_layout(pid: str) -> dict:
    p = _layout_path(pid)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("layout", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_graph_layout(pid: str, layout: dict) -> None:
    p = _layout_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"layout": layout}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# 页面：卡片列表
# ---------------------------------------------------------------------------
@page_router.get("/cards", dependencies=[Depends(read_security)])
def cards_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req, name="cards.html", context={"error": "尚未选择项目，请先在首页选择或新建项目"}
        )
    pid = active.id
    cards = db.query(
        pid,
        "SELECT card.*, concept.name AS concept_name FROM card "
        "LEFT JOIN concept ON card.concept_id = concept.id ORDER BY card.id ASC",
    )
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="cards.html",
        context={"project": active.to_dict(), "cards": cards},
    )


# ---------------------------------------------------------------------------
# 页面：概念图（SVG 环形布局）
# ---------------------------------------------------------------------------
@page_router.get("/graph", dependencies=[Depends(read_security)])
def graph_page(req: Request):
    active = proj.active_project()
    if active is None:
        return _TEMPLATES.TemplateResponse(
            request=req, name="graph.html", context={"error": "尚未选择项目，请先在首页选择或新建项目"}
        )
    pid = active.id
    concepts = db.query(pid, "SELECT * FROM concept ORDER BY id ASC")
    edges = db.query(pid, "SELECT * FROM concept_edge ORDER BY id ASC")

    W, H, R = 720, 520, 200
    cx, cy = W // 2, H // 2
    n = max(len(concepts), 1)
    saved_layout = load_graph_layout(pid)
    nodes = []
    for i, c in enumerate(concepts):
        ang = 2 * math.pi * i / n - math.pi / 2
        x = cx + R * math.cos(ang)
        y = cy + R * math.sin(ang)
        sv = saved_layout.get(c["id"])
        if isinstance(sv, dict):
            if "x" in sv:
                try:
                    x = float(sv["x"])
                except (TypeError, ValueError):
                    pass
            if "y" in sv:
                try:
                    y = float(sv["y"])
                except (TypeError, ValueError):
                    pass
        nodes.append({
            "id": c["id"],
            "name": c.get("name") or c["id"],
            "x": round(x, 1),
            "y": round(y, 1),
            "verify_status": c.get("verify_status") or "未验证",
        })
    pos = {nd["id"]: nd for nd in nodes}
    edge_views = []
    for e in edges:
        a, b = pos.get(e["src"]), pos.get(e["dst"])
        if a is None or b is None:
            continue
        edge_views.append({
            "src": e["src"], "dst": e["dst"], "kind": e.get("kind") or "",
            "x1": a["x"], "y1": a["y"], "x2": b["x"], "y2": b["y"],
        })
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="graph.html",
        context={
            "project": active.to_dict(),
            "concepts": concepts,
            "edges": edges,
            "nodes": nodes,
            "edge_views": edge_views,
            "svg": {"w": W, "h": H},
        },
    )


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------
@api_router.get("/cards", dependencies=[Depends(read_security)])
def cards_api():
    pid = _active_pid()
    if pid is None:
        return ok(None)
    cards = db.query(
        pid,
        "SELECT card.*, concept.name AS concept_name FROM card "
        "LEFT JOIN concept ON card.concept_id = concept.id ORDER BY card.id ASC",
    )
    return ok([dict(c) for c in cards])


@api_router.get("/concepts", dependencies=[Depends(read_security)])
def concepts_api():
    pid = _active_pid()
    if pid is None:
        return ok(None)
    return ok([dict(c) for c in db.query(pid, "SELECT * FROM concept ORDER BY id ASC")])


@api_router.get("/concept-edges", dependencies=[Depends(read_security)])
def concept_edges_api():
    pid = _active_pid()
    if pid is None:
        return ok(None)
    return ok([dict(e) for e in db.query(pid, "SELECT * FROM concept_edge ORDER BY id ASC")])


@api_router.post("/graph/layout", dependencies=[Depends(write_security)])
async def save_graph_layout_api(req: Request):
    pid = _active_pid()
    if pid is None:
        return ok(None)
    try:
        body = await req.json()
    except Exception:
        body = {}
    layout_in = body.get("layout") if isinstance(body, dict) else None
    if not isinstance(layout_in, dict):
        return ok({"saved": 0})
    valid_ids = {c["id"] for c in db.query(pid, "SELECT id FROM concept")}
    clean: dict = {}
    for cid, pos in layout_in.items():
        if cid not in valid_ids or not isinstance(pos, dict):
            continue
        try:
            x = float(pos.get("x"))
            y = float(pos.get("y"))
        except (TypeError, ValueError):
            continue
        clean[cid] = {"x": round(x, 2), "y": round(y, 2)}
    save_graph_layout(pid, clean)
    return ok({"saved": len(clean)})
