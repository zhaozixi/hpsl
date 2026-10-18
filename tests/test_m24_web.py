"""M24 页面层测试：P0-B 表单 / 角色面板 / 三个交互修复（P1-08/09/10/14）。"""

import pytest
from jinja2 import Environment, FileSystemLoader

from hpsl import db
from hpsl.models.base import Reply
from hpsl.routes import roles as roles_routes


def _project(client, csrf, name="Web项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "目标"})
    return r.json()["data"]["id"]


def _unit(client, csrf, title="网页单元"):
    r = client.post("/api/v1/units", headers=csrf, json={"title": title})
    return r.json()["data"]["id"]


def _htmx(csrf):
    return {**csrf, "HX-Request": "true"}


def _to_judge(client, csrf, uid):
    for cur, nxt in (("INIT", "PICK"), ("PICK", "LEARN")):
        client.post(f"/api/v1/units/{uid}/move", headers=csrf, json={"cur": cur, "nxt": nxt})
    client.post(
        f"/api/v1/units/{uid}/production", headers=csrf, json={"production": "产出"}
    )
    for cur, nxt in (("LEARN", "VERIFY"), ("VERIFY", "JUDGE")):
        client.post(f"/api/v1/units/{uid}/move", headers=csrf, json={"cur": cur, "nxt": nxt})


def test_unit_index_has_create_form(client, csrf_headers):
    _project(client, csrf_headers)
    r = client.get("/unit")
    assert r.status_code == 200
    assert "新建单元" in r.text
    assert 'hx-post="/api/v1/units"' in r.text
    assert "单元由学习会话流程产生" not in r.text


def test_unit_detail_has_new_sections(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.get(f"/unit/{uid}")
    assert r.status_code == 200
    for phrase in ("产出（回归主线）", "记录一次尝试", "判定闭环", "AI 辅助（角色调用）"):
        assert phrase in r.text, phrase
    assert f"/api/v1/units/{uid}/close" in r.text
    assert "/api/v1/roles/questioner/call" in r.text


def test_create_unit_htmx_updates_list_in_place(client, csrf_headers):
    """M35：建单元改局部刷新——不再 HX-Refresh 整页刷新（那会让提示瞬间消失）。"""
    pid = _project(client, csrf_headers)
    r = client.post(
        "/api/v1/units", headers=_htmx(csrf_headers), data={"title": "HTMX 建单元"}
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") is None, "不应再整页刷新"
    # 结果区提示 + 列表区 OOB 就地替换，两者都要有
    assert "已创建单元" in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert "HTMX 建单元" in r.text
    assert db.query(pid, "SELECT count(*) c FROM unit")[0]["c"] == 1


def test_production_htmx_fragment(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/units/{uid}/production",
        headers=_htmx(csrf_headers),
        data={"production": "一段产出"},
    )
    assert "产出已保存" in r.text


def test_attempt_htmx_fragment(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    r = client.post(
        f"/api/v1/units/{uid}/attempts",
        headers=_htmx(csrf_headers),
        data={"kind": "重述", "self_score": "0.8"},
    )
    assert "已记录一次「重述」" in r.text


def test_close_htmx_returns_panel_and_oob(client, csrf_headers):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    _to_judge(client, csrf_headers, uid)
    r = client.post(
        f"/api/v1/units/{uid}/close",
        headers=_htmx(csrf_headers),
        data={"confirmed_score": "0.9"},
    )
    assert 'id="wf-panel"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert "已闭环" in r.text
    assert "INTEGRATE" in r.text


class _FakeAdapter:
    name = "fake"
    supports_json = False

    def complete(self, messages, *, json_schema=None, stream=False):
        return Reply(
            text="<script>alert(1)</script> 正常文本",
            finish_reason="stop",
            model="deepseek-chat",
            raw={"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )


def test_role_call_htmx_escapes_and_shows_cost(client, csrf_headers, monkeypatch):
    _project(client, csrf_headers)
    uid = _unit(client, csrf_headers)
    adapter = _FakeAdapter()
    monkeypatch.setattr(roles_routes.registry, "pick_default_model", lambda role=None: "deepseek-chat")
    monkeypatch.setattr(roles_routes.registry, "build_adapter", lambda model_id, **kw: adapter)
    r = client.post(
        f"/api/v1/roles/questioner/call",
        headers=_htmx(csrf_headers),
        data={"unit_id": uid, "learner_input": "我的重述"},
    )
    assert 'class="pre-wrap"' in r.text
    assert "<script>" not in r.text  # 模型输出被转义
    assert "&lt;script&gt;" in r.text
    assert "tokens 7 / 3" in r.text


def test_settings_uses_fetch_not_htmx_ajax(client, csrf_headers):
    _project(client, csrf_headers)
    html = client.get("/settings").text
    assert "fetch('/api/v1/projects/" in html
    assert "htmx.ajax('PUT'" not in html


def test_quota_modal_confirmed_has_no_overlay():
    env = Environment(loader=FileSystemLoader("hpsl/templates"))
    t = env.get_template("_quota_modal.html")
    cost = {"status": "warn", "cumulative_usd": 10.5, "budget_cap_usd": 10.0}
    confirmed = t.render(session_id="s1", confirmed=True, cost=cost)
    assert 'class="modal"' not in confirmed
    assert "已确认继续" in confirmed
    pending = t.render(session_id="s1", confirmed=False, cost=cost)
    assert 'class="modal"' in pending


def test_global_error_toast_present():
    js = open("hpsl/static/app.js", encoding="utf-8").read()
    assert "htmx:responseError" in js
    css = open("hpsl/static/app.css", encoding="utf-8").read()
    assert "p.ok{" in css
    assert ".toast" in css


def test_backup_htmx_updates_list_in_place(client, csrf_headers):
    pid = _project(client, csrf_headers)
    r = client.post(
        f"/api/v1/projects/{pid}/backup", headers=_htmx(csrf_headers)
    )
    assert r.status_code == 200
    # 不再整页刷新（HX-Refresh 会吞掉"已生成备份"提示）：改为主片段 + OOB 同步列表与计数
    assert r.headers.get("HX-Refresh") is None
    assert "已生成备份" in r.text
    assert 'id="backup-list" hx-swap-oob="true"' in r.text
    assert 'id="backup-count" hx-swap-oob="true"' in r.text
