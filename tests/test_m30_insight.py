"""P2-05 / P2-06 / P2-07 验收（M30 洞察）：难度建议 + 验证衰减 + 周盘点批量裁决。

覆盖：
- suggest_band 四平面优先级（风险 > 质量 > 节奏 > 进度）+ 样本不足守卫；
- GET /api/v1/bands 返回 {counts, band} 且与引擎一致；
- 衰减边界（2 / 7 / 21 天，固定时间字符串）+ GET /api/v1/concepts/decay；
- GET /weekly 渲染 gap 文本 / 单元标题 / select[name=status] / HTMX 表单；
- POST /api/v1/gaps/status-batch：批量更新（JSON / 表单 HTMX）、非法 status → C400001、
  不存在 id 跳过、审计 actor=user；
- 零 DDL（16 库对象不变）。

路由挂载说明：按任务约束不编辑 hpsl/app.py，本文件在导入时幂等挂载 insight 的
api_router / page_router（若主代理已在 app.py 挂载则自动跳过），保证测试自足。
"""

import uuid

from hpsl import db
from hpsl import projects as proj
from hpsl.app import app
from hpsl.common.dto import ConfigVO, CountsVO
from hpsl.engine.decay import (
    REVIEW_INTERVALS,
    decayed_concepts,
    effective_status,
)
from hpsl.engine.governance import compute_counts, counts_context, suggest_band
from hpsl.routes import insight


def _mount_routers_once() -> None:
    """幂等挂载：app.py 未挂载时补挂，已挂载则跳过（不改 app.py 也能自测）。"""
    paths = {getattr(route, "path", "") for route in app.routes}
    if "/api/v1/bands" not in paths:
        app.include_router(insight.api_router)
    if "/weekly" not in paths:
        app.include_router(insight.page_router)


_mount_routers_once()


# ---------------------------------------------------------------------------
# 构造助手
# ---------------------------------------------------------------------------
def _new_project(client, csrf_headers) -> str:
    """建项目并切换为激活项目，返回 pid。"""
    name = f"P2洞察-{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/api/v1/projects", headers=csrf_headers, json={"name": name, "goal": "验收 P2-05/06/07"}
    )
    assert r.status_code == 200, r.text
    pid = r.json()["data"]["id"]
    client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers)
    return pid


def _counts(**over) -> CountsVO:
    """构造 CountsVO：默认全 0（空项目），按需覆盖单个平面指标。"""
    base = dict(
        pass_rate=0.0,
        review_pass_rate=0.0,
        calibration_gap=0.0,
        l1_gap_sessions=0,
        zero_gap_ratio=0.0,
        unit_completion=0.0,
        gap_clear_ratio=0.0,
        l2_gap_backlog=0,
        forced_close_ratio=0.0,
        skipped_produce_ratio=0.0,
        unresolved_disagreements=0,
        isolated_node_ratio=0.0,
    )
    base.update(over)
    return CountsVO(**base)


def _unit(pid, uid, title=None):
    db.write(
        pid,
        "unit",
        {"id": uid, "title": title or f"单元-{uid}", "status": "INIT"},
        "system",
    )


def _gap(pid, gid, uid, level, status="待补", text=None):
    db.write(
        pid,
        "gap",
        {
            "id": gid,
            "unit_id": uid,
            "session_id": None,
            "level": level,
            "text": text or f"缺口-{gid}",
            "status": status,
            "context": None,
            "created_at": "2026-01-01T00:00:00.000Z",
            "updated_at": None,
        },
        "system",
    )


def _concept(pid, cid, name, last_verified_at, verify_status="未验证"):
    db.write(
        pid,
        "concept",
        {
            "id": cid,
            "name": name,
            "verify_status": verify_status,
            "last_verified_at": last_verified_at,
        },
        "system",
    )


def _gap_status(pid, gid):
    rows = db.query(pid, "SELECT status FROM gap WHERE id=?", (gid,))
    return rows[0]["status"]


# ---------------------------------------------------------------------------
# P2-05：suggest_band 四平面优先级
# ---------------------------------------------------------------------------
def test_suggest_band_risk_takes_priority():
    """风险平面最先判定：即便通过率 / 零缺口占比偏高，也先 down。"""
    counts = _counts(
        pass_rate=0.95,
        zero_gap_ratio=0.9,
        unresolved_disagreements=2,
    )
    band = suggest_band(counts, ConfigVO(), ctx={"attempts": 10, "confirmed_sessions": 3})
    assert band["band"] == "down"
    assert band["plane"] == "风险"
    assert "一手文献" in band["reason"]


