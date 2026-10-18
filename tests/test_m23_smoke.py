"""M23 易用性改造冒烟测试：验证模板渲染出中文、帮助页可达、术语被翻译。"""

import pytest
from jinja2 import Environment, FileSystemLoader


def _create_and_switch(client, csrf_headers, name="M23项目", goal="目标g"):
    r = client.post("/api/v1/projects", headers=csrf_headers, json={"name": name, "goal": goal})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d


def test_macros_emit_chinese():
    env = Environment(loader=FileSystemLoader("hpsl/templates"))
    t = env.from_string(
        "{% from 'macros.html' import state_badge, gap_badge, cost_badge, relation_label, help_banner, tip, field %}"
        "{{ state_badge('INIT') }}|{{ state_badge('CLOSED') }}|{{ gap_badge(2) }}|"
        "{{ cost_badge('ok') }}|{{ cost_badge('blocked') }}|{{ relation_label('prereq') }}|"
        "{{ relation_label('mysteryxyz') }}"
    )
    out = t.render()
    assert "未开始" in out and "已完成" in out
    assert "一般" in out and "级别" in out
    assert "正常" in out and "已超限" in out
    assert "前置知识" in out
    assert "mysteryxyz" in out  # 未知关系原样保留


def test_unit_detail_translates_status_and_gap():
    env = Environment(loader=FileSystemLoader("hpsl/templates"))
    t = env.get_template("unit_detail.html")
    html = t.render(
        error=None,
        project={"name": "P"},
        unit={
            "title": "T", "status": "INIT", "difficulty_band": "中",
            "degrade_count": 0, "production": "", "produced_at": None,
            "skipped_produce": False, "forced_close": False, "closed_at": None,
        },
        gaps=[{"id": "g1", "level": 2, "status": "待补", "text": "x"}],
        claim_checks=[],
    )
    assert "未开始" in html, "单元状态未翻译"
    assert "一般" in html, "缺口级别未翻译"
    assert "单元元信息" in html


def test_settings_labels_and_tips():
    env = Environment(loader=FileSystemLoader("hpsl/templates"))
    t = env.get_template("settings.html")
    html = t.render(error=None, project={"name": "P"}, config={})
    assert "调整评判标准" in html
    assert "及格线" in html, "通过阈值未加通俗解释"
    assert "通过阈值" in html


def test_pages_render_chinese_and_help_reachable(client, csrf_headers):
    _create_and_switch(client, csrf_headers)
    checks = {
        "/": "这里是你的所有学习项目",
        "/unit": "学习单元 = 你要攻克",
        "/budget": "花了多少钱一目了然",
        "/cards": "要点卡片 = 你学完时记下的笔记",
        "/graph": "概念图：知识点之间的关系",
        "/ledger": "账本总览",
        "/stats": "用图表看学习进展",
        "/review": "到期了，回来巩固一下",
        "/backup": "给数据做个备份",
        "/export": "导出学习记录",
        "/wizard": "跟着四步走",
        "/settings": "调整评判标准",
        "/projects/new": "新建一个学习项目",
    }
    for path, phrase in checks.items():
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert phrase in r.text, (path, phrase)

    # 帮助页可达
    r = client.get("/static/help.html")
    assert r.status_code == 200, r.status_code
    assert "操作手册" in r.text
