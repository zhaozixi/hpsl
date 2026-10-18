"""M36 / P1（REF-3 B2/B3/B4/B7）回归：HTMX 成功提示、软提示误删、outerHTML 锚点、新建项目切换。

覆盖：
- B2：8 处 `HX-Refresh: true` 改为 `HX-Trigger: {"hpsl:refresh": …}`；响应不再带 HX-Refresh。
- B3：关闭软提示成功 → 结果区提示 + OOB 置空该行；失败 → 全局错误片段（行不删）。
- B4：arbitrate / reviews start / budget confirm 失败仍返回带原锚点的片段。
- B7：/projects/new 成功后调用 `/api/v1/projects/{newId}/switch`，再跳 /unit。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hpsl import db, projects
from hpsl.engine import claim_check as cc
from hpsl.routes._htmx import refresh_with_toast

_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _project(client, csrf, name="P1 回归", goal="目标"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": goal})
    body = r.json()
    assert body and body.get("code") == "0", r.text
    d = body["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf)
    return d


def _htmx(csrf):
    return {**csrf, "HX-Request": "true"}


def _assert_trigger(r):
    """成功响应：有 HX-Trigger(hpsl:refresh)，绝不带 HX-Refresh。"""
    assert r.status_code == 200, r.text
    assert r.headers.get("HX-Refresh") is None, "不应再整页刷新（提示会被冲掉）"
    assert "hpsl:refresh" in r.headers.get("HX-Trigger", ""), r.headers


def _unit_and_gap(pid, *, gid="g1", defer_until=None):
    db.write(pid, "unit", {"id": "u1", "title": "单元甲", "status": "INIT"}, "user")
    now = db._now_utc()
    ctx = json.dumps({"defer_until": defer_until}, ensure_ascii=False) if defer_until else None
    db.write(
        pid,
        "gap",
        {
            "id": gid,
            "unit_id": "u1",
            "session_id": None,
            "level": 2,
            "text": "缺口甲",
            "status": "待补",
            "context": ctx,
            "created_at": now,
            "updated_at": now,
        },
        "user",
    )


# ---------------------------------------------------------------------------
# B2：统一「成功提示 + 刷新」
# ---------------------------------------------------------------------------
def test_refresh_with_toast_contract():
    """助手本身：HTML 转义 + HX-Trigger JSON 可解析（中文经 \\uXXXX 走 latin-1 头）。"""
    r = refresh_with_toast("已创建 <概念>")
    body = r.body.decode("utf-8")
    assert "&lt;概念&gt;" in body
    assert r.headers.get("HX-Refresh") is None
    payload = json.loads(r.headers["HX-Trigger"])
    assert payload["hpsl:refresh"]["msg"] == "已创建 <概念>"


def test_app_js_listens_refresh_event():
    js = (_ROOT / "hpsl" / "static" / "app.js").read_text(encoding="utf-8")
    assert "hpsl:refresh" in js
    assert "hpslToast(msg)" in js
    assert "window.location.reload()" in js


def test_graph_writes_trigger_refresh(client, csrf_headers):
    pid = _project(client, csrf_headers)["id"]
    h = {**_htmx(csrf_headers), "Content-Type": "application/x-www-form-urlencoded"}

    concept = client.post("/api/v1/concepts", headers=h, data={"name": "概念甲"})
    _assert_trigger(concept)
    assert "已创建概念" in concept.text
    cid = db.query(pid, "SELECT id FROM concept WHERE name='概念甲'")[0]["id"]

    card = client.post(
        "/api/v1/cards", headers=h, data={"concept_id": cid, "my_words": "我的理解"}
    )
    _assert_trigger(card)
    assert "已创建卡片" in card.text

    concept2 = client.post("/api/v1/concepts", headers=h, data={"name": "概念乙"})
    _assert_trigger(concept2)
    cid2 = db.query(pid, "SELECT id FROM concept WHERE name='概念乙'")[0]["id"]

    edge = client.post(
        "/api/v1/concept-edges", headers=h, data={"src": cid, "dst": cid2, "kind": "前置"}
    )
    _assert_trigger(edge)
    assert "已创建概念边" in edge.text


def test_retrieval_writes_trigger_refresh(client, csrf_headers, tmp_path):
    _project(client, csrf_headers)
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.md").write_text("# 标题\n\n内容", encoding="utf-8")

    created = client.post("/api/v1/sources", headers=_htmx(csrf_headers), data={"path": str(kb)})
    _assert_trigger(created)
    assert "已索引" in created.text

    sid = client.get("/api/v1/sources").json()["data"][0]["id"]
    deleted = client.request("DELETE", f"/api/v1/sources/{sid}", headers=_htmx(csrf_headers))
    _assert_trigger(deleted)
    assert "已删除该来源" in deleted.text


def test_insight_writes_trigger_refresh(client, csrf_headers):
    pid = _project(client, csrf_headers)["id"]
    _unit_and_gap(pid, defer_until="2999-01-01")

    batch = client.post(
        "/api/v1/gaps/status-batch",
        headers=_htmx(csrf_headers),
        data={"gid": ["g1"], "status": ["已补"]},
    )
    _assert_trigger(batch)
    assert "已更新 1 条缺口" in batch.text

    undefer = client.post("/api/v1/gaps/g1/undefer", headers=_htmx(csrf_headers))
    _assert_trigger(undefer)
    assert "已取消推迟" in undefer.text


def test_ops_undo_triggers_refresh(client, csrf_headers):
    pid = _project(client, csrf_headers)["id"]
    db.write(pid, "concept", {"id": "k", "name": "v1"}, "user")
    db.write(pid, "concept", {"id": "k", "name": "v2"}, "user")
    audit_id = client.get("/api/v1/ledger/audit").json()["data"][0]["id"]

    r = client.post(f"/api/v1/audit/{audit_id}/undo", headers=_htmx(csrf_headers))
    _assert_trigger(r)
    assert "已撤销" in r.text


# ---------------------------------------------------------------------------
# B3：软提示不再「失败也删行」
# ---------------------------------------------------------------------------
def test_ledger_soft_prompt_targets_result_not_row_removal():
    tpl = (_ROOT / "hpsl" / "templates" / "ledger.html").read_text(encoding="utf-8")
    assert 'hx-target="#prompt-result"' in tpl
    assert 'id="prompt-result"' in tpl
    assert "hx-on::after-request" not in tpl, "前端不再无条件删行"


def test_soft_prompt_dismiss_success_returns_oob(client, csrf_headers):
    _project(client, csrf_headers)
    target = "risk-forced-close"
    r = client.post(f"/api/v1/soft-prompts/{target}/dismiss", headers=_htmx(csrf_headers))
    assert r.status_code == 200
    assert "已忽略" in r.text
    assert 'id="prompt-' + target + '"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    # 成功路径不应触发整页刷新（只局部替换）
    assert r.headers.get("HX-Refresh") is None


def test_soft_prompt_dismiss_failure_shows_global_error(client, csrf_headers):
    """无激活项目 → A010001：只回全局错误片段，不返回 OOB（行不会被删）。"""
    r = client.post("/api/v1/soft-prompts/risk-forced-close/dismiss", headers=_htmx(csrf_headers))
    assert r.status_code == 200
    assert "result err" in r.text
    assert "A010001" in r.text
    assert "hx-swap-oob" not in r.text
    assert "已忽略" not in r.text


# ---------------------------------------------------------------------------
# B4：outerHTML 锚点兜底
# ---------------------------------------------------------------------------
def test_arbitrate_failure_keeps_cc_anchor(client, csrf_headers):
    _project(client, csrf_headers)
    pid = projects.active_project().id
    db.write(pid, "unit", {"id": "u1", "title": "单元甲", "status": "INIT"}, "user")
    cid = cc.write_claim_check(pid, claim="挑错甲", unit_id="u1", providers=[])

    r = client.post(
        f"/api/v1/units/u1/claim-check/{cid}/arbitrate",
        headers={**_htmx(csrf_headers), "Content-Type": "application/x-www-form-urlencoded"},
        data={"ruling": "瞎裁决"},
    )
    assert r.status_code == 200
    assert f'id="cc-{cid}"' in r.text
    assert "挑错甲" in r.text
    assert "非法仲裁" in r.text
    assert "C400001" in r.text


def test_arbitrate_failure_missing_row_still_anchored(client, csrf_headers):
    """不存在 id 的失败路径也要保留 #cc-{cid} 锚点（可重试）。"""
    _project(client, csrf_headers)
    r = client.post(
        "/api/v1/units/u1/claim-check/ghost/arbitrate",
        headers={**_htmx(csrf_headers), "Content-Type": "application/x-www-form-urlencoded"},
        data={"ruling": "瞎裁决"},
    )
    assert r.status_code == 200
    assert 'id="cc-ghost"' in r.text
    assert "C400001" in r.text


