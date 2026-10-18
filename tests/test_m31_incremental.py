"""M31：知识库增量索引（mtime/size 变更才重建）。"""

import json
import os
import time
from pathlib import Path

from hpsl import db, projects
from hpsl.engine import retrieval


def _project(name="增量索引"):
    return projects.create_project(name, "g")


def _write(path: Path, text: str, mtime: float | None = None) -> None:
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _hits(pid, term):
    return retrieval.search(pid, term)


def test_incremental_first_then_unchanged(tmp_path):
    pid = _project().id
    root = tmp_path / "kb"
    root.mkdir()
    base = time.time() - 100
    _write(root / "a.md", "# Alpha\n\nALPHATOKEN 内容", base)
    _write(root / "b.md", "# Beta\n\nBETATOKEN 内容", base)

    first = retrieval.index_source(pid, str(root))
    assert first["files"] == 2
    assert first["files_indexed"] == 2
    assert first["chunks"] >= 2
    assert first["incremental"] is True

    again = retrieval.index_source(pid, str(root))
    assert again["files"] == 2
    assert again["files_indexed"] == 0
    assert again["files_unchanged"] == 2
    assert again["chunks"] == first["chunks"]
    assert again["source_id"] == first["source_id"]

    state_file = Path(projects._config.project_dir(pid)) / "source_index.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert first["source_id"] in data["sources"]


def test_changed_file_reindexed_only(tmp_path):
    pid = _project("增量改").id
    root = tmp_path / "kb"
    root.mkdir()
    base = time.time() - 100
    a = root / "a.md"
    _write(a, "# Alpha\n\nOLDCONTENT 旧内容", base)
    _write(root / "b.md", "# Beta\n\nBETATOKEN", base)
    first = retrieval.index_source(pid, str(root))

    _write(a, "# Alpha\n\nNEWCONTENT 新内容", base + 10)
    second = retrieval.index_source(pid, str(root))
    assert second["files_indexed"] == 1
    assert second["files_unchanged"] == 1
    assert second["chunks"] == first["chunks"]  # 片段数不变（同数量）

    assert _hits(pid, "NEWCONTENT"), "新内容应可检索"
    assert not _hits(pid, "OLDCONTENT"), "旧内容应已从索引移除"


def test_added_and_removed_files(tmp_path):
    pid = _project("增删文件").id
    root = tmp_path / "kb"
    root.mkdir()
    base = time.time() - 100
    _write(root / "a.md", "ALPHATOKEN", base)
    b = root / "b.md"
    _write(b, "BETATOKEN", base)
    retrieval.index_source(pid, str(root))

    _write(root / "c.md", "GAMMATOKEN", base + 5)
    added = retrieval.index_source(pid, str(root))
    assert added["files_indexed"] == 1
    assert _hits(pid, "GAMMATOKEN")

    b.unlink()
    removed = retrieval.index_source(pid, str(root))
    assert removed["files_removed"] == 1
    assert not _hits(pid, "BETATOKEN")


def test_force_rebuilds_all(tmp_path):
    pid = _project("强制重建").id
    root = tmp_path / "kb"
    root.mkdir()
    base = time.time() - 100
    _write(root / "a.md", "A", base)
    _write(root / "b.md", "B", base)
    retrieval.index_source(pid, str(root))
    forced = retrieval.index_source(pid, str(root), force=True)
    assert forced["files_indexed"] == 2
    assert forced["incremental"] is False


def test_incremental_zero_ddl(tmp_path):
    pid = _project("增量零DDL").id
    root = tmp_path / "kb"
    root.mkdir()
    _write(root / "a.md", "内容", time.time() - 10)
    retrieval.index_source(pid, str(root))
    retrieval.index_source(pid, str(root))
    assert db.count_user_objects(pid) == 16