def test_suggest_band_risk_skipped_and_isolated():
    cfg = ConfigVO()
    b = suggest_band(_counts(pass_rate=0.95, skipped_produce_ratio=0.25), cfg, ctx={"attempts": 5})
    assert (b["band"], b["plane"]) == ("down", "风险")

    b = suggest_band(_counts(pass_rate=0.95, isolated_node_ratio=0.3), cfg, ctx={"attempts": 5})
    assert (b["band"], b["plane"]) == ("down", "风险")
    assert "孤岛" in b["reason"]


def test_suggest_band_quality_before_pace():
    """质量平面优先于节奏：有节奏信号时仍按质量给出升降。"""
    counts = _counts(pass_rate=0.9, l1_gap_sessions=2, zero_gap_ratio=0.9)
    b = suggest_band(counts, ConfigVO(), ctx={"attempts": 4, "confirmed_sessions": 2})
    assert (b["band"], b["plane"]) == ("up", "质量")

    b = suggest_band(_counts(pass_rate=0.2), ConfigVO(), ctx={"attempts": 4})
    assert (b["band"], b["plane"]) == ("down", "质量")
    assert "前置概念" in b["reason"]


def test_suggest_band_sample_guard():
    """ctx 样本不足（attempts=0）：不因 pass_rate=0 误判降难度，落到进度平面 hold。"""
    b = suggest_band(_counts(pass_rate=0.0), ConfigVO(), ctx={"attempts": 0, "confirmed_sessions": 0})
    assert b["band"] == "hold"
    assert b["plane"] == "进度"
    assert "处于目标区间" in b["reason"]


def test_suggest_band_quality_without_ctx():
    """ctx 缺省时的回退：质量指标非 0 视为有样本，全 0 视为空项目（不误报 down）。"""
    b = suggest_band(_counts(pass_rate=0.9), ConfigVO())
    assert (b["band"], b["plane"]) == ("up", "质量")

    b = suggest_band(_counts(), ConfigVO())  # 空项目：质量三项全 0
    assert (b["band"], b["plane"]) == ("hold", "进度")


def test_suggest_band_pace_rules():
    cfg = ConfigVO()
    b = suggest_band(_counts(pass_rate=0.7, l1_gap_sessions=1), cfg, ctx={"attempts": 3})
    assert (b["band"], b["plane"]) == ("down", "节奏")

    b = suggest_band(
        _counts(pass_rate=0.7, zero_gap_ratio=0.5), cfg, ctx={"attempts": 3, "confirmed_sessions": 2}
    )
    assert (b["band"], b["plane"]) == ("up", "节奏")

    # 已确认会话为 0：零缺口占比不参与判定（防「不确认缺口」操纵）
    b = suggest_band(
        _counts(pass_rate=0.7, zero_gap_ratio=0.5), cfg, ctx={"attempts": 3, "confirmed_sessions": 0}
    )
    assert b["band"] == "hold"


def test_suggest_band_progress_hold():
    b = suggest_band(
        _counts(pass_rate=0.7, gap_clear_ratio=0.2, l2_gap_backlog=3),
        ConfigVO(),
        ctx={"attempts": 3},
    )
    assert (b["band"], b["plane"]) == ("hold", "进度")
    assert "清账" in b["reason"]


# ---------------------------------------------------------------------------
# P2-05：GET /api/v1/bands
# ---------------------------------------------------------------------------
def test_bands_api_matches_engine(client, csrf_headers):
    pid = _new_project(client, csrf_headers)
    _unit(pid, "u1")
    _gap(pid, "g1", "u1", 2)
    _gap(pid, "g2", "u1", 2)

    r = client.get("/api/v1/bands")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["code"] == "0"
    data = payload["data"]
    assert set(data) == {"counts", "band"}
    assert data["counts"] == compute_counts(pid).to_dict()
    expected = suggest_band(
        compute_counts(pid), proj.get_effective_config(pid), ctx=counts_context(pid)
    )
    assert data["band"] == expected
    assert data["band"]["band"] in ("up", "hold", "down")
    assert data["band"]["plane"] in ("风险", "质量", "节奏", "进度")


