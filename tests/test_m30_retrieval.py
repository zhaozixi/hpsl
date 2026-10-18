"""M30 / P2-01 + P2-02：知识库检索（FTS5）与空白上下文模式。

覆盖：
- 索引成功（source 行 + chunk_fts 行、幂等重索引）、中文/英文检索命中并展示原文；
- 无命中 `hit=false`；删除来源后检索不到；
- 参数校验复用 C400001（不存在目录 / 文件伪目录 / 相对路径越界）；
- 零 DDL：`count_user_objects == 16`；
- 空白上下文：角色调用不读账本/单元字段；答案机仍映射 question；input-preview 同口径。

说明：路由由 `hpsl/app.py` 统一挂载（B10：测试不在 import 期重复 include_router）。
"""

import uuid

import pytest

from hpsl import db
from hpsl.models.base import Reply
from hpsl.routes import roles as roles_routes


# ---------------------------------------------------------------------------
# 公共设施
# ---------------------------------------------------------------------------
class _FakeAdapter:
    """记录 messages 的假适配器（不发网络请求）。"""

    name = "fake"
    supports_json = False

    def __init__(self, text="空白上下文回答"):
        self.text = text
        self.calls = []

    def complete(self, messages, *, json_schema=None, stream=False):
        self.calls.append(messages)
        return Reply(
            text=self.text,
            finish_reason="stop",
            model="deepseek-chat",
            raw={"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )


@pytest.fixture
def fake(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(roles_routes.registry, "pick_default_model", lambda role=None: "deepseek-chat")
    monkeypatch.setattr(roles_routes.registry, "build_adapter", lambda model_id, **kw: adapter)
    return adapter


@pytest.fixture
def kb(tmp_path):
    """临时知识库：2 个 md + 1 个非索引扩展名文件。"""
    root = tmp_path / "kb"
    root.mkdir()
    (root / "python.md").write_text(
        "# Python 基础\n\n"
        "Python 是一门编程语言。列表与字典是最常用的数据结构。\n\n"
        "## 生成器\n\n"
        "生成器用 yield 惰性产出数据，适合处理大数据流。",
        encoding="utf-8",
    )
    (root / "ml.md").write_text(
        "# Machine Learning\n\n"
        "Machine learning models learn from data.\n\n"
        "监督学习需要标注数据；梯度下降用于优化模型参数。",
        encoding="utf-8",
    )
    (root / "ignored.json").write_text("{}", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "notes.txt").write_text("子目录笔记：空白上下文模式。", encoding="utf-8")
    return root


def _project(client, csrf):
    name = f"检索-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "知识库"})
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _unit(client, csrf, title="单元"):
    r = client.post("/api/v1/units", headers=csrf, json={"title": title})
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _index(client, csrf, path):
    return client.post("/api/v1/sources", headers=csrf, json={"path": str(path)})


def _search(client, q, limit=None):
    params = {"q": q}
    if limit is not None:
        params["limit"] = limit
    return client.get("/api/v1/search", params=params).json()["data"]


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------
def test_index_source_success_and_idempotent(client, csrf_headers, kb):
    pid = _project(client, csrf_headers)
    r = _index(client, csrf_headers, kb)
    assert r.json()["code"] == "0", r.text
    data = r.json()["data"]
    assert data["source_id"]
    assert data["files"] == 3  # 2 个 md + 子目录 notes.txt（json 不索引）
    assert data["chunks"] > 0

    listed = client.get("/api/v1/sources").json()["data"]
    assert len(listed) == 1
    assert listed[0]["id"] == data["source_id"]
    assert listed[0]["path"] == str(kb.resolve())
    assert listed[0]["kind"] == "folder"
    assert listed[0]["chunks"] == data["chunks"]
    assert db.query(pid, "SELECT count(*) c FROM source")[0]["c"] == 1

    # 幂等重索引：同一 path 复用 source_id，FTS 行整体替换而非累加
    again = _index(client, csrf_headers, kb).json()["data"]
    assert again["source_id"] == data["source_id"]
    assert again["chunks"] == data["chunks"]
    assert db.query(pid, "SELECT count(*) c FROM source")[0]["c"] == 1
    assert db.query(pid, "SELECT count(*) c FROM chunk_fts")[0]["c"] == data["chunks"]


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def test_search_chinese_hits_and_shows_original_text(client, csrf_headers, kb):
    _project(client, csrf_headers)
    _index(client, csrf_headers, kb)
    data = _search(client, "生成器")
    assert data["hit"] is True
    assert data["items"]
    assert any("生成器" in it["text"] for it in data["items"])
    assert any(it["page"] == "python.md" for it in data["items"])


def test_search_english_hits_and_shows_original_text(client, csrf_headers, kb):
    _project(client, csrf_headers)
    _index(client, csrf_headers, kb)
    data = _search(client, "machine learning")
    assert data["hit"] is True
    hit = next(it for it in data["items"] if "Machine learning" in it["text"])
    assert hit["page"] == "ml.md"
    assert isinstance(hit["score"], float)


def test_search_no_hit_returns_false_and_empty(client, csrf_headers, kb):
    _project(client, csrf_headers)
    _index(client, csrf_headers, kb)
    for q in ("量子纠缠", ""):
        data = _search(client, q)
        assert data["hit"] is False
        assert data["items"] == []


def test_delete_source_removes_hits(client, csrf_headers, kb):
    pid = _project(client, csrf_headers)
    sid = _index(client, csrf_headers, kb).json()["data"]["source_id"]
    assert _search(client, "生成器")["hit"] is True

    r = client.delete(f"/api/v1/sources/{sid}", headers=csrf_headers)
    assert r.json()["code"] == "0", r.text
    assert r.json()["data"]["deleted"] is True

    assert client.get("/api/v1/sources").json()["data"] == []
    assert _search(client, "生成器")["hit"] is False
    assert db.query(pid, "SELECT count(*) c FROM chunk_fts")[0]["c"] == 0
    assert db.query(pid, "SELECT count(*) c FROM source")[0]["c"] == 0
    # 删除不存在的来源 → C400001（不新增错误码）
    assert client.delete(f"/api/v1/sources/{sid}", headers=csrf_headers).json()["code"] == "C400001"


# ---------------------------------------------------------------------------
# 参数/路径校验
# ---------------------------------------------------------------------------
def test_index_missing_directory_c400001(client, csrf_headers, tmp_path):
    _project(client, csrf_headers)
    r = _index(client, csrf_headers, tmp_path / "not-exist")
    assert r.json()["code"] == "C400001", r.text


def test_index_file_as_directory_c400001(client, csrf_headers, tmp_path):
    _project(client, csrf_headers)
    f = tmp_path / "plain.md"
    f.write_text("x", encoding="utf-8")
    assert _index(client, csrf_headers, f).json()["code"] == "C400001"


def test_index_relative_or_empty_path_c400001(client, csrf_headers):
    _project(client, csrf_headers)
    # 相对路径存在越界歧义 → 拒绝；空 path 同样 C400001
    assert _index(client, csrf_headers, "relative/kb").json()["code"] == "C400001"
    r = client.post("/api/v1/sources", headers=csrf_headers, json={"path": ""})
    assert r.json()["code"] == "C400001"


def test_zero_ddl_count_still_16(client, csrf_headers, kb):
    pid = _project(client, csrf_headers)
    sid = _index(client, csrf_headers, kb).json()["data"]["source_id"]
    _search(client, "生成器")
    assert db.count_user_objects(pid) == 16
    client.delete(f"/api/v1/sources/{sid}", headers=csrf_headers)
    assert db.count_user_objects(pid) == 16


# ---------------------------------------------------------------------------
# 空白上下文模式（P2-02）
# ---------------------------------------------------------------------------
def test_blank_context_call_reads_no_ledger(client, csrf_headers, fake):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers, title="哨兵-单元标题")
    db.write(
        pid,
        "concept",
        {"id": "sentinel-c", "name": "哨兵-概念摘要", "verify_status": "未验证"},
        "user",
    )
    r = client.post(
        "/api/v1/roles/critic/call",
        headers=csrf_headers,
        json={
            "unit_id": uid,
            "blank_context": True,
            "learner_input": "我的空白上下文解释",
            "context": {
                "unit_title": "注入-单元标题",
                "concept_summary": "注入-概念摘要",
                "topic": "注入-主题",
                "learner_production": "注入-学习者产出",
            },
        },
    )
    assert r.json()["code"] == "0", r.text
    blob = "\n".join(m.content for m in fake.calls[0])
    # 账本/单元字段（值）绝不进入 messages
    assert "哨兵-单元标题" not in blob
    assert "哨兵-概念摘要" not in blob
    assert "注入-" not in blob
    # 空白上下文下 critic 仅能看到学习者自身输入（映射为 learner_production），无任何账本字段
    assert "（无可见上下文）" not in blob
    assert "learner_production：我的空白上下文解释" in blob
    assert "我的空白上下文解释" in blob


def test_blank_context_answer_machine_maps_question(client, csrf_headers, fake):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers, title="哨兵-答案机标题")
    r = client.post(
        "/api/v1/roles/answer_machine/call",
        headers=csrf_headers,
        json={
            "unit_id": uid,
            "blank_context": True,
            "learner_input": "什么是空白上下文？",
            "context": {"unit_title": "注入-答案机标题", "topic": "注入-答案机主题"},
        },
    )
    assert r.json()["code"] == "0", r.text
    system = fake.calls[0][0].content
    user = fake.calls[0][1].content
    assert "question：什么是空白上下文？" in system
    assert "unit_title：" not in system
    assert "注入-" not in system
    assert user == "什么是空白上下文？"


def test_input_preview_blank_only_learner_input(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers, title="哨兵-预览标题")
    db.write(
        pid,
        "concept",
        {"id": "sentinel-p", "name": "哨兵-预览概念", "verify_status": "未验证"},
        "user",
    )

    blank = client.get(
        f"/api/v1/units/{uid}/input-preview",
        params={
            "role": "answer_machine",
            "blank_context": "true",
            "learner_input": "我的预览问题",
        },
    ).json()["data"]
    assert blank["visible_fields"] == ["question"]
    assert blank["visible_values"]["question"] == "我的预览问题"
    assert "unit_title" not in blank["visible_fields"]
    assert "concept_summary" not in blank["visible_fields"]
    assert "哨兵-" not in str(blank["visible_values"])

    # 对照：非空白预览仍含单元/概念字段
    normal = client.get(
        f"/api/v1/units/{uid}/input-preview", params={"role": "topic_picker"}
    ).json()["data"]
    assert "unit_title" in normal["visible_fields"]
    assert "concept_summary" in normal["visible_fields"]
    assert "哨兵-预览标题" in normal["visible_values"]["unit_title"]
