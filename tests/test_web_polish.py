"""M10B 收尾打磨三项验收：settings 空值 / 备份轮转（日7+月12）/ 概念图交互增强。

- AC-S1：GET /settings 对 None 配置渲染为空值（非字面 "None"）。
- AC-S2：保存时留空字段不参与提交（后端合并保留既有值）。
- AC-R1：rotate_backups 保留最近 7 日档 + 近 12 月各最早一份，删除其余。
- AC-R2：rotate_backups 仅删除 project-*.db，不触碰 project.db 或他者文件。
- AC-R3：backup_project 导出后自动轮转（集成）。
- AC-R4：轮转为文件级操作，零 DDL（count_user_objects == 16）。
- AC-G1：GET /graph 注入交互容器（viewport / 详情面板 / 数据 JSON / 节点点击）。
- AC-G2：概念图交互增强不改变零 DDL 约束。
"""

from pathlib import Path

import pytest

from hpsl import db, projects
from hpsl import config as cfg


def _create_and_switch(client, csrf_headers, name="POLISH项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _seed_concept_edge(pid, *, cid="c1", cid2="c2", kid="k1"):
    db.write(pid, "concept", {"id": cid, "name": "概念甲", "verify_status": "未验证"}, "user")
    db.write(pid, "concept", {"id": cid2, "name": "概念乙", "verify_status": "已验证"}, "user")
    db.write(
        pid, "card",
        {"id": kid, "concept_id": cid, "my_words": "我的理解A", "links": [], "verify_status": "未验证"},
        "user",
    )
    db.write(pid, "concept_edge", {"id": "e1", "src": cid, "dst": cid2, "kind": "prereq"}, "user")


# ---------------------------------------------------------------------------
# AC-S1：settings 空值渲染
# ---------------------------------------------------------------------------
def test_settings_null_threshold_renders_empty(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    # 显式写入一个 Null 阈值（模拟异常落库的 None）
    projects.set_config(pid, {"pass_threshold": None}, "user")
    r = client.get("/settings")
    assert r.status_code == 200
    # 属性渲染顺序为 id → name → value：None 应渲染为空串而非字面 "None"
    assert 'name="pass_threshold" value=""' in r.text
    assert 'value="None"' not in r.text


def test_settings_zero_value_not_hidden(client, csrf_headers):
    # 合法 0.0 不应被当作空值隐藏（skipped_produce_max 默认 0.0）
    d = _create_and_switch(client, csrf_headers)
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'name="skipped_produce_max" value="0.0"' in r.text


# ---------------------------------------------------------------------------
# AC-S2：保存留空不改既有值（后端合并契约）
# ---------------------------------------------------------------------------
def test_settings_save_omits_blank_keeps_others(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    # 仅提交 pass_threshold，其余留空（JS 行为对应的后端契约：PATCH 合并）
    r = client.put(
        f"/api/v1/projects/{pid}/config",
        headers=csrf_headers,
        json={"pass_threshold": 0.75},
    )
    assert r.status_code == 200
    got = r.json()["data"]
    assert got["pass_threshold"] == 0.75
    # 未提交的字段保持原默认，未被误写为 0 / None
    assert got["budget_cap_usd"] == 10.0
    assert got["session_minutes"] == 25


# ---------------------------------------------------------------------------
# AC-R1 / AC-R2：rotate_backups 保留逻辑 + 文件保护
# ---------------------------------------------------------------------------
def _seed_backup_files(backups_dir: Path, items: list) -> None:
    backups_dir.mkdir(parents=True, exist_ok=True)
    for name in items:
        (backups_dir / name).write_text("x")


def test_rotate_keeps_daily7_and_monthly12(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    bd = cfg.project_dir(pid) / "backups"
    daily = [f"project-2026-10-{dd:02d}T00-00-00-000Z-aaaaaaaa.db" for dd in range(1, 11)]  # 10/01..10/10
    monthly = [
        "project-2026-09-15T00-00-00-000Z-bbbbbbbb.db",
        "project-2026-08-15T00-00-00-000Z-cccccccc.db",
    ]
    # 保护用例：非本系统命名文件不应被删
    stray = ["other-2026-10-05T00-00-00-000Z-dddddddd.db", "notes.txt"]
    _seed_backup_files(bd, daily + monthly + stray)

    deleted = db.rotate_backups(pid)

    # 删除的恰好是 10/02、10/03（10/01 作为当月最早被月锚点保留，不应被删）
    assert "project-2026-10-02T00-00-00-000Z-aaaaaaaa.db" in deleted
    assert "project-2026-10-03T00-00-00-000Z-aaaaaaaa.db" in deleted
    # 删除后磁盘上确已移除
    assert not (bd / "project-2026-10-02T00-00-00-000Z-aaaaaaaa.db").exists()
    assert not (bd / "project-2026-10-03T00-00-00-000Z-aaaaaaaa.db").exists()
    # 保留：日档 10/04..10/10（7 份）+ 月锚点 10/01, 09/15, 08/15
    for keep in [
        "project-2026-10-01T00-00-00-000Z-aaaaaaaa.db",
        "project-2026-10-04T00-00-00-000Z-aaaaaaaa.db",
        "project-2026-10-10T00-00-00-000Z-aaaaaaaa.db",
        "project-2026-09-15T00-00-00-000Z-bbbbbbbb.db",
        "project-2026-08-15T00-00-00-000Z-cccccccc.db",
    ]:
        assert (bd / keep).exists(), f"应保留 {keep}"
    # 保护：非 project-*.db 文件不受影响
    assert (bd / "other-2026-10-05T00-00-00-000Z-dddddddd.db").exists()
    assert (bd / "notes.txt").exists()


def test_rotate_no_backups_is_noop(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    assert db.rotate_backups(pid) == []


# ---------------------------------------------------------------------------
# AC-R3：backup_project 导出后自动轮转（集成）
# ---------------------------------------------------------------------------
def test_backup_project_auto_rotates(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    bd = cfg.project_dir(pid) / "backups"
    # 预置 20 份旧日档（10/01..10/20）
    old = [f"project-2026-10-{dd:02d}T00-00-00-000Z-aaaaaaaa.db" for dd in range(1, 21)]
    _seed_backup_files(bd, old)

    dest = db.backup_project(pid)  # 本次导出（当前日期，最新）后会自动轮转
    assert Path(dest).exists()

    remaining = sorted(p.name for p in bd.glob("project-*.db"))
    # 日档保留最近 7 份 + 当月最早（10/01）月锚点 = 8 份
    assert len(remaining) == 8, remaining
    assert "project-2026-10-01T00-00-00-000Z-aaaaaaaa.db" in remaining  # 月锚点保留


# ---------------------------------------------------------------------------
# AC-R4 / AC-G2：零 DDL 守护
# ---------------------------------------------------------------------------
def test_zero_ddl_after_rotation(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    bd = cfg.project_dir(pid) / "backups"
    _seed_backup_files(bd, [f"project-2026-10-{dd:02d}T00-00-00-000Z-aaaaaaaa.db" for dd in range(1, 11)])
    db.rotate_backups(pid)
    assert db.count_user_objects(pid) == 16


def test_zero_ddl_after_graph_features(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_edge(pid)
    client.get("/graph")
    client.get("/settings")
    assert db.count_user_objects(pid) == 16


# ---------------------------------------------------------------------------
# AC-G1：概念图交互增强钩子
# ---------------------------------------------------------------------------
def test_graph_interaction_hooks(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    _seed_concept_edge(pid)
    r = client.get("/graph")
    assert r.status_code == 200
    # 交互容器
    assert 'id="concept-graph"' in r.text
    assert 'id="viewport"' in r.text          # 平移/缩放视口组
    assert 'id="graph-detail"' in r.text       # 节点详情面板
    assert "resetView" in r.text               # 重置视图
    assert "selectNode" in r.text              # 节点点击选择
    # 服务端注入的数据（供前端高亮/详情使用）
    assert "const NODES" in r.text
    assert "const EDGES" in r.text
    assert "const CONCEPTS" in r.text
    # 节点点击处理器已绑定
    assert "selectNode('c1')" in r.text
    # 边类型标注可见
    assert "prereq" in r.text
    # 既有渲染契约仍成立
    assert "概念节点（2）" in r.text
    assert "概念甲" in r.text