# ---------------------------------------------------------------------------
# P2-06：衰减边界（2 / 7 / 21 天）
# ---------------------------------------------------------------------------
def test_effective_status_boundaries():
    assert REVIEW_INTERVALS == (2, 7, 21)
    now = "2026-10-18T00:00:00.000Z"
    assert effective_status(None, now) == "未验证"
    assert effective_status("2026-10-16T00:00:00.000Z", now) == "已验证"  # 恰好 2 天
    assert effective_status("2026-10-15T23:00:00.000Z", now) == "待验证"  # 2 天 1 小时
    assert effective_status("2026-10-11T00:00:00.000Z", now) == "待验证"  # 恰好 7 天
    assert effective_status("2026-10-10T23:00:00.000Z", now) == "未验证"  # 7 天 1 小时
    assert effective_status("2026-09-01T00:00:00.000Z", now) == "未验证"  # 远超 21 天
    assert effective_status("2026-10-16T00:00:00", now) == "已验证"  # 无时区按 UTC
    assert effective_status("不是时间", now) == "未验证"


def test_decayed_concepts_and_api(client, csrf_headers):
    pid = _new_project(client, csrf_headers)
    _concept(pid, "c1", "概念甲", "2026-10-16T00:00:00.000Z", "已验证")
    _concept(pid, "c2", "概念乙", None, "未验证")
    _concept(pid, "c3", "概念丙", "2026-10-08T00:00:00.000Z", "已验证")  # 10 天

    rows = decayed_concepts(pid, now="2026-10-18T00:00:00.000Z")
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"c1", "c2", "c3"}  # 只读计算，不改库
    assert set(by_id["c1"]) == {
        "id", "name", "verify_status", "last_verified_at", "effective_status", "days",
    }
    assert by_id["c1"]["days"] == 2
    assert by_id["c1"]["effective_status"] == "已验证"
    assert by_id["c1"]["verify_status"] == "已验证"  # 落库值原样返回
    assert by_id["c2"]["days"] is None
    assert by_id["c2"]["effective_status"] == "未验证"
    assert by_id["c3"]["days"] == 10
    assert by_id["c3"]["effective_status"] == "未验证"

    # 库内 verify_status 不被衰减计算改写（只读）
    assert db.query(pid, "SELECT verify_status FROM concept WHERE id='c3'")[0]["verify_status"] == "已验证"

    r = client.get("/api/v1/concepts/decay")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["code"] == "0"
    api_rows = payload["data"]
    assert {row["id"] for row in api_rows} == {"c1", "c2", "c3"}
    api_by_id = {row["id"]: row for row in api_rows}
    assert api_by_id["c2"]["effective_status"] == "未验证"
    assert api_by_id["c3"]["effective_status"] == "未验证"


# ---------------------------------------------------------------------------
# P2-07：周盘点页
# ---------------------------------------------------------------------------
def test_weekly_page_lists_pending_gaps(client, csrf_headers):
    pid = _new_project(client, csrf_headers)
    _unit(pid, "u1", "单元标题甲")
    _gap(pid, "g2", "u1", 2, text="缺口-二级文本")
    _gap(pid, "g3", "u1", 3, text="缺口-三级文本")
    _gap(pid, "g1", "u1", 1, text="缺口-一级积压")
    _gap(pid, "g9", "u1", 2, status="已补", text="缺口-已补不该出现")

    r = client.get("/weekly")
    assert r.status_code == 200, r.text
    text = r.text
    for expected in (
        "缺口-二级文本",
        "缺口-三级文本",
        "缺口-一级积压",
        "单元标题甲",
        "批量提交",
        "难度建议",
    ):
        assert expected in text, expected
    assert "缺口-已补不该出现" not in text
    assert 'hx-post="/api/v1/gaps/weekly-batch"' in text
    assert 'name="status-' in text
    assert '<option value="已补"' in text
    assert '<option value="废弃"' in text
    assert 'class="table-wrap"' in text


