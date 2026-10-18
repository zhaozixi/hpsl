"""隔离与写入纪律守护（对应 AC4 / AC5 / AC7 / AC9）。"""

from pathlib import Path

import pytest

from hpsl import db, projects
from hpsl.common.errors import BizError


def test_audit_appended_on_write():
    vo = projects.create_project("审计同事务", "写入成功则审计必在")
    db.write(vo.id, "concept", {"id": "k1", "name": "x"}, "user")
    rows = db.query(
        vo.id, "SELECT * FROM audit_log WHERE table_name=? AND row_id=?", ("concept", "k1")
    )
    assert len(rows) == 1
    assert rows[0]["actor"] == "user"


def test_cannot_write_audit_log_directly():
    """audit_log 不在写白名单，业务代码直写应被拒绝（KeyError）。"""
    vo = projects.create_project("禁止直写审计", "白名单拒绝")
    with pytest.raises(KeyError):
        db.write(
            vo.id,
            "audit_log",
            {"id": 1, "ts": "t", "actor": "user", "table_name": "t", "row_id": "r", "change": "{}"},
            "user",
        )


def test_migrate_idempotent():
    vo = projects.create_project("迁移幂等", "重复 migrate 不报错/不重复建表")
    w = db.get_write_conn(vo.id)
    db.migrate(w)  # 再次迁移
    assert db.count_user_objects(vo.id) == 16


def test_no_replace_semantics_update_not_insert():
    """写路径禁用 REPLACE：相同主键两次写入为 UPDATE（单行 + 两次审计），非删除式 INSERT。"""
    vo = projects.create_project("禁用REPLACE", "UPDATE 而非覆盖")
    db.write(vo.id, "concept", {"id": "k", "name": "v1"}, "user")
    db.write(vo.id, "concept", {"id": "k", "name": "v2"}, "user")
    cnt = db.query(vo.id, "SELECT count(*) c FROM concept")[0]["c"]
    assert cnt == 1  # 单行（非 REPLACE 触发的删除+插入）
    name = db.query(vo.id, "SELECT name FROM concept WHERE id=?", ("k",))[0]["name"]
    assert name == "v2"
    alc = db.query(
        vo.id, "SELECT count(*) c FROM audit_log WHERE table_name='concept' AND row_id='k'"
    )[0]["c"]
    assert alc == 2  # 一次 INSERT 审计 + 一次 UPDATE 审计


def test_no_replace_sql_token_in_source():
    """源码不得出现 REPLACE 写语句（仅允许 UPDATE/INSERT），防注入与 FK 删除语义。"""
    src = Path(__file__).resolve().parent.parent.joinpath("hpsl", "db.py").read_text(encoding="utf-8")
    upper = src.upper()
    assert "REPLACE " not in upper
    assert "REPLACE(" not in upper


def test_write_whitelist_rejects_unknown_column():
    vo = projects.create_project("白名单列", "未知列被拒")
    with pytest.raises(ValueError):
        db.write(vo.id, "concept", {"id": "k", "name": "x", "evil_col": 1}, "user")
