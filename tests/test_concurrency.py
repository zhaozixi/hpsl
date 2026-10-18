"""D1 闭环并发验证（规格 AC1）。

多线程对同一 pid 持续 write：在每 pid 写锁 + 单写连接（check_same_thread=False）下，
事务体被串行化，不应出现 SQLITE_BUSY / cursor 交错；审计条目数须等于写入数。
"""

import threading

from hpsl import db, projects


def test_concurrent_same_pid_writes_under_lock():
    vo = projects.create_project("并发写_D1闭环", "多线程同 pid 写入验证")
    pid = vo.id
    N = 20
    errors = []

    def worker(i: int) -> None:
        try:
            db.write(pid, "concept", {"id": f"c{i}", "name": f"概念{i}"}, "user")
        except Exception as exc:  # noqa: BLE001 - 收集异常用于断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写出现异常：{errors[:3]}"

    # 审计条目数 == 写入数（每个 concept 一次 INSERT + 一次审计）
    audit_n = db.query(
        pid, "SELECT count(*) c FROM audit_log WHERE table_name='concept'"
    )[0]["c"]
    assert audit_n == N

    # 数据完整：N 条 concept 均落库
    concept_n = db.query(pid, "SELECT count(*) c FROM concept")[0]["c"]
    assert concept_n == N


def test_concurrent_mixed_update_same_row_no_busy():
    """对同一行的并发 UPDATE（同 id 反复写）也不应触发 SQLITE_BUSY。"""
    vo = projects.create_project("并发改同键", "UPDATE 竞争")
    pid = vo.id
    db.write(pid, "concept", {"id": "k", "name": "v0"}, "user")
    errors = []

    def worker(i: int) -> None:
        try:
            db.write(pid, "concept", {"id": "k", "name": f"v{i}"}, "user")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 UPDATE 异常：{errors[:3]}"
    # 单次 INSERT + 15 次 UPDATE = 16 条审计（table_name='concept'）
    audit_n = db.query(
        pid, "SELECT count(*) c FROM audit_log WHERE table_name='concept'"
    )[0]["c"]
    assert audit_n == 16
    # 行仍唯一，最终值为最后一次写入之一
    assert db.query(pid, "SELECT count(*) c FROM concept WHERE id='k'")[0]["c"] == 1
