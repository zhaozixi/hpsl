"""M31：工程债补强——M12 设计系统 / M22 厂商矩阵 / WAL 与检索边界 / 前端语法。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hpsl import db, projects
from hpsl.engine import retrieval
from hpsl.models import registry
from hpsl.models.anthropic import AnthropicAdapter
from hpsl.models.gemini import GeminiAdapter
from hpsl.models.openai_compat import OpenAICompatAdapter
from hpsl.models.base import Msg


# ---------------------------------------------------------------------------
# M12：设计系统（token + 暗色 + 模板继承）
# ---------------------------------------------------------------------------
def test_m12_css_tokens_present():
    css = Path("hpsl/static/app.css").read_text(encoding="utf-8")
    for token in ("--brand", "--bg", "--surface", "--border", "--text",
                  "--sp-1", "--sp-8", "--r-md", "--shadow-sm", "--maxw"):
        assert token in css, token
    assert ":root{" in css
    assert "html.dark" in css
    assert "--bg:#0b0f17" in css  # 暗色覆写


def test_m12_all_page_templates_extend_base():
    tpl_dir = Path("hpsl/templates")
    pages = [
        p for p in tpl_dir.glob("*.html")
        if not p.name.startswith("_") and p.name not in ("base.html", "macros.html")
    ]
    assert len(pages) >= 17
    for p in pages:
        text = p.read_text(encoding="utf-8")
        assert '{% extends "base.html" %}' in text, p.name


# ---------------------------------------------------------------------------
# M22：8 厂商 / 3 类协议 / 适配器工厂 / 异构判定
# ---------------------------------------------------------------------------
def test_m22_provider_matrix():
    providers = registry.list_providers()
    assert len(providers) >= 8
    for p in providers:
        assert registry.provider_kind(p) in ("openai_compat", "anthropic", "gemini")
        assert registry.default_base_url(p).startswith("https://")
        assert registry.provider_display(p)


def test_m22_models_reference_known_providers():
    providers = set(registry.list_providers())
    ids = [m.id for m in registry.list_models()]
    assert len(ids) == len(set(ids)), "模型 id 不得重复"
    for m in registry.list_models():
        assert m.provider in providers, m.id


@pytest.mark.parametrize(
    "model_id,cls",
    [
        ("deepseek-flash", OpenAICompatAdapter),
        ("deepseek-v4-pro", OpenAICompatAdapter),
        ("qwen-plus", OpenAICompatAdapter),
        ("claude-3-5-haiku-20241022", AnthropicAdapter),
        ("gemini-1.5-flash", GeminiAdapter),
    ],
)
def test_m22_build_adapter_routes_by_kind(model_id, cls):
    adapter = registry.build_adapter(model_id, key_resolver=lambda: "sk-x")
    assert isinstance(adapter, cls)


def test_m22_heterogeneous_rules():
    ds = registry.get_model("deepseek-flash")
    ds_chat = registry.get_model("deepseek-chat")
    gpt = registry.get_model("gpt-4o")
    qwen = registry.get_model("qwen-plus")
    assert registry.heterogeneous(ds, qwen) is True       # 不同族
    assert registry.heterogeneous(ds, ds_chat) is False   # 同族
    # deepseek-chat 声明 derived_from=gpt4o → 与 gpt-4o 同源
    assert registry.heterogeneous(ds_chat, gpt) is False


def test_m22_gemini_adapter_is_placeholder():
    from hpsl.common.errors import BizError

    with pytest.raises(BizError) as exc:
        GeminiAdapter().complete([Msg("user", "x")])
    assert exc.value.code == "A040002"


# ---------------------------------------------------------------------------
# 工程债：WAL checkpoint / close_all / 检索边界
# ---------------------------------------------------------------------------
def test_wal_checkpoint_and_reopen():
    vo = projects.create_project("WAL项目", "g")
    db.write(vo.id, "concept", {"id": "c1", "name": "x"}, "user")
    conn = db.get_write_conn(vo.id)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert db.query(vo.id, "SELECT count(*) c FROM concept")[0]["c"] == 1
    db.close_all()
    assert db.query(vo.id, "SELECT name FROM concept WHERE id='c1'")[0]["name"] == "x"


@pytest.mark.parametrize("q", ["", "   ", '"', "*", "AND OR", "a" * 300])
def test_retrieval_search_handles_edge_queries(q):
    vo = projects.create_project("检索边界", "g")
    assert retrieval.search(vo.id, q) == []


# ---------------------------------------------------------------------------
# 工程债：前端语法（node 可用时）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["hpsl/static/app.js", "hpsl/static/htmx.min.js"])
def test_frontend_js_syntax(path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用")
    proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
