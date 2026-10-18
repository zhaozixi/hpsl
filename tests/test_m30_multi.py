"""M30（P2-03 / P2-04）：多模型交叉验证 + 多挑错 + 评分要点核对。

覆盖：
- 多挑错：3 个模型 → 3 条 claim_check，同源/异源标记与落账字段正确（BD-03 同源降级）。
- 两两同源矩阵 same_source_pairs（复用 heterogeneous）。
- 多答案机 cross-check：2 个模型 → answers 2 条 / llm_call 2 条 / turn 4 条（各调一次、各自记账）。
- 未知模型 → A040003（明确断言）。
- 评分要点核对：核对意见写入 claim_check（diff 含模型输出）。
- attempt 不存在 / rubric 为空 → C400001。
- 零 DDL：全部操作后库对象仍为 16。

全部使用 fake adapter（monkeypatch `registry.build_adapter`），不发真实网络请求。
说明：按要求不编辑 `hpsl/app.py`，本测试在导入时把 `multi.api_router` 挂到全局 app；
若装配方已在 app.py 挂载（路径已存在）则跳过，避免重复注册。
"""

import json

import pytest

from hpsl import db
from hpsl.app import app
from hpsl.engine import claim_check as cc
from hpsl.models import registry as model_registry
from hpsl.models.base import Reply
from hpsl.routes import multi as multi_routes

# 模型（models.yaml）：gpt-4o-mini(family gpt4o) / claude-3-5-haiku(family claude3) /
# deepseek-chat(family deepseek, derived_from gpt4o) → deepseek 与 gpt 同源，claude 异源。
GPT = "gpt-4o-mini"
CLAUDE = "claude-3-5-haiku-20241022"
DEEPSEEK = "deepseek-chat"  # derived_from gpt4o → 与 GPT 同源

# 不编辑 app.py：测试自行挂载 multi router（装配方已挂载则跳过）
_MULTI_PATH = "/api/v1/units/{uid}/critic/multi"
if _MULTI_PATH not in {getattr(r, "path", None) for r in app.routes}:
    app.include_router(multi_routes.api_router)


class _FakeAdapter:
    """记录 messages 的假适配器（不发网络请求）。"""

    name = "fake"
    supports_json = False

    def __init__(self, text="模型回答", model=GPT):
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
            made[model_id] = _FakeAdapter(text=f"{model_id} 的答案", model=model_id)
        return made[model_id]

    monkeypatch.setattr(model_registry, "build_adapter", _build)
    return made


