"""M35：启动恢复上次项目 + 错误文案分层 + 顶栏「当前项目」可见性。

设计约束（本文件即验收口径）：
- 恢复**只读**：不写 config、不追加 audit_log，唯一写入点仍是 switch；
- 恢复**显式**：只在 serve() 调用，模块导入期绝不自动执行（否则污染测试隔离）；
- 恢复**可关**：HPSL_NO_RESTORE_ACTIVE=1 / --no-restore；
- 恢复**可见**：base.html 顶栏常驻「当前项目」，由中间件注入 request.state；
- 错误文案分层：msg 只放中文结论，上游英文原文进 details 折叠区；JSON 契约不变。
"""

from __future__ import annotations

import json
import os

import pytest
from starlette.requests import Request

from hpsl import db
from hpsl import projects as proj
from hpsl.common.errors import BizError
from hpsl.models.openai_compat import _http_hint


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """独立 HPSL_HOME + 清空进程内 active 句柄，保证 restore 结果不受其它用例影响。"""
    monkeypatch.setenv("HPSL_HOME", str(tmp_path))
    monkeypatch.setattr(proj, "_active_pid", None)
    monkeypatch.setattr(proj, "_active_label", None)
    db.close_all()
    yield tmp_path
    db.close_all()


def _stamp(pid: str, ts: str) -> None:
    """直接写 config.last_active_at（等价于 switch 端点的留痕写入）。"""
    proj.set_config(pid, {"last_active_at": ts}, actor="user")


# ---------------------------------------------------------------------------
# 1. 恢复的取值逻辑
# ---------------------------------------------------------------------------
def test_restore_returns_none_when_no_history(fresh_home):
    assert proj.restore_last_active() is None
    assert proj.active_project() is None


def test_restore_picks_latest_timestamp(fresh_home):
    a = proj.create_project("项目甲", "目标甲")
    b = proj.create_project("项目乙", "目标乙")
    _stamp(a.id, "2026-10-01T00:00:00.000Z")
    _stamp(b.id, "2026-10-17T08:30:00.000Z")

    vo = proj.restore_last_active()
    assert vo is not None
    assert vo.id == b.id, "应恢复到 last_active_at 最新的项目"
    assert proj.active_label() == "项目乙"


def test_restore_ignores_projects_without_stamp(fresh_home):
    a = proj.create_project("项目甲", "目标甲")
    proj.create_project("项目乙", "目标乙")  # 从未 switch，无时间戳
    _stamp(a.id, "2026-10-05T12:00:00.000Z")

    vo = proj.restore_last_active()
    assert vo is not None and vo.id == a.id


def test_restore_is_read_only(fresh_home):
    """恢复不得写入：config 逐字节不变、audit_log 不新增。"""
    a = proj.create_project("项目甲", "目标甲")
    _stamp(a.id, "2026-10-05T12:00:00.000Z")

    before_cfg = json.dumps(proj.get_raw_config(a.id), sort_keys=True, ensure_ascii=False)
    before_audit = db.query(a.id, "SELECT count(*) c FROM audit_log")[0]["c"]

    proj.restore_last_active()

    after_cfg = json.dumps(proj.get_raw_config(a.id), sort_keys=True, ensure_ascii=False)
    after_audit = db.query(a.id, "SELECT count(*) c FROM audit_log")[0]["c"]
    assert after_cfg == before_cfg, "恢复只读，不得改动 config"
    assert after_audit == before_audit, "恢复不得追加审计"


# ---------------------------------------------------------------------------
# 2. 可关
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("val", ["1", "true", "YES", "on"])
def test_restore_disabled_by_env(monkeypatch, val):
    from hpsl.app import _restore_disabled

    monkeypatch.setenv("HPSL_NO_RESTORE_ACTIVE", val)
    assert _restore_disabled() is True


def test_restore_enabled_when_env_absent(monkeypatch):
    from hpsl.app import _restore_disabled

    monkeypatch.delenv("HPSL_NO_RESTORE_ACTIVE", raising=False)
    assert _restore_disabled() is False


def test_restore_env_off_keeps_active_empty(fresh_home, monkeypatch):
    """env 打开时 serve 侧不调用 restore → 仍为「未选择项目」。"""
    a = proj.create_project("项目甲", "目标甲")
    _stamp(a.id, "2026-10-05T12:00:00.000Z")
    monkeypatch.setattr(proj, "_active_pid", None)
    monkeypatch.setattr(proj, "_active_label", None)

    monkeypatch.setenv("HPSL_NO_RESTORE_ACTIVE", "1")
    from hpsl.app import _restore_active_project

    _restore_active_project()
    assert proj.active_project() is None


# ---------------------------------------------------------------------------
# 3. 可见：顶栏「当前项目」
# ---------------------------------------------------------------------------
def test_active_ctx_banner_visible(client, csrf_headers):
    r = client.post("/api/v1/projects", headers=csrf_headers, json={"name": "可见性项目", "goal": "看一眼顶栏"})
    pid = r.json()["data"]["id"]
    client.post(f"/api/v1/projects/{pid}/switch", headers=csrf_headers, json={})

    page = client.get("/")
    assert page.status_code == 200
    assert 'class="active-ctx"' in page.text
    assert "可见性项目" in page.text
    # fresh_home 未生效（本用例走 conftest 的共享 home），因此只断言横幅存在与项目名


def test_active_ctx_absent_without_active(client, monkeypatch):
    """未选择项目时不该出现顶栏提示（避免给用户一个空壳横幅）。"""
    monkeypatch.setattr(proj, "_active_pid", None)
    monkeypatch.setattr(proj, "_active_label", None)
    page = client.get("/")
    assert page.status_code == 200
    assert 'class="active-ctx"' not in page.text


# ---------------------------------------------------------------------------
# 4. 错误文案分层
# ---------------------------------------------------------------------------
def _req(headers: dict) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def _handler():
    from hpsl.app import app

    return app.exception_handlers[BizError]


def test_htmx_error_puts_upstream_text_into_details():
    exc = BizError(
        "A040001",
        msg="deepseek 返回 401：密钥无效或已失效，请到「设置 → 密钥」重新填写",
        detail="Authentication Fails, Your api key: ****abcd is invalid",
    )
    resp = _handler()(_req({"hx-request": "true"}), exc)
    body = resp.body.decode("utf-8")
    assert resp.status_code == 200
    assert "密钥无效或已失效" in body
    assert "<details>" in body and "<pre>" in body
    # 上游原文必须在折叠区内，仍然留存但不干扰主提示
    assert "Authentication Fails" in body


def test_json_error_contract_unchanged():
    """非 HTMX 路径仍是 Result 的四个字段，不把 detail 泄漏进契约。"""
    exc = BizError("A040001", msg="中文结论", detail="upstream english text")
    resp = _handler()(_req({"accept": "application/json"}), exc)
    payload = json.loads(resp.body.decode("utf-8"))
    assert set(payload) == {"code", "msg", "data", "traceId"}
    assert "upstream english" not in resp.body.decode("utf-8")


@pytest.mark.parametrize(
    "status,expect",
    [(401, "密钥无效"), (403, "权限"), (404, "模型或接口地址不存在"), (400, "请求被拒绝")],
)
def test_http_hint_is_chinese(status, expect):
    hint = _http_hint(status)
    assert expect in hint
    assert hint.isascii() is False, "提示必须是中文，不能回落下游英文"
