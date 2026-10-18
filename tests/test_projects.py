"""项目管理 / 物理隔离 / 全局唯一 / 目录权限 守护（对应 AC1 / AC3 / AC8）。"""

import os

import pytest

from hpsl import db, projects, config
from hpsl.common.errors import BizError


def test_two_projects_physically_isolated():
    a = projects.create_project("项目A", "学习目标A")
    b = projects.create_project("项目B", "学习目标B")
    assert a.id != b.id

    # 在 A 中写入一条概念
    db.write(a.id, "concept", {"id": "c1", "name": "概念A"}, "user")

    # B 看不到 A 的数据（每项目独立库，物理隔离）
    rows_b = db.query(b.id, "SELECT * FROM concept WHERE id=?", ("c1",))
    assert rows_b == []
    rows_a = db.query(a.id, "SELECT * FROM concept WHERE id=?", ("c1",))
    assert len(rows_a) == 1

    # 项目 B 的资源 ID 在项目 A 上下文使用被拒绝（物理上不存在）
    cross = db.query(a.id, "SELECT * FROM concept WHERE id=?", ("from-b-not-exist",))
    assert cross == []


def test_duplicate_name_returns_a010002():
    projects.create_project("重名项目", "首个")
    with pytest.raises(BizError) as exc:
        projects.create_project("重名项目", "第二个")
    assert exc.value.code == "A010002"


def test_list_and_active_handle():
    a = projects.create_project("列表A", "g")
    b = projects.create_project("列表B", "g")
    names = {p.name for p in projects.list_projects()}
    assert names == {"列表A", "列表B"}
    # active 句柄为最近创建者
    assert projects.active_project().id == b.id
    projects.set_active(a.id)
    assert projects.active_project().id == a.id


def test_project_dir_permission_0700():
    vo = projects.create_project("权限测试", "目标目录权限 0700")
    mode = os.stat(str(config.project_dir(vo.id))).st_mode & 0o777
    if os.name == "nt":
        # Windows 不表达 POSIX 0700：Python 对非只读目录模拟 0o777，
        # 创建时仍显式请求 0o700（跨平台正确）；严格等价断言在 POSIX 才成立。
        assert mode in (0o700, 0o777)
    else:
        assert mode == 0o700
    # 子目录（backups/exports/logs）已建占位
    for sub in ("backups", "exports", "logs"):
        assert (config.project_dir(vo.id) / sub).is_dir()


def test_get_effective_config_defaults():
    vo = projects.create_project("配置默认", "阈值默认值")
    cfg = projects.get_effective_config(vo.id)
    assert cfg.pass_threshold == 0.7
    assert cfg.budget_cap_usd == 10.0
    assert cfg.review_intervals == (2, 7, 21)


def test_set_config_audited_and_merged():
    vo = projects.create_project("配置覆盖", "项目级阈值覆盖")
    projects.set_config(vo.id, {"pass_threshold": 0.9, "budget_cap_usd": 20.0}, "user")
    cfg = projects.get_effective_config(vo.id)
    assert cfg.pass_threshold == 0.9
    assert cfg.budget_cap_usd == 20.0
    # 覆盖写入经 db.write 审计留痕
    n = db.query(vo.id, "SELECT count(*) c FROM audit_log WHERE table_name='project'")[0]["c"]
    assert n >= 2  # create_project 一次 + set_config 一次
