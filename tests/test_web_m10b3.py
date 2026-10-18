"""M10B 第三批验收（规格 §3 AC1–AC6）：`/unit` 单元详情 + 工作流页（消费 M2 state.move / M5 claim_check / M7 ledger）。

- AC1：GET /unit（索引）/ GET /unit/{uid}（详情，含 M2 工作流 + M7 缺口 + M5 挑错）渲染 200。
- AC2：GET /api/v1/units 列表 / GET /api/v1/units/{uid} 详情（含 allowed_next / gaps / claim_checks）200。
- AC3：POST /api/v1/units/{uid}/move 合法转移 → 状态更新（cur→nxt）。
- AC4：非法转移 / 状态冲突 → A020001；LEARN→VERIFY 无产出无理由 → A020003；缺 cur/nxt → C400001。
- AC5：POST /api/v1/units/{uid}/claim-check/{cid}/arbitrate 合法 ruling（采纳/驳回/待验证）→ 写回 user_ruling/residual；非法 ruling → C400001。
- AC6：零 DDL（count_user_objects == 16 仍成立）。
- 附加：HTMX 片段端点（move / arbitrate）返回正确锚点片段（守护此前 context 变量修正）。
"""

import pytest

from hpsl import db, projects
from hpsl.engine import claim_check as cc


def _create_and_switch(client, csrf_headers, name="M10B3项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed_unit(client, csrf_headers, *, uid="u1", title="单元甲", status="INIT",
               difficulty_band=None, production=None):
    """建项目并切换，插入一条单元（部分列，模拟学习会话产出的单元行），返回 (project_dict, pid)。"""
    d = _create_and_switch(client, csrf_headers, name=f"{title}项目")
    pid = d["id"]
    row = {"id": uid, "title": title, "status": status}
    if difficulty_band is not None:
        row["difficulty_band"] = difficulty_band
    if production is not None:
        row["production"] = production
    db.write(pid, "unit", row, "user")
    return d, pid


def _seed_two_units(client, csrf_headers, *, u1="单元甲", u2="单元乙"):
    """建一个项目并切换，在其中插入两条单元（u1 / u2），返回 (project_dict, pid)。

    用于列表类测试：必须处于同一激活项目下，GET /unit 与 /api/v1/units 才能同时见到两条。
    """
    d = _create_and_switch(client, csrf_headers, name="多单元项目")
    pid = d["id"]
    db.write(pid, "unit", {"id": "u1", "title": u1, "status": "INIT"}, "user")
    db.write(pid, "unit", {"id": "u2", "title": u2, "status": "INIT"}, "user")
    return d, pid


def _seed_claim_check(pid, uid, *, claim="挑错甲"):
    """插入一条挑错记录（经引擎入口，处理 providers 序列化），返回 cid。"""
    return cc.write_claim_check(pid, claim=claim, unit_id=uid, providers=[])


def _seed_gap(pid, uid, *, level=2, text="缺口甲", status="待补"):
    """插入一条缺口（gap 表含 NOT NULL 时间戳列），返回 gid。"""
    now = db._now_utc()
    gid = "g1"
    db.write(
        pid, "gap",
        {
            "id": gid, "unit_id": uid, "session_id": None,
            "level": level, "text": text, "status": status,
            "context": None, "created_at": now, "updated_at": now,
        },
        "user",
    )
    return gid


# ---------------------------------------------------------------------------
# AC1：页面渲染
# ---------------------------------------------------------------------------
def test_unit_index_no_active_project(client):
    r = client.get("/unit")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


def test_unit_index_lists_units(client, csrf_headers):
    _seed_two_units(client, csrf_headers)
    r = client.get("/unit")
    assert r.status_code == 200
    assert "单元甲" in r.text
    assert "单元乙" in r.text
    assert "单元列表（2）" in r.text
    assert 'id="unit-u1"' in r.text
    assert 'href="/unit/u1"' in r.text


