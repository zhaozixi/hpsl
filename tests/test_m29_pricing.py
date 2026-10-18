"""M29 / P1-01：DeepSeek 价格登记与"未登记价格"可见性。"""

from hpsl import db
from hpsl.engine import cost as cost_engine
from hpsl.models import registry
from hpsl.models.base import Reply


def _project(client, csrf, name="价格项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    return r.json()["data"]["id"]


def _session(pid, sid="s-cost"):
    db.write(pid, "session", {"id": sid, "unit_id": None, "started_at": db._now_utc()}, "user")
    return sid


def test_deepseek_models_are_priced():
    for model in ("deepseek-flash", "deepseek-v4-pro"):
        p = registry.pricing(model)
        assert p, model
        assert p["in_per_1k"] > 0 and p["out_per_1k"] > 0
    # 1M 输入 + 1M 输出 ≈ 10 CNY / 7.2 ≈ $1.39（高峰价）
    p = registry.pricing("deepseek-flash")
    total = (1_000_000 * p["in_per_1k"] + 1_000_000 * p["out_per_1k"]) / 1000
    assert 1.3 < total < 1.5


def test_record_call_flash_cost_positive(client, csrf_headers):
    pid = _project(client, csrf_headers)
    sid = _session(pid)
    vo = cost_engine.record_call(
        "clerk",
        Reply(
            text="ok",
            finish_reason="stop",
            model="deepseek-flash",
            raw={"usage": {"prompt_tokens": 1000, "completion_tokens": 1000}},
        ),
        sid,
        None,
    )
    assert vo.cost_usd > 0
    row = db.query(pid, "SELECT cost FROM llm_call")[0]
    assert row["cost"] == vo.cost_usd

    monthly = client.get("/api/v1/cost/monthly").json()["data"]
    assert monthly["unpriced_models"] == []


def test_unpriced_model_is_flagged_not_silent(client, csrf_headers):
    pid = _project(client, csrf_headers)
    sid = _session(pid, "s-unpriced")
    cost_engine.record_call(
        "clerk",
        Reply(
            text="ok",
            finish_reason="stop",
            model="mystery-model",
            raw={"usage": {"prompt_tokens": 1000, "completion_tokens": 1000}},
        ),
        sid,
        None,
    )
    monthly = client.get("/api/v1/cost/monthly").json()["data"]
    assert "mystery-model" in monthly["unpriced_models"]
    assert monthly["cumulative_usd"] == 0.0  # 未登记 → 0，但已被显式标出
    page = client.get("/budget")
    assert "未登记价格的模型" in page.text
    assert "mystery-model" in page.text
    assert db.count_user_objects(pid) == 16
