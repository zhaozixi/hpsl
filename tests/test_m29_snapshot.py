"""M29 验收：每日自动快照 + 恢复 CLI（P1-04，零 DDL）。

覆盖：
- daily_snapshot_all：幂等（同日只多 1 份）、文件名以 project-<今天 UTC> 开头；
- restore_project：回滚到快照内容、objects==16、拒绝不存在文件 / backups 之外路径 /
  损坏快照（B999001）；
- CLI：app.main(["snapshot"]) 打印 pid；
- 零 DDL：快照/恢复后 count_user_objects == 16。

纪律：只做文件级操作，不新增表/列；不修改 delivery/ 与 paper/；不改既有测试断言。
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hpsl import app as hpsl_app
from hpsl import config as cfg
from hpsl import db, projects
from hpsl.common.errors import BizError


def _project(name: str) -> str:
    return projects.create_project(name, "M29 目标").id


def _backups(pid: str) -> Path:
    return cfg.project_dir(pid) / "backups"


def _snapshots(pid: str) -> list[Path]:
    return sorted(_backups(pid).glob("project-*.db"))


# ---------------------------------------------------------------------------
# 每日快照
# ---------------------------------------------------------------------------
def test_daily_snapshot_all_idempotent():
    pid = _project("M29-每日快照幂等")
    assert _snapshots(pid) == []

    first = db.daily_snapshot_all()
    assert pid in first
    assert Path(first[pid]).exists()
    assert len(_snapshots(pid)) == 1

    second = db.daily_snapshot_all()
    assert pid not in second  # 当天已有 → 跳过
    assert len(_snapshots(pid)) == 1  # 两次调用只多 1 个文件


def test_daily_snapshot_filename_starts_with_utc_today():
    pid = _project("M29-每日快照命名")
    created = db.daily_snapshot_all()
    assert pid in created
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert Path(created[pid]).name.startswith(f"project-{today}")
    assert _snapshots(pid)[0].name == Path(created[pid]).name


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------
def test_restore_project_rolls_back_to_snapshot():
    pid = _project("M29-恢复回滚")
    db.write(pid, "concept", {"id": "k1", "name": "v1"}, "user")
    backup = db.backup_project(pid)

    db.write(pid, "concept", {"id": "k1", "name": "v2"}, "user")
    assert db.query(pid, "SELECT name FROM concept WHERE id='k1'")[0]["name"] == "v2"

    info = db.restore_project(pid, backup)
    assert info["pid"] == pid
    assert Path(info["restored_from"]).resolve() == Path(backup).resolve()
    pre = Path(info["pre_restore"])
    assert pre.exists() and pre.parent == _backups(pid) and pre.name.startswith("pre-restore-")
    assert info["objects"] == 16
    # 回滚到快照时刻的 v1
    assert db.query(pid, "SELECT name FROM concept WHERE id='k1'")[0]["name"] == "v1"


def test_restore_project_rejects_missing_file():
    pid = _project("M29-恢复缺文件")
    with pytest.raises(BizError) as ei:
        db.restore_project(pid, "project-2000-01-01T00-00-00-000Z-deadbeef.db")
    assert ei.value.code == "C400001"

    # 绝对路径但不存在，同样拒绝
    with pytest.raises(BizError) as ei2:
        db.restore_project(pid, str(cfg.project_dir(pid) / "nope.db"))
    assert ei2.value.code == "C400001"


def test_restore_project_rejects_path_outside_backups():
    pid = _project("M29-恢复越界")
    outside = db.safe_path(pid, "outside.db")
    sqlite3.connect(outside).close()  # 即便是一个合法 sqlite 文件，也不得从 backups/ 之外恢复

    with pytest.raises(BizError) as ei:
        db.restore_project(pid, str(outside))
    assert ei.value.code == "C400001"

    # 相对路径 ../outside.db（解析后落到项目根，不在 backups/ 内）
    with pytest.raises(BizError) as ei2:
        db.restore_project(pid, "../outside.db")
    assert ei2.value.code == "C400001"


def test_restore_project_rejects_corrupt_snapshot():
    pid = _project("M29-恢复损坏快照")
    corrupt = _backups(pid) / "project-2020-01-01T00-00-00-000Z-badbad00.db"
    corrupt.write_bytes(b"this is definitely not a sqlite database")
    with pytest.raises(BizError) as ei:
        db.restore_project(pid, corrupt.name)
    assert ei.value.code == "B999001"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_snapshot_prints_pid(capsys):
    pid = _project("M29-CLI快照")
    hpsl_app.main(["snapshot"])
    out = capsys.readouterr().out
    assert "[snapshot]" in out
    assert pid in out
    assert _snapshots(pid)


# ---------------------------------------------------------------------------
# 零 DDL
# ---------------------------------------------------------------------------
def test_zero_ddl_after_snapshot_and_restore():
    pid = _project("M29-零DDL")
    db.write(pid, "concept", {"id": "k1", "name": "v1"}, "user")
    created = db.daily_snapshot_all()
    assert pid in created
    db.write(pid, "concept", {"id": "k1", "name": "v2"}, "user")
    db.restore_project(pid, created[pid])
    assert db.count_user_objects(pid) == 16
