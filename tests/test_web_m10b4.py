"""M10B 第四批验收（F1 备份导出）：`/backup` 页 + `POST /api/v1/projects/{pid}/backup`（委托 db.backup_project）+ 列表 REST。

- AC1：GET /backup 渲染（无激活提示 / 列出历史快照 / 导出按钮）。
- AC2：POST /api/v1/projects/{pid}/backup → 200；生成快照文件（VACUUM INTO 落到 backups/）。
- AC3：GET /api/v1/projects/{pid}/backups → 200；返回快照文件名列表。
- AC4：错误路径（项目不存在 → A010001）。
- AC5：htmx 片段（导出返回「已生成备份」片段）。
- AC6：零 DDL（count_user_objects == 16 仍成立）。
"""

from pathlib import Path

import pytest

from hpsl import db, projects
from hpsl import config as cfg


def _create_and_switch(client, csrf_headers, name="M10B4项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


# ---------------------------------------------------------------------------
# AC1：页渲染
# ---------------------------------------------------------------------------
def test_backup_page_no_active(client):
    r = client.get("/backup")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


def test_backup_page_lists_and_export_button(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    r = client.get("/backup")
    assert r.status_code == 200
    assert "导出当前库快照" in r.text
    assert "历史快照（0）" in r.text
    assert f"/api/v1/projects/{d['id']}/backup" in r.text


# ---------------------------------------------------------------------------
# AC2：导出生成快照文件
# ---------------------------------------------------------------------------
def test_backup_api_creates_file(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    r = client.post(f"/api/v1/projects/{pid}/backup", headers=csrf_headers)
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "0"
    data = r.json()["data"]
    assert data["pid"] == pid
    assert data["backup_file"].endswith(".db")
    # 文件确实落盘到项目 backups/ 目录
    path = Path(data["backup_path"])
    assert path.exists()
    assert path.parent.name == "backups"


def test_backup_page_lists_after_export(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    client.post(f"/api/v1/projects/{pid}/backup", headers=csrf_headers)
    r = client.get("/backup")
    assert r.status_code == 200
    # 列表中出现刚生成的快照
    assert "历史快照（1）" in r.text


# ---------------------------------------------------------------------------
# AC3：列表 REST
# ---------------------------------------------------------------------------
def test_list_backups_api(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    client.post(f"/api/v1/projects/{pid}/backup", headers=csrf_headers)
    r = client.get(f"/api/v1/projects/{pid}/backups")
    assert r.status_code == 200
    assert r.json()["code"] == "0"
    assert isinstance(r.json()["data"], list)
    assert len(r.json()["data"]) >= 1
    assert r.json()["data"][0].endswith(".db")


# ---------------------------------------------------------------------------
# AC4：错误路径
# ---------------------------------------------------------------------------
def test_backup_api_missing_project_a010001(client, csrf_headers):
    r = client.post("/api/v1/projects/no-such/backup", headers=csrf_headers)
    assert r.status_code == 200
    assert r.json()["code"] == "A010001"


def test_list_backups_api_missing_project_a010001(client, csrf_headers):
    r = client.get("/api/v1/projects/no-such/backups")
    assert r.status_code == 200
    assert r.json()["code"] == "A010001"


# ---------------------------------------------------------------------------
# AC5：htmx 片段
# ---------------------------------------------------------------------------
def test_backup_htmx_returns_fragment(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    r = client.post(
        f"/api/v1/projects/{pid}/backup",
        headers={**csrf_headers, "HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "已生成备份" in r.text


# ---------------------------------------------------------------------------
# AC6：零 DDL 守护
# ---------------------------------------------------------------------------
def test_zero_ddl_after_backup(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    client.post(f"/api/v1/projects/{pid}/backup", headers=csrf_headers)
    assert db.count_user_objects(pid) == 16
