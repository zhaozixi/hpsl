"""M36 / C12：固定三问结构化落库 + 自动三模型交叉验证。

覆盖：
- 固定三问：写入成功（attempt.kind == "三问" / rubric JSON 内容正确 / 绑定单元）、
  页面回显与表单接线、缺字段 / 空白 / 超长 → C400001、未知单元 → 既有错误码。
- HTMX 成功：提示文案 + `hpsl:refresh` 事件，不再 `HX-Refresh` 整页刷新。
- 自动交叉验证：按 provider 去重取 3 家 → 3 条 answers / 3 条 llm_call / 6 条 turn；
  只有 1 家 provider → C400001；未知单元 → C400001；缺 question → C400001。
- 零 DDL：全部流程后库对象仍为 16。

全部使用 fake adapter（monkeypatch `registry.build_adapter`），不发真实网络请求。
"""

import json

import pytest

from hpsl import db
from hpsl.models import registry as model_registry
from hpsl.models.base import Reply

TQ = {
    "what_problem": "解决「按组汇总要先拆再合」的问题",
    "how_it_works": "先按列分组，再对每组分别聚合，最后合并结果",
    "when_not_applicable": "数据量极大且分组键高度离散时，内存与耗时不可接受",
}


class _FakeAdapter:
    """记录 messages 的假适配器（不发网络请求）。"""

    name = "fake"
    supports_json = False

    def __init__(self, text="模型回答", model="deepseek-flash"):
        self.text = text
        self.model = model
        self.calls = []

    def complete(self, messages, *, json_schema=None, stream=False):
        self.calls.append(messages)
        return Reply(
            text=self.text,
            finish_reason="stop",
            model=self.model,
            raw={"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )


@pytest.fixture
def fake_adapters(monkeypatch):
    """按 model_id 返回独立假适配器（每个模型一次调用可分别断言）。"""
    made: dict[str, _FakeAdapter] = {}

    def _build(model_id, **kwargs):
        if model_id not in made:
            made[model_id] = _FakeAdapter(
                text=f"{model_id} 的答案", model=model_id
            )
        return made[model_id]

    monkeypatch.setattr(model_registry, "build_adapter", _build)
    return made


def _project(client, csrf, name="C12 固定三问"):
    r = client.post(
        "/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"}
    )
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _unit(client, csrf, title="单元C12"):
    r = client.post("/api/v1/units", headers=csrf, json={"title": title})
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _htmx(csrf):
    return {**csrf, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# 固定三问
# ---------------------------------------------------------------------------
def test_three_questions_records_attempt_and_rubric(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)

    r = client.post(
        f"/api/v1/units/{uid}/three-questions", headers=csrf_headers, json=TQ
    )
    assert r.json()["code"] == "0", r.text
    aid = r.json()["data"]["attempt_id"]
    assert aid

    rows = db.query(pid, "SELECT * FROM attempt")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == aid
    assert row["unit_id"] == uid
    assert row["kind"] == "三问"
    assert row["ts"]
    # rubric 是 JSON 列：落库为 {"three_questions": {...}}，三问逐字保留
    assert json.loads(row["rubric"]) == {"three_questions": TQ}
    # 写入经唯一写入口，audit_log 同步追加
    audit = db.query(
        pid,
        "SELECT count(*) c FROM audit_log WHERE table_name='attempt' AND row_id=?",
        (aid,),
    )
    assert audit[0]["c"] == 1


def test_three_questions_htmx_returns_toast_not_refresh(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)

    r = client.post(
        f"/api/v1/units/{uid}/three-questions",
        headers=_htmx(csrf_headers),
        data=TQ,
    )
    assert r.status_code == 200
    assert "已记录固定三问" in r.text
    assert r.headers.get("HX-Refresh") is None, "不应整页刷新（提示会被冲掉）"
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["hpsl:refresh"]["msg"] == "已记录固定三问"


def test_unit_detail_shows_form_and_recorded_three_questions(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    client.post(
        f"/api/v1/units/{uid}/three-questions", headers=csrf_headers, json=TQ
    )

    r = client.get(f"/unit/{uid}")
    assert r.status_code == 200
    # 表单接线：三个字段 + 长度限制 + 结果锚点
    assert f'hx-post="/api/v1/units/{uid}/three-questions"' in r.text
    assert 'hx-target="#three-q-result"' in r.text
    for name in ("what_problem", "how_it_works", "when_not_applicable"):
        assert f'name="{name}"' in r.text
    assert 'maxlength="1000"' in r.text
    assert 'id="three-q-result"' in r.text
    # 已记录内容回显（页面下方展示）
    for value in TQ.values():
        assert value in r.text
    assert "最近记录：" in r.text
    # 既有编号分区 / 状态机门控保持原样
    assert "④ 记录一次尝试" in r.text
    assert "⑤ 判定闭环" in r.text


@pytest.mark.parametrize(
    "missing", ["what_problem", "how_it_works", "when_not_applicable"]
)
def test_three_questions_missing_field_rejected(client, csrf_headers, missing):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    body = {**TQ}
    del body[missing]

    r = client.post(
        f"/api/v1/units/{uid}/three-questions", headers=csrf_headers, json=body
    )
    assert r.json()["code"] == "C400001", r.text
    assert db.query(pid, "SELECT count(*) c FROM attempt")[0]["c"] == 0


def test_three_questions_blank_field_rejected(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    body = {**TQ, "what_problem": "   "}

    r = client.post(
        f"/api/v1/units/{uid}/three-questions", headers=csrf_headers, json=body
    )
    assert r.json()["code"] == "C400001", r.text
    assert db.query(pid, "SELECT count(*) c FROM attempt")[0]["c"] == 0


@pytest.mark.parametrize(
    "field", ["what_problem", "how_it_works", "when_not_applicable"]
)
def test_three_questions_overlong_rejected(client, csrf_headers, field):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    body = {**TQ, field: "字" * 1001}

    r = client.post(
        f"/api/v1/units/{uid}/three-questions", headers=csrf_headers, json=body
    )
    assert r.json()["code"] == "C400001", r.text
    assert db.query(pid, "SELECT count(*) c FROM attempt")[0]["c"] == 0


def test_three_questions_boundary_1000_chars_accepted(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    body = {**TQ, "what_problem": "字" * 1000}

    r = client.post(
        f"/api/v1/units/{uid}/three-questions", headers=csrf_headers, json=body
    )
    assert r.json()["code"] == "0", r.text
    row = db.query(pid, "SELECT rubric FROM attempt")[0]
    assert len(json.loads(row["rubric"])["three_questions"]["what_problem"]) == 1000


def test_three_questions_unknown_unit_uses_existing_error(client, csrf_headers):
    pid = _project(client, csrf_headers)
    r = client.post(
        "/api/v1/units/no-such-unit/three-questions",
        headers=csrf_headers,
        json=TQ,
    )
    assert r.json()["code"] == "A020001", r.text
    assert db.query(pid, "SELECT count(*) c FROM attempt")[0]["c"] == 0


def test_unit_detail_without_three_questions_shows_empty_state(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.get(f"/unit/{uid}")
    assert r.status_code == 200
    assert "本单元尚未记录固定三问" in r.text


# ---------------------------------------------------------------------------
# 自动三模型交叉验证
# ---------------------------------------------------------------------------
def test_auto_cross_check_uses_three_providers(client, csrf_headers, fake_adapters):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    question = "为什么 groupby 会按键排序？"

    r = client.post(
        f"/api/v1/units/{uid}/answers/cross-check/auto",
        headers=csrf_headers,
        json={"question": question},
    )
    assert r.json()["code"] == "0", r.text
    data = r.json()["data"]

    # models：按 provider 去重后的 3 家；answers 与之一一对应
    assert len(data["models"]) == 3
    providers = {model_registry.get_model(m).provider for m in data["models"]}
    assert providers == {"deepseek", "qwen", "zhipu"}
    assert [a["model"] for a in data["answers"]] == data["models"]
    assert [a["text"] for a in data["answers"]] == [
        f"{m} 的答案" for m in data["models"]
    ]
    for a in data["answers"]:
        assert a["tokens_in"] == 10 and a["tokens_out"] == 5
        assert a["cost_usd"] >= 0.0
    assert data["session_id"]

    # 3 家各调一次、各自记账（llm_call 3 行），turn = 3 × (user + answer_machine) = 6
    calls = db.query(pid, "SELECT * FROM llm_call ORDER BY rowid")
    assert len(calls) == 3
    assert [c["model"] for c in calls] == data["models"]
    assert all(c["role"] == "answer_machine" for c in calls)
    assert all(c["session_id"] == data["session_id"] for c in calls)

    turns = db.query(pid, "SELECT * FROM turn ORDER BY id")
    assert len(turns) == 6
    assert [t["role"] for t in turns] == ["user", "answer_machine"] * 3
    assert [t["content"] for t in turns if t["role"] == "user"] == [question] * 3

    sessions = db.query(pid, "SELECT * FROM session")
    assert len(sessions) == 1
    assert sessions[0]["id"] == data["session_id"]
    assert sessions[0]["unit_id"] == uid

    # 每家模型真实被调用一次，system 上下文含单元标题与问题
    for model_id in data["models"]:
        assert len(fake_adapters[model_id].calls) == 1
    sent = "\n".join(m.content for m in fake_adapters[data["models"][0]].calls[0])
    assert "单元C12" in sent and question in sent


def test_auto_cross_check_single_provider_rejected(
    client, csrf_headers, monkeypatch
):
    all_models = model_registry.list_models()
    only = [m for m in all_models if m.provider == all_models[0].provider]
    assert len(only) >= 1
    monkeypatch.setattr(model_registry, "list_models", lambda: only)

    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/units/{uid}/answers/cross-check/auto",
        headers=csrf_headers,
        json={"question": "问题"},
    )
    assert r.json()["code"] == "C400001", r.text
    # 失败前不创建 session、不产生记账
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0
    assert db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"] == 0
    assert db.query(pid, "SELECT count(*) c FROM turn")[0]["c"] == 0


def test_auto_cross_check_unknown_unit(client, csrf_headers):
    pid = _project(client, csrf_headers)
    r = client.post(
        "/api/v1/units/no-such-unit/answers/cross-check/auto",
        headers=csrf_headers,
        json={"question": "问题"},
    )
    assert r.json()["code"] == "C400001", r.text
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0
    assert db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"] == 0


def test_auto_cross_check_requires_question(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/units/{uid}/answers/cross-check/auto",
        headers=csrf_headers,
        json={},
    )
    assert r.json()["code"] == "C400001", r.text
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0


# ---------------------------------------------------------------------------
# 零 DDL
# ---------------------------------------------------------------------------
def test_zero_ddl_after_c12_flows(client, csrf_headers, fake_adapters):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)

    r1 = client.post(
        f"/api/v1/units/{uid}/three-questions", headers=csrf_headers, json=TQ
    )
    assert r1.json()["code"] == "0", r1.text
    r2 = client.post(
        f"/api/v1/units/{uid}/answers/cross-check/auto",
        headers=csrf_headers,
        json={"question": "问题"},
    )
    assert r2.json()["code"] == "0", r2.text

    # 零 DDL：仍为 16 个既有库对象（无新表/索引/触发器）
    assert db.count_user_objects(pid) == 16