def test_unit_detail_renders_sections(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1", title="单元甲")
    cid = _seed_claim_check(pid, "u1", claim="挑错甲")
    _seed_gap(pid, "u1", text="缺口甲")
    r = client.get("/unit/u1")
    assert r.status_code == 200
    # 四区齐备（M24 在 ② 与 缺口 之间插入了产出/尝试/闭环/角色四块，编号顺延）
    assert "单元甲" in r.text
    assert "① 单元元信息" in r.text
    assert "② 工作流" in r.text
    assert "③ 产出（回归主线）" in r.text
    assert "④ 记录一次尝试" in r.text
    assert "⑤ 判定闭环" in r.text
    assert "⑥ AI 辅助（角色调用）" in r.text
    assert "⑦ 缺口清单" in r.text
    assert "⑧ 挑错清单" in r.text
    # 工作流面板锚点 + 当前状态 + 可推进按钮
    assert 'id="wf-panel"' in r.text
    assert "当前状态" in r.text
    # 缺口与挑错内容
    assert "缺口甲" in r.text
    assert "挑错甲" in r.text
    assert f'id="cc-{cid}"' in r.text


def test_unit_detail_not_found(client, csrf_headers):
    _seed_unit(client, csrf_headers, uid="u1")
    r = client.get("/unit/non-existent")
    assert r.status_code == 200
    assert "不存在" in r.text


# ---------------------------------------------------------------------------
# AC2：REST 列表 / 详情
# ---------------------------------------------------------------------------
def test_units_api_lists(client, csrf_headers):
    _seed_two_units(client, csrf_headers)
    r = client.get("/api/v1/units")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    data = r.json()["data"]
    assert len(data) >= 2
    ids = {u["id"] for u in data}
    assert {"u1", "u2"} <= ids


def test_units_api_no_active(client):
    r = client.get("/api/v1/units")
    assert r.status_code == 200
    assert r.json()["code"] == "A010001"


def test_unit_detail_api(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1", title="单元甲")
    cid = _seed_claim_check(pid, "u1", claim="挑错甲")
    _seed_gap(pid, "u1", text="缺口甲")
    r = client.get("/api/v1/units/u1")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    d = r.json()["data"]
    assert d["unit"]["title"] == "单元甲"
    assert d["unit"]["status"] == "INIT"
    # INIT 仅可推进到 PICK
    assert d["allowed_next"] == ["PICK"]
    # gaps / claim_checks 随详情返回
    assert any(g["text"] == "缺口甲" for g in d["gaps"])
    cc_ids = {c["id"] for c in d["claim_checks"]}
    assert cid in cc_ids


def test_unit_detail_api_not_found(client, csrf_headers):
    _seed_unit(client, csrf_headers, uid="u1")
    r = client.get("/api/v1/units/no-such-unit")
    assert r.status_code == 200
    assert r.json()["code"] == "A020001"


# ---------------------------------------------------------------------------
# AC3：move 合法转移
# ---------------------------------------------------------------------------
def test_move_unit_valid(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1", status="INIT")
    r = client.post(
        "/api/v1/units/u1/move",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"cur": "INIT", "nxt": "PICK"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "0"
    d = r.json()["data"]
    assert d["status"] == "PICK"
    # PICK 仅可推进到 LEARN
    assert d["allowed_next"] == ["LEARN"]
    # DB 实际落账
    row = db.query(pid, "SELECT status FROM unit WHERE id=?", ("u1",))[0]
    assert row["status"] == "PICK"
    # 详情 REST 反映新状态的可推进集
    r2 = client.get("/api/v1/units/u1")
    assert r2.json()["data"]["allowed_next"] == ["LEARN"]


# ---------------------------------------------------------------------------
# AC4：move 错误码（A020001 / A020003 / C400001）
# ---------------------------------------------------------------------------
def test_move_unit_illegal_transition_a020001(client, csrf_headers):
    _seed_unit(client, csrf_headers, uid="u1", status="INIT")
    r = client.post(
        "/api/v1/units/u1/move",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"cur": "INIT", "nxt": "LEARN"},  # INIT 不可直达 LEARN
    )
    assert r.status_code == 200
    assert r.json()["code"] == "A020001"


def test_move_unit_status_conflict_a020001(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1", status="PICK")
    # 调用方仍按 INIT 提交 → 与库内 PICK 冲突
    r = client.post(
        "/api/v1/units/u1/move",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"cur": "INIT", "nxt": "LEARN"},
    )
    assert r.status_code == 200
    assert r.json()["code"] == "A020001"
    # 状态未被改写
    row = db.query(pid, "SELECT status FROM unit WHERE id=?", ("u1",))[0]
    assert row["status"] == "PICK"


def test_move_verify_without_production_a020003(client, csrf_headers):
    # LEARN 且 production 为空，进入 VERIFY 无 reason → A020003
    _seed_unit(client, csrf_headers, uid="u1", status="LEARN", production="")
    r = client.post(
        "/api/v1/units/u1/move",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"cur": "LEARN", "nxt": "VERIFY"},
    )
    assert r.status_code == 200
    assert r.json()["code"] == "A020003"


def test_move_missing_cur_c400001(client, csrf_headers):
    _seed_unit(client, csrf_headers, uid="u1", status="INIT")
    r = client.post(
        "/api/v1/units/u1/move",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"nxt": "PICK"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"


def test_move_missing_nxt_c400001(client, csrf_headers):
    _seed_unit(client, csrf_headers, uid="u1", status="INIT")
    r = client.post(
        "/api/v1/units/u1/move",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"cur": "INIT"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# AC5：arbitrate 仲裁
# ---------------------------------------------------------------------------
def test_arbitrate_adopt_writes_residual(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1")
    cid = _seed_claim_check(pid, "u1", claim="挑错甲")
    r = client.post(
        f"/api/v1/units/u1/claim-check/{cid}/arbitrate",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"ruling": "采纳"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "0"
    row = db.query(pid, "SELECT user_ruling, residual FROM claim_check WHERE id=?", (cid,))[0]
    assert row["user_ruling"] == "采纳"
    assert row["residual"] == 1  # 采纳 → 残留


def test_arbitrate_reject_clears_residual(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1")
    cid = _seed_claim_check(pid, "u1")
    r = client.post(
        f"/api/v1/units/u1/claim-check/{cid}/arbitrate",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"ruling": "驳回"},
    )
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    row = db.query(pid, "SELECT user_ruling, residual FROM claim_check WHERE id=?", (cid,))[0]
    assert row["user_ruling"] == "驳回"
    assert row["residual"] == 0  # 驳回 → 清零


def test_arbitrate_pending_residual(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1")
    cid = _seed_claim_check(pid, "u1")
    r = client.post(
        f"/api/v1/units/u1/claim-check/{cid}/arbitrate",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"ruling": "待验证"},
    )
    assert r.status_code == 200
    row = db.query(pid, "SELECT user_ruling, residual FROM claim_check WHERE id=?", (cid,))[0]
    assert row["user_ruling"] == "待验证"
    assert row["residual"] == 1  # 待验证 → 残留但不阻断


def test_arbitrate_invalid_ruling_c400001(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1")
    cid = _seed_claim_check(pid, "u1")
    r = client.post(
        f"/api/v1/units/u1/claim-check/{cid}/arbitrate",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"ruling": "其它"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"
    row = db.query(pid, "SELECT user_ruling FROM claim_check WHERE id=?", (cid,))[0]
    assert row["user_ruling"] is None  # 非法裁决不落账


def test_arbitrate_missing_ruling_c400001(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1")
    cid = _seed_claim_check(pid, "u1")
    r = client.post(
        f"/api/v1/units/u1/claim-check/{cid}/arbitrate",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# AC6：零 DDL 守护（不新增库对象）
# ---------------------------------------------------------------------------
def test_zero_ddl_after_unit_features(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1", status="INIT")
    cid = _seed_claim_check(pid, "u1")
    _seed_gap(pid, "u1")
    # 跑一轮合法 move + 合法 arbitrate，确保全程不触发任何 DDL
    client.post(
        "/api/v1/units/u1/move",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"cur": "INIT", "nxt": "PICK"},
    )
    client.post(
        f"/api/v1/units/u1/claim-check/{cid}/arbitrate",
        headers={**csrf_headers, "Content-Type": "application/x-www-form-urlencoded"},
        data={"ruling": "驳回"},
    )
    assert db.count_user_objects(pid) == 16


# ---------------------------------------------------------------------------
# 附加：HTMX 片段端点（守护 context 变量修正）
# ---------------------------------------------------------------------------
def test_move_htmx_returns_panel_fragment(client, csrf_headers):
    _seed_unit(client, csrf_headers, uid="u1", status="INIT")
    r = client.post(
        "/api/v1/units/u1/move",
        headers={
            **csrf_headers,
            "HX-Request": "true",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"cur": "INIT", "nxt": "PICK"},
    )
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/html")
    assert 'id="wf-panel"' in r.text
    assert "当前状态" in r.text
    assert "推进到" in r.text


def test_arbitrate_htmx_returns_row_fragment(client, csrf_headers):
    _, pid = _seed_unit(client, csrf_headers, uid="u1")
    cid = _seed_claim_check(pid, "u1", claim="挑错甲")
    r = client.post(
        f"/api/v1/units/u1/claim-check/{cid}/arbitrate",
        headers={
            **csrf_headers,
            "HX-Request": "true",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"ruling": "驳回"},
    )
    assert r.status_code == 200
    assert 'id="cc-' in r.text
    assert "挑错甲" in r.text
