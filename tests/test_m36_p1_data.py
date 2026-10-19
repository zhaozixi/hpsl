"""M36 / P1 数据与安全回归（B6 / B8 / B9 / B10 / B11）。

覆盖：
- B6 撤销语义：`expected_prev_hash` 同时接受「当前行」与「prev 行」指纹；`delete_source`
  的 audit.change 为完整被删行快照，undo 可恢复 source 行（FTS 文本不落 audit，重新索引即恢复检索）；
- B8 Web 恢复入口：`POST /api/v1/backup/{name}/restore` 的 basename + 快照白名单校验、
  HTMX 片段 + HX-Refresh、非 HTMX Result、备份页二次确认按钮；
  `rotate_all_backups` 清理 `pre-restore-*.db`（保留最近 3 份，不影响 project-* 规则）；
- B9 测试隔离：根 conftest 每例重置 `_active_label` / `sse_limiter` / `_write_locks`；
- B10 路由由 app.py 统一挂载，测试文件不再在 import 期 include_router（路由不重复注册）；
- B11 providers：未知 unit_id → A020001（不落 session、不裸 500）；未知 model 的 KeyError → A040003。

纪律：零 DDL（全部用例结束库对象仍为 16）；不新增错误码。
"""

import json
import uuid
from pathlib import Path

import pytest

from hpsl import config as cfg
from hpsl import db, projects
from hpsl.common.errors import BizError
from hpsl.engine import retrieval
from hpsl.routes import security
from hpsl.routes.backup import _validate_backup_name


