"""M11 批次3 验收：Markdown 导出（GET /export 页 + POST /api/v1/export/{kind}）。

- AC1：GET /export（激活项目）→ 200；页面含 4 个导出入口（project/units/ledger/concepts）。
- AC2：对每个 kind，POST /api/v1/export/{kind}（csrf_headers）→ 200，且
  exports/{kind}-*.md 文件存在；内容含预期 Markdown 标题、且 "sk-" not in content。
- AC3：无激活项目时 POST /api/v1/export/project → code 字段为 A080002。
- AC4：未知 kind → code 字段为 C400001。
- AC5：导出全部 kind 后 db.count_user_objects(pid) == 16（零 DDL 守护）。

纪律：仅动 export.py / export.html / app.py / __init__.py；零 DDL、无新错误码、密钥不随导出。
"""

from pathlib import Path

import pytest

from hpsl import config as cfg
from hpsl import db, projects


def _create_and_switch(client, csrf_headers, name="M11B3项目", goal="目标g"):
    body = {"name": name, "goal": goal}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def _exported_file(pid: str, kind: str) -> Path:
    d = cfg.project_dir(pid) / "exports"
    matches = list(d.glob(f"{kind}-*.md"))
    assert matches, f"未生成 {kind} 导出文件：{list(d.glob('*.md')) if d.is_dir() else '目录不存在'}"
    return matches[0]


# ---------------------------------------------------------------------------
# AC1：导出页结构
# ---------------------------------------------------------------------------
def test_export_page_has_four_entries(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    r = client.get("/export")
    assert r.status_code == 200
    # 四个 kind 的导出入口
    for kind in ("project", "units", "ledger", "concepts"):
        assert kind in r.text, f"导出页缺少入口：{kind}"


def test_export_page_no_project_shows_note(client):
    r = client.get("/export")
    assert r.status_code == 200
    assert "尚未选择项目" in r.text


# ---------------------------------------------------------------------------
# AC2：四种导出均生成文件且内容安全
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,title", [
    ("project", "# 项目导出"),
    ("units", "# 单元导出"),
    ("ledger", "# 四本账导出"),
    ("concepts", "# 概念导出"),
])
def test_export_kind_writes_markdown(client, csrf_headers, kind, title):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    r = client.post(f"/api/v1/export/{kind}", headers=csrf_headers, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == "0"
    # 文件存在
    fpath = _exported_file(pid, kind)
    content = fpath.read_text(encoding="utf-8")
    assert title in content
    # 密钥红线：导出的 Markdown 不含 sk- 类密钥字串 / Fernet token / Cookie
    assert "sk-" not in content
    assert "gAAAAA" not in content
    assert "Cookie:" not in content
    # 返回元信息自洽（bytes 为 len(md) 字符数，与落盘内容字符数一致）
    assert body["data"]["file"] == fpath.name
    assert body["data"]["bytes"] == len(content)


# ---------------------------------------------------------------------------
# AC3：无激活项目 → A080002
# ---------------------------------------------------------------------------
def test_export_without_active_returns_a080002(client, csrf_headers, monkeypatch):
    monkeypatch.setattr(projects, "_active_pid", None)
    r = client.post("/api/v1/export/project", headers=csrf_headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "A080002"


# ---------------------------------------------------------------------------
# AC4：未知 kind → C400001
# ---------------------------------------------------------------------------
def test_export_unknown_kind_returns_c400001(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    # C400001 为客户端错误，HTTP 状态 400；仅校验响应体 code 字段
    r = client.post("/api/v1/export/bogus_kind", headers=csrf_headers, json={})
    assert r.json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# AC5：零 DDL 守护（导出全部 kind 后库对象数仍为 16）
# ---------------------------------------------------------------------------
def test_zero_ddl_after_export_all_kinds(client, csrf_headers):
    d = _create_and_switch(client, csrf_headers)
    pid = d["id"]
    for kind in ("project", "units", "ledger", "concepts"):
        r = client.post(f"/api/v1/export/{kind}", headers=csrf_headers, json={})
        assert r.status_code == 200, r.text
    assert db.count_user_objects(pid) == 16
