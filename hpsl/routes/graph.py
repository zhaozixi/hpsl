"""M10 Phase B/C 第五批：卡片与概念图 UI（消费 M7 card / concept / concept_edge）。

- GET /cards：卡片列表页（卡片 my_words + 所属概念名 + 验证状态 + 新建概念/卡片表单）。
- GET /graph：概念图页（concept 节点 + concept_edge 连线，SVG 环形布局）。
- GET /api/v1/cards：卡片 REST（含 concept_name）。
- GET /api/v1/concepts：概念 REST。
- GET /api/v1/concept-edges：概念边 REST。
- POST /api/v1/concepts、/cards、/concept-edges：P1-05 写入（全经 db.write，actor=user）。

纪律约束：零 DDL（只写既有 concept/card/concept_edge 表，16 库对象不变）；
读接口 read_security、写接口 write_security；不新增错误码（C400001 参数/不存在、A010001 无激活）；
不直写 audit_log（由 db.write 同事务追加）；本批次不改 engine（直接 db.query/db.write）。
"""

from __future__ import annotations

import json
import math
import uuid
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


def _active_pid() -> Optional[str]:
    active = proj.active_project()
    return active.id if active is not None else None


def _require_pid() -> str:
    """校验存在激活项目；否则 A010001（与 unit 路由同构）。"""
    pid = _active_pid()
    if pid is None:
        raise BizError("A010001", msg="尚未选择项目")
    return pid


async def _parse_body(req: Request) -> dict:
    """兼容 JSON 与表单（HTMX 默认 form-urlencoded），统一成 dict。"""
    ct = req.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            body = await req.json()
        except Exception:
            raise BizError("C400001", msg="请求体需为 JSON")
        if not isinstance(body, dict):
            raise BizError("C400001", msg="请求体需为 JSON 对象")
        return body
    try:
        form = await req.form()
        return {k: v for k, v in form.items()}
    except Exception:
        return {}


def _is_htmx(req: Request) -> bool:
    return req.headers.get("hx-request") is not None


def _htmx_refresh(msg: str) -> HTMLResponse:
    """HTMX 写成功：提示片段 + HX-Refresh 触发整页刷新。"""
    return HTMLResponse(f"<p class='ok'>{msg}</p>", headers={"HX-Refresh": "true"})



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
    concepts = db.query(pid, "SELECT id,name FROM concept ORDER BY name COLLATE NOCASE ASC")
    return _TEMPLATES.TemplateResponse(
        request=req,
        name="cards.html",
        context={"project": active.to_dict(), "cards": cards, "concepts": concepts},
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


# ---------------------------------------------------------------------------
# P1-05 写入：概念 / 卡片 / 概念边（零 DDL，全经 db.write，actor=user）
# ---------------------------------------------------------------------------
@api_router.post("/concepts", dependencies=[Depends(write_security)])
async def create_concept_api(req: Request):
    """新建概念：name 1~100 字符，同名（NOCASE）→ C400001（先查后写）。"""
    pid = _require_pid()
    data = await _parse_body(req)
    name = data.get("name")
    if not isinstance(name, str):
        raise BizError("C400001", msg="name 必填（1~100 字符）")
    name = name.strip()
    if not name or len(name) > 100:
        raise BizError("C400001", msg="name 须为 1~100 字符")
    if db.query(pid, "SELECT id FROM concept WHERE name=? COLLATE NOCASE", (name,)):
        raise BizError("C400001", msg="同名概念已存在")
    cid = uuid.uuid4().hex
    db.write(
        pid,
        "concept",
        {"id": cid, "name": name, "verify_status": "未验证", "last_verified_at": None},
        "user",
    )
    payload = {"id": cid, "name": name, "verify_status": "未验证"}
    if _is_htmx(req):
        return _htmx_refresh("已创建概念，正在刷新…")
    return ok(payload)


@api_router.post("/cards", dependencies=[Depends(write_security)])
async def create_card_api(req: Request):
    """新建卡片：concept 必须存在；my_words 1~500；links 为 null 或 dict/list。"""
    pid = _require_pid()
    data = await _parse_body(req)
    concept_id = data.get("concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        raise BizError("C400001", msg="concept_id 必填")
    concept_id = concept_id.strip()
    if not db.query(pid, "SELECT id FROM concept WHERE id=?", (concept_id,)):
        raise BizError("C400001", msg="concept_id 对应的概念不存在")
    my_words = data.get("my_words")
    if not isinstance(my_words, str):
        raise BizError("C400001", msg="my_words 必填（1~500 字符）")
    my_words = my_words.strip()
    if not my_words or len(my_words) > 500:
        raise BizError("C400001", msg="my_words 须为 1~500 字符")
    links = data.get("links")
    if links is not None and not isinstance(links, (dict, list)):
        raise BizError("C400001", msg="links 须为对象/数组或留空")
    card_id = uuid.uuid4().hex
    db.write(
        pid,
        "card",
        {
            "id": card_id,
            "concept_id": concept_id,
            "my_words": my_words,
            "links": links,
            "verify_status": "未验证",
        },
        "user",
    )
    payload = {
        "id": card_id,
        "concept_id": concept_id,
        "my_words": my_words,
        "links": links,
        "verify_status": "未验证",
    }
    if _is_htmx(req):
        return _htmx_refresh("已创建卡片，正在刷新…")
    return ok(payload)


@api_router.post("/concept-edges", dependencies=[Depends(write_security)])
async def create_concept_edge_api(req: Request):
    """新建概念边：两端概念存在且不同；kind 1~20；重复边先查重 → C400001。"""
    pid = _require_pid()
    data = await _parse_body(req)
    src = data.get("src")
    dst = data.get("dst")
    kind = data.get("kind")
    if not isinstance(src, str) or not src.strip():
        raise BizError("C400001", msg="src 必填（概念 id）")
    if not isinstance(dst, str) or not dst.strip():
        raise BizError("C400001", msg="dst 必填（概念 id）")
    if not isinstance(kind, str):
        raise BizError("C400001", msg="kind 必填（1~20 字符）")
    src, dst, kind = src.strip(), dst.strip(), kind.strip()
    if src == dst:
        raise BizError("C400001", msg="src 与 dst 不能是同一概念")
    if not kind or len(kind) > 20:
        raise BizError("C400001", msg="kind 须为 1~20 字符")
    found = {
        r["id"] for r in db.query(pid, "SELECT id FROM concept WHERE id IN (?,?)", (src, dst))
    }
    if src not in found or dst not in found:
        raise BizError("C400001", msg="src 或 dst 对应的概念不存在")
    if db.query(
        pid,
        "SELECT id FROM concept_edge WHERE src=? AND dst=? AND kind=?",
        (src, dst, kind),
    ):
        raise BizError("C400001", msg="重复的概念边已存在")
    edge_id = uuid.uuid4().hex
    db.write(pid, "concept_edge", {"id": edge_id, "src": src, "dst": dst, "kind": kind}, "user")
    payload = {"id": edge_id, "src": src, "dst": dst, "kind": kind}
    if _is_htmx(req):
        return _htmx_refresh("已创建概念边，正在刷新…")
    return ok(payload)


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
