"""M40 验收：项目归档 / 取消归档 / 删除（零 DDL，状态落在 project.config）。

设计口径（与 docs/M40-项目归档与删除.md 一致）：
- 归档 = 写 `config.archived_at`，幂等；不删任何数据，备份/轮转仍覆盖归档项目。
- 归档当前 active 项目 → 清空 active（不隐式跳到别的项目）。
- 删除 = 整目录 rename 到 `HPSL_HOME/trash/`，要求逐字输入项目名确认。
- `list_projects()` 默认含归档（保持 REST/CLI 契约），页面显式传 False。
"""

from pathlib import Path

from hpsl import config as _config
from hpsl import projects as proj


def _mk(client, csrf, name="甲项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def _htmx(csrf, extra=None):
    return {**csrf, "HX-Request": "true", **(extra or {})}


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------
def _active_list_html(home: str) -> str:
    """截取「活跃项目列表」那一段，避免把归档区里的项目名误判成"还在列表里"。"""
    start = home.find('id="project-list"')
    if start == -1:
        return ""
    end = home.find("</ul>", start)
    return home[start:end]


def test_archive_sets_timestamp_and_hides_from_page(client, csrf_headers):
    pid = _mk(client, csrf_headers)

    r = client.post(f"/api/v1/projects/{pid}/archive", headers=csrf_headers)
    assert r.status_code == 200
    assert r.json()["data"]["archived_at"], "归档必须写入 archived_at"

    # REST 列表仍包含（备份/轮转 CLI 依赖），页面则不再出现在活跃列表
    listed = [p["id"] for p in client.get("/api/v1/projects").json()["data"]]
    assert pid in listed
    home = client.get("/").text
    assert pid not in _active_list_html(home), "归档项目不应出现在活跃列表"
    assert "已归档（1）" in home, "归档项应出现在折叠区，且标明数量"


def test_archive_is_idempotent(client, csrf_headers):
    pid = _mk(client, csrf_headers)
    first = client.post(f"/api/v1/projects/{pid}/archive", headers=csrf_headers).json()["data"]
    second = client.post(f"/api/v1/projects/{pid}/archive", headers=csrf_headers).json()["data"]
    assert first["archived_at"] == second["archived_at"], "重复归档不得刷新时间戳"


def test_archive_active_project_clears_active(client, csrf_headers):
    pid = _mk(client, csrf_headers)  # create_project 会把新项目设为 active
    assert proj.active_project().id == pid
    client.post(f"/api/v1/projects/{pid}/archive", headers=csrf_headers)
    assert proj.active_project() is None, "归档当前项目后应退回「未选择项目」，不得隐式切换"
    assert proj.active_label() is None


def test_archived_project_cannot_be_switched(client, csrf_headers):
    """M41/F-04：归档项目不能被设为 active（页面隐藏按钮，API 也必须拒绝）。"""
    pid = _mk(client, csrf_headers, "归档不可切换")
    client.post(f"/api/v1/projects/{pid}/archive", headers=csrf_headers)
    r = client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers)
    body = r.json()
    assert body["code"] == "A010001", body
    assert "已归档" in body["msg"]
    assert proj.active_project() is None

    client.post(f"/api/v1/projects/{pid}/unarchive", headers=csrf_headers)
    r2 = client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers)
    assert r2.json()["code"] == "0", r2.text
    assert proj.active_project().id == pid


def test_unarchive_restores_to_list(client, csrf_headers):
    pid = _mk(client, csrf_headers)
    client.post(f"/api/v1/projects/{pid}/archive", headers=csrf_headers)
    r = client.post(f"/api/v1/projects/{pid}/unarchive", headers=csrf_headers)
    assert r.status_code == 200
    assert r.json()["data"]["archived_at"] is None
    assert "甲项目" in client.get("/").text


def test_restore_last_active_skips_archived(client, csrf_headers):
    archived = _mk(client, csrf_headers, name="将归档")
    kept = _mk(client, csrf_headers, name="保留")
    # 两个项目各自留 last_active_at，且归档项时间更新（若不过滤会被选中）
    client.post(f"/api/v1/projects/{archived}/switch", headers=csrf_headers)
    client.post(f"/api/v1/projects/{kept}/switch", headers=csrf_headers)
    client.post(f"/api/v1/projects/{archived}/switch", headers=csrf_headers)

    proj._clear_active()
    client.post(f"/api/v1/projects/{archived}/archive", headers=csrf_headers)
    proj._clear_active()

    vo = proj.restore_last_active()
    assert vo is not None and vo.id == kept, "启动恢复不得跳回已归档项目"