# ---------------------------------------------------------------------------
# 公共设施
# ---------------------------------------------------------------------------
def _new_project(client, csrf, label="P1"):
    name = f"{label}-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "P1 回归"})
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _kb(tmp_path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    (root / "python.md").write_text(
        "# 生成器\n\n生成器用 yield 惰性产出数据，适合处理大数据流。",
        encoding="utf-8",
    )
    return root


def _last_audit(pid: str, table: str) -> dict:
    return db.query(
        pid,
        "SELECT id, change, prev FROM audit_log WHERE table_name=? ORDER BY id DESC LIMIT 1",
        (table,),
    )[0]


# ---------------------------------------------------------------------------
# B6 撤销语义
# ---------------------------------------------------------------------------
def test_undo_accepts_current_row_fingerprint():
    """expected_prev_hash 传「当前行」指纹（旧调用口径）仍应接受。"""
    vo = projects.create_project("B6-当前指纹", "g")
    db.write(vo.id, "concept", {"id": "c1", "name": "n1"}, "user")
    audit = _last_audit(vo.id, "concept")
    current = db.query(vo.id, "SELECT * FROM concept WHERE id='c1'")[0]
    assert db.undo(
        vo.id, audit["id"], "user", expected_prev_hash=db._row_fingerprint(current)
    ) is True
    assert db.query(vo.id, "SELECT * FROM concept WHERE id='c1'") == []


def test_undo_accepts_prev_row_fingerprint():
    """DTO 所述「旧值指纹」：传 prev 行指纹也应接受，并按 prev 回滚。"""
    vo = projects.create_project("B6-旧值指纹", "g")
    db.write(vo.id, "concept", {"id": "c1", "name": "n1"}, "user")
    db.write(vo.id, "concept", {"id": "c1", "name": "n2"}, "user")
    audit = _last_audit(vo.id, "concept")
    prev = json.loads(audit["prev"])
    assert db.undo(
        vo.id, audit["id"], "user", expected_prev_hash=db._row_fingerprint(prev)
    ) is True
    assert db.query(vo.id, "SELECT name FROM concept WHERE id='c1'")[0]["name"] == "n1"


def test_undo_still_rejects_unknown_fingerprint():
    vo = projects.create_project("B6-未知指纹", "g")
    db.write(vo.id, "concept", {"id": "c1", "name": "n1"}, "user")
    audit = _last_audit(vo.id, "concept")
    with pytest.raises(BizError) as exc:
        db.undo(vo.id, audit["id"], "user", expected_prev_hash="deadbeef")
    assert exc.value.code == "A070002"


def test_undo_delete_source_restores_source_row(tmp_path):
    """删除来源 → undo 恢复 source 行；重新索引后 FTS 检索恢复（零 DDL）。"""
    vo = projects.create_project("B6-删除撤销", "g")
    kb = _kb(tmp_path)
    indexed = retrieval.index_source(vo.id, str(kb))
    sid = indexed["source_id"]
    before = db.query(vo.id, "SELECT * FROM source WHERE id=?", (sid,))[0]
    assert retrieval.search(vo.id, "生成器")

    retrieval.delete_source(vo.id, sid)
    assert db.query(vo.id, "SELECT * FROM source WHERE id=?", (sid,)) == []
    assert db.query(vo.id, "SELECT count(*) c FROM chunk_fts")[0]["c"] == 0

    audit = _last_audit(vo.id, "source")
    assert json.loads(audit["change"]) == before, "change 必须是完整被删行快照"
    assert json.loads(audit["prev"]) == before

    # undo 无需传 expected_prev_hash 即可识别删除类审计并恢复
    assert db.undo(vo.id, audit["id"], "user") is True
    assert db.query(vo.id, "SELECT * FROM source WHERE id=?", (sid,)) == [before]
    assert db.count_user_objects(vo.id) == 16

    # FTS 文本不落 audit；重新索引即可按同一 source_id 恢复检索能力
    retrieval.index_source(vo.id, str(kb))
    assert retrieval.search(vo.id, "生成器")
    assert db.query(vo.id, "SELECT count(*) c FROM chunk_fts WHERE source_id=?", (sid,))[0]["c"] > 0


# ---------------------------------------------------------------------------
# B8 Web 恢复入口
# ---------------------------------------------------------------------------
def _make_backup(client, csrf) -> tuple[str, str]:
    pid = _new_project(client, csrf, "B8")
    db.write(pid, "concept", {"id": "c1", "name": "v1"}, "user")
    name = Path(db.backup_project(pid)).name
    db.write(pid, "concept", {"id": "c1", "name": "v2"}, "user")
    return pid, name


def test_backup_page_has_restore_button_with_confirm(client, csrf_headers):
    pid, name = _make_backup(client, csrf_headers)
    page = client.get("/backup")
    assert page.status_code == 200
    assert name in page.text
    assert "恢复" in page.text
    assert "hx-confirm" in page.text
    assert f"/api/v1/backup/{name}/restore" in page.text


def test_restore_endpoint_htmx_fragment_and_refresh(client, csrf_headers):
    pid, name = _make_backup(client, csrf_headers)
    headers = dict(csrf_headers)
    headers["HX-Request"] = "true"
    r = client.post(f"/api/v1/backup/{name}/restore", headers=headers)
    assert r.status_code == 200, r.text
    # M39 起不再整页刷新：结果片段 + OOB 同步列表与计数，否则「已恢复」提示会被立刻冲掉
    assert r.headers.get("HX-Refresh") is None
    assert 'id="backup-list"' in r.text and 'hx-swap-oob="true"' in r.text
    assert "已从快照恢复" in r.text and name in r.text
    assert db.query(pid, "SELECT name FROM concept WHERE id='c1'")[0]["name"] == "v1"
    pre = list((cfg.project_dir(pid) / "backups").glob("pre-restore-*.db"))
    assert pre, "恢复前必须留底 pre-restore-*.db"


def test_restore_endpoint_json_returns_ok(client, csrf_headers):
    pid, name = _make_backup(client, csrf_headers)
    r = client.post(f"/api/v1/backup/{name}/restore", headers=csrf_headers)
    body = r.json()
    assert body["code"] == "0", r.text
    assert body["data"]["backup_file"] == name
    assert body["data"]["objects"] == 16
    assert db.query(pid, "SELECT name FROM concept WHERE id='c1'")[0]["name"] == "v1"


def test_restore_name_validation_rejects_traversal_and_unknown():
    vo = projects.create_project("B8-文件名校验", "g")
    for bad in ("", ".", "..", "../project.db", "a/b.db", "a\\b.db", "/abs.db", "nope.db"):
        with pytest.raises(BizError) as exc:
            _validate_backup_name(vo.id, bad)
        assert exc.value.code == "C400001", bad


def test_restore_endpoint_rejects_unknown_snapshot(client, csrf_headers):
    _new_project(client, csrf_headers, "B8-未知快照")
    r = client.post(
        "/api/v1/backup/project-2000-01-01T00-00-00-000Z-deadbeef.db/restore",
        headers=csrf_headers,
    )
    assert r.json()["code"] == "C400001", r.text


def test_restore_endpoint_rejects_corrupt_snapshot(client, csrf_headers):
    pid = _new_project(client, csrf_headers, "B8-损坏快照")
    base = cfg.project_dir(pid) / "backups"
    base.mkdir(parents=True, exist_ok=True)
    name = "project-2020-01-01T00-00-00-000Z-badbad00.db"
    (base / name).write_bytes(b"not a sqlite database")
    r = client.post(f"/api/v1/backup/{name}/restore", headers=csrf_headers)
    assert r.json()["code"] == "B999001", r.text


def test_rotate_all_backups_prunes_pre_restore_keeps_three():
    vo = projects.create_project("B8-留底清理", "g")
    base = cfg.project_dir(vo.id) / "backups"
    base.mkdir(parents=True, exist_ok=True)
    names = [
        f"pre-restore-2026-10-{day:02d}T00-00-00-000Z-aaaa{day:02d}.db"
        for day in range(1, 6)
    ]
    for name in names:
        (base / name).write_bytes(b"x")
    snapshot = base / "project-2026-10-17T00-00-00-000Z-bbbb1111.db"
    snapshot.write_bytes(b"x")

    deleted = db.rotate_all_backups()
    remaining = sorted(p.name for p in base.glob("pre-restore-*.db"))
    assert remaining == sorted(names[2:]), "仅保留最近 3 份 pre-restore-*.db"
    assert set(deleted.get(vo.id, [])) == set(names[:2])
    assert snapshot.exists(), "project-* 轮转规则不受影响"


# ---------------------------------------------------------------------------
# B9 测试隔离（根 conftest 每例重置进程级状态）
# ---------------------------------------------------------------------------
def test_b9_leaks_process_state():
    """故意污染进程级状态；下一条用例应看到根 conftest 已重置。"""
    projects._active_label = "leaked-label"
    security.sse_limiter.configure(1)
    db._write_locks["leaked-pid"] = object()


def test_b9_process_state_is_reset_each_test():
    expected = int(cfg.rate_limits().get("sse_concurrency", 2))
    assert projects._active_label is None
    assert security.sse_limiter.active() == 0
    assert security.sse_limiter.limit == expected
    assert "leaked-pid" not in db._write_locks


# ---------------------------------------------------------------------------
# B10 路由只挂载一次
# ---------------------------------------------------------------------------
def test_b10_routes_mounted_once():
    """三个 m30 测试不再在 import 期 include_router → 每个 router 只被 app 挂载一次。

    注：本环境 FastAPI 0.143 的 include_router 以 `_IncludedRouter` 包装持有
    `original_router`，故按 router 对象身份计数（而非拍平后的 route.path）。
    """
    from hpsl.app import app
    from hpsl.routes import insight, multi, retrieval

    originals = [getattr(r, "original_router", None) for r in app.routes]
    for router in (
        retrieval.api_router,
        retrieval.page_router,
        multi.api_router,
        insight.api_router,
        insight.page_router,
    ):
        assert originals.count(router) == 1, f"router 被重复挂载：{router}"


# ---------------------------------------------------------------------------
# B11 providers：unit_id / model 校验
# ---------------------------------------------------------------------------
def _chat(client, csrf, **over):
    body = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(over)
    return client.post("/api/v1/providers/deepseek/chat", headers=csrf, json=body)


def test_provider_chat_unknown_unit_id_a020001(client, csrf_headers):
    pid = _new_project(client, csrf_headers, "B11-unit")
    r = _chat(client, csrf_headers, unit_id="no-such-unit")
    assert r.json()["code"] == "A020001", r.text
    # 校验发生在写 session 之前：不得留下半截 session 行
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0


def test_provider_chat_unknown_model_a040003(client, csrf_headers):
    _new_project(client, csrf_headers, "B11-model")
    r = _chat(client, csrf_headers, model="no-such-model")
    assert r.json()["code"] == "A040003", r.text


def test_provider_stream_unknown_unit_id_a020001(client, csrf_headers):
    _new_project(client, csrf_headers, "B11-stream-unit")
    r = client.post(
        "/api/v1/providers/deepseek/chat/stream",
        headers=csrf_headers,
        json={
            "model": "deepseek-chat",
            "unit_id": "no-such-unit",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.json()["code"] == "A020001", r.text


def test_provider_stream_unknown_model_a040003(client, csrf_headers):
    _new_project(client, csrf_headers, "B11-stream-model")
    r = client.post(
        "/api/v1/providers/deepseek/chat/stream",
        headers=csrf_headers,
        json={
            "model": "no-such-model",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.json()["code"] == "A040003", r.text
