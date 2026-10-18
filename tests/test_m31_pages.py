"""M31：知识库页面 + 卡片衰减展示。"""

from datetime import datetime, timedelta, timezone

from hpsl import db


def _project(client, csrf, name="知识库项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    return r.json()["data"]["id"]


def _htmx(csrf):
    return {**csrf, "HX-Request": "true"}


def test_knowledge_page_requires_project(client):
    r = client.get("/knowledge")
    assert "尚未选择项目" in r.text


def test_knowledge_index_and_search(client, csrf_headers, tmp_path):
    _project(client, csrf_headers)
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.md").write_text("# 索引对齐\n\n两个 Series 相加时按索引对齐。", encoding="utf-8")

    r = client.post(
        "/api/v1/sources", headers=_htmx(csrf_headers), data={"path": str(kb)}
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") is None
    assert "hpsl:refresh" in r.headers.get("HX-Trigger", "")

    page = client.get("/knowledge")
    assert str(kb) in page.text
    assert "已挂载来源（1）" in page.text

    hit = client.get("/knowledge?q=索引对齐")
    assert "命中" in hit.text
    assert "按索引对齐" in hit.text

    miss = client.get("/knowledge?q=完全不存在词")
    assert "未检索到" in miss.text


def test_knowledge_delete_source(client, csrf_headers, tmp_path):
    _project(client, csrf_headers)
    kb = tmp_path / "kb2"
    kb.mkdir()
    (kb / "b.md").write_text("内容", encoding="utf-8")
    client.post("/api/v1/sources", headers=csrf_headers, json={"path": str(kb)})
    sources = client.get("/api/v1/sources").json()["data"]
    assert len(sources) == 1
    r = client.request(
        "DELETE", f"/api/v1/sources/{sources[0]['id']}", headers=_htmx(csrf_headers)
    )
    assert r.headers.get("HX-Refresh") is None
    assert "hpsl:refresh" in r.headers.get("HX-Trigger", "")
    assert client.get("/api/v1/sources").json()["data"] == []


def test_cards_show_decayed_status(client, csrf_headers):
    pid = _project(client, csrf_headers)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    stamp = old.strftime("%Y-%m-%dT%H:%M:%S.") + f"{old.microsecond // 1000:03d}Z"
    db.write(
        pid,
        "concept",
        {"id": "c-old", "name": "旧概念", "verify_status": "已验证", "last_verified_at": stamp},
        "user",
    )
    db.write(
        pid,
        "card",
        {"id": "k1", "concept_id": "c-old", "my_words": "我的理解", "verify_status": "已验证"},
        "user",
    )
    page = client.get("/cards")
    assert "当前状态（衰减后）" in page.text
    assert "未验证" in page.text
    assert "天）" in page.text


def test_knowledge_in_nav(client, csrf_headers):
    _project(client, csrf_headers)
    page = client.get("/knowledge")
    assert 'href="/knowledge"' in page.text