def test_archived_project_detail_shows_badge(client, csrf_headers):
    pid = _mk(client, csrf_headers)
    client.post(f"/api/v1/projects/{pid}/archive", headers=csrf_headers)
    html = client.get(f"/projects/{pid}").text
    assert "已归档" in html
    assert "取消归档" in html


# ---------------------------------------------------------------------------
# HTMX 片段
# ---------------------------------------------------------------------------
def test_archive_htmx_returns_panel_fragment(client, csrf_headers):
    pid = _mk(client, csrf_headers)
    r = client.post(f"/api/v1/projects/{pid}/archive", headers=_htmx(csrf_headers))
    assert r.status_code == 200
    assert 'id="project-panel"' in r.text, "片段必须保留外层 id，否则 hx-swap=outerHTML 会失效"
    assert "已归档" in r.text


def test_archive_from_detail_redirects_to_home(client, csrf_headers):
    pid = _mk(client, csrf_headers)
    r = client.post(
        f"/api/v1/projects/{pid}/archive",
        headers=_htmx(csrf_headers),
        data={"origin": "detail"},
    )
    assert r.headers.get("HX-Redirect") == "/"


def test_unarchive_from_detail_redirects_back(client, csrf_headers):
    pid = _mk(client, csrf_headers)
    client.post(f"/api/v1/projects/{pid}/archive", headers=csrf_headers)
    r = client.post(
        f"/api/v1/projects/{pid}/unarchive",
        headers=_htmx(csrf_headers),
        data={"origin": "detail"},
    )
    assert r.headers.get("HX-Redirect") == f"/projects/{pid}"


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
def test_delete_requires_exact_project_name(client, csrf_headers):
    pid = _mk(client, csrf_headers, name="要删的项目")
    r = client.post(
        f"/api/v1/projects/{pid}/delete", headers=csrf_headers, data={"confirm_name": "要删"}
    )
    assert r.status_code == 400
    assert r.json()["code"] == "C400001"
    assert _config.project_dir(pid).exists(), "确认名不符时不得动目录"


def test_delete_moves_directory_to_trash(client, csrf_headers):
    pid = _mk(client, csrf_headers, name="待删项目")
    r = client.post(
        f"/api/v1/projects/{pid}/delete", headers=csrf_headers, data={"confirm_name": "待删项目"}
    )
    assert r.status_code == 200
    trash_path = Path(r.json()["data"]["trash_path"])
    assert trash_path.exists(), "项目目录应整体移入 trash，而不是直接销毁"
    assert trash_path.parent == _config.trash_root()
    assert (trash_path / "project.db").exists(), "库文件必须随目录一起保留（可恢复）"
    assert not _config.project_dir(pid).exists()
    assert pid not in [p.id for p in proj.list_projects()]
    assert proj.active_project() is None


def test_delete_htmx_wrong_name_keeps_panel(client, csrf_headers):
    """HTMX 通道下错误必须是「完整面板 + 红条」，不能只剩一个错误片段把列表顶掉。"""
    pid = _mk(client, csrf_headers, name="名称校验")
    r = client.post(
        f"/api/v1/projects/{pid}/delete",
        headers=_htmx(csrf_headers),
        data={"confirm_name": "错的"},
    )
    assert r.status_code == 200
    assert 'id="project-panel"' in r.text, "错误响应仍须保留面板结构"
    assert 'class="banner err"' in r.text
    assert str(pid) in r.text, "项目应仍在列表里"
    assert _config.project_dir(pid).exists()


def test_delete_htmx_from_detail_redirects(client, csrf_headers):
    pid = _mk(client, csrf_headers, name="详情页删除")
    r = client.post(
        f"/api/v1/projects/{pid}/delete",
        headers=_htmx(csrf_headers),
        data={"confirm_name": "详情页删除", "origin": "detail"},
    )
    assert r.headers.get("HX-Redirect") == "/"


def test_delete_unknown_project_reports_not_found(client, csrf_headers):
    """A010001 在码表里固定映射 200（HTMX 业务错误要能 swap），判定看 code 不看状态码。"""
    r = client.post(
        "/api/v1/projects/" + "f" * 32 + "/delete", headers=csrf_headers, data={"confirm_name": "x"}
    )
    assert r.status_code == 200
    assert r.json()["code"] == "A010001"