def test_weekly_page_empty_state(client, csrf_headers):
    """无待补缺口时页面仍有难度建议与空态提示（不渲染空表格 / 空表单）。"""
    _new_project(client, csrf_headers)
    r = client.get("/weekly")
    assert r.status_code == 200
    assert "本周没有待补的缺口" in r.text
    assert "难度建议" in r.text
    assert 'hx-post="/api/v1/gaps/status-batch"' not in r.text


def test_weekly_page_without_project(client, monkeypatch):
    monkeypatch.setattr("hpsl.routes.insight.proj.active_project", lambda: None)
    r = client.get("/weekly")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


# ---------------------------------------------------------------------------
# P2-07：批量裁决
# ---------------------------------------------------------------------------
def test_status_batch_json_updates_and_skips(client, csrf_headers):
    pid = _new_project(client, csrf_headers)
    _unit(pid, "u1")
    _gap(pid, "g1", "u1", 2)
    _gap(pid, "g2", "u1", 2)

    r = client.post(
        "/api/v1/gaps/status-batch",
        headers=csrf_headers,
        json={
            "items": [
                {"id": "g1", "status": "已补"},
                {"id": "g2", "status": "废弃"},
                {"id": "不存在的id", "status": "已补"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["code"] == "0"
    assert payload["data"] == {"updated": 2, "skipped": 1}
    assert _gap_status(pid, "g1") == "已补"
    assert _gap_status(pid, "g2") == "废弃"

    # 写经 db.write：同事务审计 actor=user
    audit = db.query(pid, "SELECT actor, table_name FROM audit_log ORDER BY id DESC LIMIT 1")
    assert (audit[0]["actor"], audit[0]["table_name"]) == ("user", "gap")


def test_status_batch_htmx_form_refresh(client, csrf_headers):
    pid = _new_project(client, csrf_headers)
    _unit(pid, "u1")
    _gap(pid, "g1", "u1", 2)
    _gap(pid, "g2", "u1", 1)

    htmx = {**csrf_headers, "HX-Request": "true"}
    r = client.post(
        "/api/v1/gaps/status-batch",
        headers=htmx,
        data={"gid": ["g1", "g2"], "status": ["已补", "废弃"]},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("HX-Refresh") == "true"
    assert "已更新 2 条缺口" in r.text
    assert _gap_status(pid, "g1") == "已补"
    assert _gap_status(pid, "g2") == "废弃"


def test_status_batch_invalid_status_c400001(client, csrf_headers):
    pid = _new_project(client, csrf_headers)
    _unit(pid, "u1")
    _gap(pid, "g1", "u1", 2)
    _gap(pid, "g2", "u1", 2)

    r = client.post(
        "/api/v1/gaps/status-batch", headers=csrf_headers,
        json={"items": [{"id": "g1", "status": "验证中"}]},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "C400001"
    assert _gap_status(pid, "g1") == "待补"

    # 混合批次：一个合法 + 一个非法 → 整体拒绝，不留部分写入
    r = client.post(
        "/api/v1/gaps/status-batch", headers=csrf_headers,
        json={
            "items": [
                {"id": "g1", "status": "已补"},
                {"id": "g2", "status": "进行中"},
            ]
        },
    )
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"
    assert _gap_status(pid, "g1") == "待补"
    assert _gap_status(pid, "g2") == "待补"


def test_status_batch_empty_and_missing_items(client, csrf_headers):
    _new_project(client, csrf_headers)
    r = client.post("/api/v1/gaps/status-batch", headers=csrf_headers, json={"items": []})
    assert r.status_code == 200
    assert r.json()["data"] == {"updated": 0, "skipped": 0}

    r = client.post("/api/v1/gaps/status-batch", headers=csrf_headers, json={})
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# 零 DDL（16 库对象不变）
# ---------------------------------------------------------------------------
def test_insight_zero_ddl(client, csrf_headers):
    pid = _new_project(client, csrf_headers)
    _unit(pid, "u1")
    _gap(pid, "g1", "u1", 2)
    _concept(pid, "c1", "概念甲", "2026-01-01T00:00:00.000Z", "已验证")

    client.get("/api/v1/bands")
    client.get("/api/v1/concepts/decay")
    client.get("/weekly")
    client.post(
        "/api/v1/gaps/status-batch",
        headers=csrf_headers,
        json={"items": [{"id": "g1", "status": "已补"}]},
    )
    assert db.count_user_objects(pid) == 16
