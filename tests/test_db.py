"""连接属性 / 迁移产物 / 时间字段 守护（对应 AC2 / AC6 / AC10）。"""

import os
import re

from hpsl import db, projects, config
from hpsl.common.errors import BizError


def test_connection_properties():
    vo = projects.create_project("连接属性", "验证 WAL/busy_timeout/foreign_keys")
    w = db.get_write_conn(vo.id)
    assert w.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert w.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert w.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_user_version_and_sixteen_objects():
    vo = projects.create_project("库对象", "16 个库对象计数 + user_version")
    w = db.get_write_conn(vo.id)
    assert w.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.count_user_objects(vo.id) == 16  # 15 业务表 + chunk_fts 虚表（FTS5 影子表已排除）
    # chunk_fts 确实存在且为虚表
    fts = w.execute(
        "SELECT name, sql FROM sqlite_master WHERE name='chunk_fts'"
    ).fetchone()
    assert fts is not None
    assert "CREATE VIRTUAL TABLE" in (fts["sql"] or "")


def test_iso8601_utc_time():
    vo = projects.create_project("时间格式", "created_at 须为 ISO-8601 UTC")
    created = db.query(vo.id, "SELECT created_at FROM project WHERE id=?", (vo.id,))[0]["created_at"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", created), created
    assert created.endswith("Z")
    # 字典序即时间序
    assert created > "2020-01-01T00:00:00.000Z"