def test_review_start_failure_keeps_panel_anchor(client, csrf_headers):
    _project(client, csrf_headers)
    r = client.post("/api/v1/reviews/no-such-review/start", headers=_htmx(csrf_headers))
    assert r.status_code == 200
    assert 'id="panel-no-such-review"' in r.text
    assert "复测条目不存在" in r.text
    assert "A060001" in r.text


def test_budget_confirm_failure_keeps_modal_anchor(client, csrf_headers):
    # 无激活项目 → cost.confirm_continue 抛 A080002（BizError）
    r = client.post(
        "/api/v1/budget/confirm",
        headers={**_htmx(csrf_headers), "Content-Type": "application/x-www-form-urlencoded"},
        data={"session_id": "s1"},
    )
    assert r.status_code == 200
    assert 'id="quota-modal-s1"' in r.text
    assert "A080002" in r.text
    assert "重试确认" in r.text


# ---------------------------------------------------------------------------
# B7：新建项目后自动 switch
# ---------------------------------------------------------------------------
def test_create_page_calls_switch_after_create(client, csrf_headers):
    page = client.get("/projects/new")
    assert page.status_code == 200
    assert "/switch" in page.text
    assert "hpslAfterCreate" in page.text

    created = client.post(
        "/api/v1/projects",
        headers=_htmx(csrf_headers),
        data={"name": "新建即切换", "goal": "验证 B7"},
    )
    assert created.status_code == 200
    m = re.search(r'data-project-id="([0-9a-f]+)"', created.text)
    assert m, created.text
    pid = m.group(1)
    assert pid in created.text

    # 页面脚本调用的 switch 端点确实会写 last_active_at（重启恢复依赖它）
    switched = client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers)
    assert switched.json()["code"] == "0"
    assert "last_active_at" in projects.get_raw_config(pid)
