"""M14 验收：暗色模式（token 扩展 + 主题切换，零 DDL）。

- AC1：页面注入主题切换按钮（theme-toggle-btn）与切换函数（hpslToggleTheme）。
- AC2：app.css 定义 html.dark 主题变量覆盖（深色背景/表面/浅色文本）。
- AC3：渲染前应用已存偏好（防闪烁脚本，localStorage 'hpsl-theme'）。
- AC4：零 DDL（count_user_objects == 16）。

纪律：纯前端；偏好存 localStorage，不新增库对象/配置表/端点。
"""

import pytest

from hpsl import db, projects  # noqa: F401


def test_theme_toggle_present(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="theme-toggle-btn"' in r.text
    # C9：切换函数外置到 /static/theme.js（严格 CSP 不再允许内联脚本）
    assert 'data-action="toggle-theme"' in r.text
    assert '/static/theme.js' in r.text
    assert "hpslToggleTheme" not in r.text
    # 防闪烁：head 内同步外链脚本在渲染前读取 localStorage 偏好
    assert r.text.index('/static/theme.js') < r.text.index('<body>')
    js = client.get("/static/theme.js")
    assert js.status_code == 200
    assert "hpslToggleTheme" in js.text
    assert "hpslSyncThemeLabel" in js.text
    assert "hpsl-theme" in js.text


def test_dark_tokens_defined(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert "html.dark" in r.text
    # 深色背景 token 覆盖
    assert "#0b0f17" in r.text
    assert "var(--text)" in r.text or "--text:#e5e7eb" in r.text


def test_zero_ddl_after_theme(client, csrf_headers):
    body = {"name": "M14项目", "goal": "目标g"}
    r = client.post("/api/v1/projects", headers=csrf_headers, json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    client.get("/")
    assert db.count_user_objects(d["id"]) == 16