def _project(client, csrf, name="M30 多模型"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _unit(client, csrf, title="单元M30"):
    r = client.post("/api/v1/units", headers=csrf, json={"title": title})
    assert r.json()["code"] == "0", r.text
    return r.json()["data"]["id"]


def _attempt(pid, aid, uid, rubric):
    db.write(
        pid,
        "attempt",
        {
            "id": aid,
            "unit_id": uid,
            "concept_id": None,
            "kind": "边界题",
            "rubric": rubric,
            "self_score": None,
            "confirmed_score": None,
            "seconds": 30,
            "ts": db._now_utc(),
        },
        "user",
    )


# ---------------------------------------------------------------------------
# P2-04 多挑错
# ---------------------------------------------------------------------------
def test_multi_critic_three_models_flags_same_source(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    claim = "断言：groupby 默认会按键排序"
    r = client.post(
        f"/api/v1/units/{uid}/critic/multi",
        headers=csrf_headers,
        json={
            "claim": claim,
            "model_ids": [GPT, CLAUDE, DEEPSEEK],
            "answer_model_id": GPT,
            "is_keypoint": True,
        },
    )
    assert r.json()["code"] == "0", r.text
    data = r.json()["data"]

    # 3 个模型 → 3 条挑错；GPT（自比）/ DeepSeek（蒸馏自 gpt4o）同源，Claude 异源
    assert len(data["created"]) == 3
    assert [c["model"] for c in data["created"]] == [GPT, CLAUDE, DEEPSEEK]
    assert [c["same_source"] for c in data["created"]] == [True, False, True]
    # 两两同源矩阵随响应返回（3 个模型 → 3 个 pair）
    assert len(data["pairs"]) == 3

    rows = db.query(pid, "SELECT * FROM claim_check")
    assert len(rows) == 3
    by_critic = {json.loads(row["providers"])["critic"]: row for row in rows}
    assert set(by_critic) == {GPT, CLAUDE, DEEPSEEK}
    for row in rows:
        prov = json.loads(row["providers"])
        assert prov["answer"] == GPT
        assert row["agree"] is None  # 多模型挑错均未评（交由用户仲裁）
    for critic in (GPT, DEEPSEEK):
        assert by_critic[critic]["user_ruling"] is None
        assert "同源降级" in (by_critic[critic]["diff"] or "")
    # 异源行：claim=原 claim、residual=0
    assert by_critic[CLAUDE]["claim"] == claim
    assert by_critic[CLAUDE]["residual"] == 0
    assert json.loads(by_critic[CLAUDE]["providers"])["same_source"] is False


def test_same_source_pairs_matrix():
    pairs = cc.same_source_pairs([GPT, CLAUDE, DEEPSEEK])
    assert len(pairs) == 3
    assert [p["a"] for p in pairs] == [GPT, GPT, CLAUDE]
    assert [p["b"] for p in pairs] == [CLAUDE, DEEPSEEK, DEEPSEEK]
    by_pair = {(p["a"], p["b"]): p["same_source"] for p in pairs}
    assert by_pair[(GPT, CLAUDE)] is False
    assert by_pair[(GPT, DEEPSEEK)] is True   # 蒸馏关系 → 同源
    assert by_pair[(CLAUDE, DEEPSEEK)] is False
    # 单模型 / 空列表 → 无 pair
    assert cc.same_source_pairs([GPT]) == []
    assert cc.same_source_pairs([]) == []


# ---------------------------------------------------------------------------
# P2-03 多答案机交叉验证
# ---------------------------------------------------------------------------
def test_cross_check_two_models_records_and_turns(client, csrf_headers, fake_adapters):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    mids = [GPT, CLAUDE]
    question = "为什么 groupby 会按键排序？"
    r = client.post(
        f"/api/v1/units/{uid}/answers/cross-check",
        headers=csrf_headers,
        json={"question": question, "model_ids": mids},
    )
    assert r.json()["code"] == "0", r.text
    data = r.json()["data"]

    # 每模型一条答案，字段齐全
    assert [a["model"] for a in data["answers"]] == mids
    assert data["answers"][0]["text"] == f"{GPT} 的答案"
    assert data["answers"][1]["text"] == f"{CLAUDE} 的答案"
    for a in data["answers"]:
        assert a["tokens_in"] == 10 and a["tokens_out"] == 5
        assert a["cost_usd"] >= 0.0
    assert data["session_id"]

    # 每模型一次调用、各自记账（llm_call 2 条），会话归属单元
    calls = db.query(pid, "SELECT * FROM llm_call ORDER BY rowid")
    assert len(calls) == 2
    assert [c["model"] for c in calls] == mids
    assert all(c["role"] == "answer_machine" for c in calls)
    assert all(c["session_id"] == data["session_id"] for c in calls)
    # 每模型各写 user + 角色两轮 turn → 4 条
    turns = db.query(pid, "SELECT * FROM turn ORDER BY id")
    assert len(turns) == 4
    assert [t["role"] for t in turns] == ["user", "answer_machine", "user", "answer_machine"]
    assert [t["content"] for t in turns if t["role"] == "user"] == [question, question]

    # 两个模型各自真实被调用一次，且 system 上下文含单元标题与问题
    assert len(fake_adapters[GPT].calls) == 1
    assert len(fake_adapters[CLAUDE].calls) == 1
    sent = "\n".join(m.content for m in fake_adapters[GPT].calls[0])
    assert "单元M30" in sent and question in sent

    sessions = db.query(pid, "SELECT * FROM session")
    assert len(sessions) == 1
    assert sessions[0]["id"] == data["session_id"]
    assert sessions[0]["unit_id"] == uid


def test_cross_check_unknown_model_fails_closed(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/units/{uid}/answers/cross-check",
        headers=csrf_headers,
        json={"question": "问题", "model_ids": ["no-such-model"]},
    )
    # 明确断言：未注册/不可用模型统一走 A040003（密钥缺失或无效同一出口）
    assert r.json()["code"] == "A040003", r.text
    # 失败前不创建 session、不产生记账（fail closed）
    assert db.query(pid, "SELECT count(*) c FROM session")[0]["c"] == 0
    assert db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"] == 0


# ---------------------------------------------------------------------------
# P2-04 评分要点核对
# ---------------------------------------------------------------------------
def test_rubric_verify_writes_claim_check(client, csrf_headers, fake_adapters):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    aid = "att-m30-1"
    rubric = {"criteria": ["能解释 groupby 的排序行为", "能指出 sort 参数的作用"]}
    _attempt(pid, aid, uid, rubric)
    feedback = "疑点：要点 2 与题干不符，建议补充稳定性说明"
    fake_adapters[GPT] = _FakeAdapter(text=feedback, model=GPT)

    r = client.post(
        f"/api/v1/attempts/{aid}/rubric/verify",
        headers=csrf_headers,
        json={"model_id": GPT},
    )
    assert r.json()["code"] == "0", r.text
    data = r.json()["data"]
    assert data["feedback"] == feedback

    rows = db.query(pid, "SELECT * FROM claim_check")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == data["claim_id"]
    assert row["claim"] == "评分要点核对"
    assert feedback in (row["diff"] or "")          # diff 含模型输出
    assert row["agree"] is None
    assert row["residual"] == 0
    assert row["unit_id"] == uid
    assert json.loads(row["providers"]) == {"verifier": GPT, "attempt_id": aid}

    # 调用消息：system=核对指令；user=评分要点 JSON
    sent = fake_adapters[GPT].calls[0]
    assert sent[0].role == "system" and "核对评分要点是否有误" in sent[0].content
    assert sent[1].role == "user"
    assert "groupby" in sent[1].content
    assert json.loads(sent[1].content) == rubric


def test_rubric_verify_attempt_missing_or_empty_rubric(client, csrf_headers):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)

    # attempt 不存在 → C400001
    r = client.post(
        "/api/v1/attempts/no-such-attempt/rubric/verify",
        headers=csrf_headers,
        json={"model_id": GPT},
    )
    assert r.json()["code"] == "C400001", r.text

    # rubric 为空（NULL / 空 JSON 对象）→ C400001
    _attempt(pid, "att-null", uid, None)
    _attempt(pid, "att-empty", uid, {})
    for aid in ("att-null", "att-empty"):
        r = client.post(
            f"/api/v1/attempts/{aid}/rubric/verify",
            headers=csrf_headers,
            json={"model_id": GPT},
        )
        assert r.json()["code"] == "C400001", r.text
    assert db.query(pid, "SELECT count(*) c FROM claim_check")[0]["c"] == 0


# ---------------------------------------------------------------------------
# 缺省模型 + 零 DDL
# ---------------------------------------------------------------------------
def test_multi_critic_default_model_ids(client, csrf_headers, monkeypatch):
    monkeypatch.setattr(model_registry, "pick_default_model", lambda role=None: CLAUDE)
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/units/{uid}/critic/multi",
        headers=csrf_headers,
        json={"claim": "某断言"},
    )
    assert r.json()["code"] == "0", r.text
    data = r.json()["data"]
    assert [c["model"] for c in data["created"]] == [CLAUDE]
    assert data["created"][0]["same_source"] is False  # 未给 answer_model_id，按异源处理
    assert data["pairs"] == []


def test_zero_ddl_after_multi_flows(client, csrf_headers, fake_adapters):
    pid = _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    aid = "att-m30-ddl"
    _attempt(pid, aid, uid, {"criteria": ["要点一"]})

    r1 = client.post(
        f"/api/v1/units/{uid}/critic/multi",
        headers=csrf_headers,
        json={"claim": "断言", "model_ids": [GPT, CLAUDE], "answer_model_id": GPT},
    )
    assert r1.json()["code"] == "0", r1.text
    r2 = client.post(
        f"/api/v1/units/{uid}/answers/cross-check",
        headers=csrf_headers,
        json={"question": "问题", "model_ids": [GPT]},
    )
    assert r2.json()["code"] == "0", r2.text
    r3 = client.post(
        f"/api/v1/attempts/{aid}/rubric/verify",
        headers=csrf_headers,
        json={"model_id": CLAUDE},
    )
    assert r3.json()["code"] == "0", r3.text

    # 零 DDL：仍为 16 个既有库对象（无新表/索引/触发器）
    assert db.count_user_objects(pid) == 16
