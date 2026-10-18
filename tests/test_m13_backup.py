import datetime as _dt

from hpsl import projects, db
from hpsl import app as hpsl_app


def _seed_backups(pid, date_strings):
    base = db._config.project_dir(pid) / "backups"
    base.mkdir(parents=True, exist_ok=True)
    for ds in date_strings:
        (base / f"project-{ds}-aaaa1111.db").write_bytes(b"x")
    return base


def test_rotate_all_backups_retention():
    p1 = projects.create_project("M13-P1", "g1")
    p2 = projects.create_project("M13-P2", "g2")
    today = _dt.date(2026, 10, 17)
    daily = [(today - _dt.timedelta(days=i)).isoformat() for i in range(10)]  # 10 日档
    base = _seed_backups(p1.id, daily)
    # 跨月历史档（应保留为月锚点）
    (base / "project-2026-01-05-bbbb2222.db").write_bytes(b"x")
    # 非本系统文件不应被删
    (base / "notes.txt").write_bytes(b"keep")

    deleted = db.rotate_all_backups(keep_daily=7, keep_monthly=12)
    dl = deleted.get(p1.id, [])
    # 10 日档保留最近 7 份（10-17..10-11）；10-08 因作为 10 月月锚点被保留 → 仅删 10-10/10-09
    # 月锚点 2026-01 在 12 月窗口内保留
    assert len(dl) == 2, dl
    remaining = [p for p in base.glob("project-*.db")]
    assert len(remaining) == 9, len(remaining)  # 7 日档 + 10-08 月锚点 + 01-05 月锚点
    assert (base / "notes.txt").exists()
    # p2 无快照 → 不出现
    assert p2.id not in deleted


def test_cli_rotate_runs(capsys):
    p1 = projects.create_project("M13-P3", "g1")
    today = _dt.date(2026, 10, 17)
    _seed_backups(p1.id, [(today - _dt.timedelta(days=i)).isoformat() for i in range(10)])
    hpsl_app.main(["rotate", "--pid", str(p1.id)])
    out = capsys.readouterr().out
    assert "[rotate]" in out


def test_cli_backup_runs(capsys):
    p1 = projects.create_project("M13-P4", "g1")
    hpsl_app.main(["backup", "--pid", str(p1.id)])
    out = capsys.readouterr().out
    assert "[backup]" in out
    base = db._config.project_dir(p1.id) / "backups"
    assert list(base.glob("project-*.db"))
