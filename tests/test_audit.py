"""审计与撤销守护（对应 AC4 / 撤销留痕 / A070002 / A070001）。

注意：create_project 自身也会写一条 project 行审计，故概念写入的审计 id 不是 1/2，
测试一律按 table_name+row_id 动态查询审计 id，避免硬编码偏移。
"""

import json

import pytest

from hpsl import db, projects
from hpsl.common.errors import BizError


def _concept_audit_ids(pid: str, rid: str = "x") -> list[int]:
    rows = db.query(
        pid, "SELECT id FROM audit_log WHERE table_name='concept' AND row_id=? ORDER BY id", (rid,)
    )
    return [r["id"] for r in rows]


def test_every_write_has_audit_entry():
    vo = projects.create_project("每笔留痕", "g")
    db.write(vo.id, "concept", {"id": "x", "name": "n"}, "user")
    alc = db.query(vo.id, "SELECT count(*) c FROM audit_log WHERE table_name='concept'")[0]["c"]
    assert alc == 1
    a = db.query(vo.id, "SELECT * FROM audit_log WHERE table_name='concept' AND row_id='x'")[0]
    assert a["actor"] == "user" and a["table_name"] == "concept" and a["row_id"] == "x"
    assert json.loads(a["change"])["name"] == "n"
    assert json.loads(a["prev"]) is None  # INSERT 无旧值（JSON 列存为 "null"）


def test_undo_leaves_its_own_trace():
    vo = projects.create_project("撤销留痕", "g")
    db.write(vo.id, "concept", {"id": "x", "name": "n1"}, "user")  # 概念审计 #1
    db.write(vo.id, "concept", {"id": "x", "name": "n2"}, "user")  # 概念审计 #2（最后写入）
    last = _concept_audit_ids(vo.id)[-1]
    before = db.query(vo.id, "SELECT count(*) c FROM audit_log")[0]["c"]
    db.undo(vo.id, last, "user")  # 撤销 n2 写入 → 回到 n1
    after = db.query(vo.id, "SELECT count(*) c FROM audit_log")[0]["c"]
    assert after == before + 1  # 撤销动作本身留痕
    cur = db.query(vo.id, "SELECT name FROM concept WHERE id=?", ("x",))[0]["name"]
    assert cur == "n1"
    last_entry = db.query(vo.id, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 1")[0]
    assert json.loads(last_entry["prev"])["name"] == "n2"  # 撤销条目的 prev = 撤销前的当前值


def test_undo_fingerprint_mismatch_rejected_a070002():
    """行已被后续变更，撤销旧条目应被拒（A070002），且当前值不变。"""
    vo = projects.create_project("指纹不匹配", "g")
    db.write(vo.id, "concept", {"id": "x", "name": "n1"}, "user")  # 概念 #1: change=n1
    db.write(vo.id, "concept", {"id": "x", "name": "n2"}, "user")  # 概念 #2: change=n2（后续变更）
    first = _concept_audit_ids(vo.id)[0]
    with pytest.raises(BizError) as exc:
        db.undo(vo.id, first, "user")  # 试图撤销已被 n2 覆盖的旧写入
    assert exc.value.code == "A070002"
    assert db.query(vo.id, "SELECT name FROM concept WHERE id=?", ("x",))[0]["name"] == "n2"


def test_undo_expected_prev_hash_rejects():
    vo = projects.create_project("期望指纹", "g")
    db.write(vo.id, "concept", {"id": "x", "name": "n1"}, "user")
    first = _concept_audit_ids(vo.id)[0]
    with pytest.raises(BizError) as exc:
        db.undo(vo.id, first, "user", expected_prev_hash="deadbeef")
    assert exc.value.code == "A070002"


def test_undo_insert_deletes_row():
    """撤销一次 INSERT（prev=None）→ 删除目标行。"""
    vo = projects.create_project("撤销插入", "g")
    db.write(vo.id, "concept", {"id": "x", "name": "n1"}, "user")
    first = _concept_audit_ids(vo.id)[0]
    cur = db.query(vo.id, "SELECT * FROM concept WHERE id=?", ("x",))[0]
    fp = db._row_fingerprint({k: cur[k] for k in cur})
    db.undo(vo.id, first, "user", expected_prev_hash=fp)
    assert db.query(vo.id, "SELECT * FROM concept WHERE id=?", ("x",)) == []


def test_undo_missing_entry_a070001():
    vo = projects.create_project("撤销不存在", "g")
    with pytest.raises(BizError) as exc:
        db.undo(vo.id, 99999, "user")
    assert exc.value.code == "A070001"
