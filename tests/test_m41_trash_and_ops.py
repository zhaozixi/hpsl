"""M41：逐字删除确认、密钥状态异常日志、trash 列表/清理 CLI。"""

import os
import time
from pathlib import Path

import pytest

from hpsl import app as app_mod, config as _config, projects as proj
from hpsl.common.errors import BizError
from hpsl.models import registry


# ---------------------------------------------------------------------------
# F-05：删除确认严格逐字（不 strip）
# ---------------------------------------------------------------------------
def _mk(client, csrf, name="逐字项目"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "g"})
    return r.json()["data"]["id"]


def test_delete_requires_exact_name(client, csrf_headers):
    pid = _mk(client, csrf_headers, "甲项目")
    r = client.post(
        f"/api/v1/projects/{pid}/delete",
        headers=csrf_headers,
        json={"confirm_name": "甲项目 "},  # 尾随空格
    )
    assert r.json()["code"] == "C400001", r.text
    assert proj.get_project(pid).name == "甲项目"  # 未删除

    ok = client.post(
        f"/api/v1/projects/{pid}/delete",
        headers=csrf_headers,
        json={"confirm_name": "甲项目"},
    )
    assert ok.json()["code"] == "0", ok.text
    assert not _config.project_dir(pid).exists()


# ---------------------------------------------------------------------------
# F-08：key_status 异常不再静默
# ---------------------------------------------------------------------------
def test_provider_has_key_logs_on_failure(monkeypatch, caplog):
    from hpsl.secrets import keystore

    def boom(*a, **k):
        raise RuntimeError("vault corrupted")

    monkeypatch.setattr(keystore, "key_status", boom)
    with caplog.at_level("WARNING", logger="hpsl"):
        assert registry.provider_has_key("deepseek") is False
    assert "读取厂商 deepseek 的密钥状态失败" in caplog.text


# ---------------------------------------------------------------------------
# F-07：trash 列表 / 预览 / 清理
# ---------------------------------------------------------------------------
def _trash_dir(name: str, days_old: int) -> Path:
    root = _config.trash_root()
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.mkdir()
    ts = time.time() - days_old * 86400
    os.utime(p, (ts, ts))
    return p


def test_trash_list_and_purge():
    old = _trash_dir("old-proj", 40)
    new = _trash_dir("new-proj", 1)
    names = [x["name"] for x in proj.list_trash()]
    assert names[0] == "new-proj" and "old-proj" in names

    preview = proj.purge_trash(30, dry_run=True)
    assert preview["candidates"] == ["old-proj"]
    assert old.exists() and new.exists()

    res = proj.purge_trash(30, dry_run=False)
    assert res["deleted"] == 1
    assert not old.exists() and new.exists()


def test_trash_purge_rejects_negative_days():
    with pytest.raises(BizError) as exc:
        proj.purge_trash(-1)
    assert exc.value.code == "C400001"


def test_trash_cli_list_and_confirm(capsys):
    _trash_dir("cli-old", 40)
    _trash_dir("cli-new", 1)

    app_mod.main(["trash", "list"])
    out = capsys.readouterr().out
    assert "cli-old" in out and "cli-new" in out

    # 不带 --yes：只预览，不删除
    app_mod.main(["trash", "purge", "--days", "30"])
    out = capsys.readouterr().out
    assert "cli-old" in out and "--yes" in out
    assert (_config.trash_root() / "cli-old").exists()

    app_mod.main(["trash", "purge", "--days", "30", "--yes"])
    out = capsys.readouterr().out
    assert "deleted 1" in out
    assert not (_config.trash_root() / "cli-old").exists()
