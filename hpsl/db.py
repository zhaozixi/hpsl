"""连接管理、幂等迁移、带审计日志的写入（规格 §5 / §6 / §7 / §8）。

职责（M0 数据底座，唯一写入口）：
- 每项目单一写连接（缓存单例）+ busy_timeout=5000ms + WAL + 每连接 foreign_keys=ON。
- 多读连接工厂（WAL 读不阻塞写）。
- 建库即创建全部 16 个库对象（15 表 + chunk_fts FTS5 虚表），user_version=1，幂等。
- write(table, row, actor)：白名单校验 → BEGIN IMMEDIATE → UPDATE/INSERT（禁用 REPLACE）
  → 同事务追加 audit_log → COMMIT；SQLITE_BUSY 排队重试，耗尽 → B999002。
- undo(audit_id, actor)：校验旧值指纹 → 按 prev 回滚（prev=None 则删行）→ 撤销本身留痕；
  指纹不匹配 → A070002。

纪律（冻结）：不引入 ORM；值一律参数绑定；表名/列名只允许来自 TABLES 白名单（防注入 T-06）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config as _config  # 同包导入（包名 hpsl）
from .common.dto import ConfigVO
from .common.errors import BizError

SQLITE_BUSY = 5
MAX_WRITE_RETRIES = 5

# ---------------------------------------------------------------------------
# 运行时断言（启动前）：SQLite >= 3.45.0 且 FTS5 可用
# ---------------------------------------------------------------------------
_RUNTIME_CHECKED = False


def _assert_runtime() -> None:
    global _RUNTIME_CHECKED
    if _RUNTIME_CHECKED:
        return
    if sqlite3.sqlite_version_info < (3, 45, 0):
        raise RuntimeError(
            f"需要 SQLite >= 3.45.0，当前为 {sqlite3.sqlite_version}（FTS5/严格 WAL 依赖）"
        )
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(a, b)")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"当前 SQLite 构建缺少 FTS5 支持：{exc}") from exc
    finally:
        probe.close()
    _RUNTIME_CHECKED = True


# ---------------------------------------------------------------------------
# TABLES 写白名单（规格 §8.1）：逐字对齐 §7 的 14 张业务表 + audit_log（共 15 张表），
# 其中 audit_log 不在白名单（仅 write() 内部追加）。
# audit_log 不在写白名单，仅由 write() 内部追加。chunk_fts 为检索虚表，不在此列。
# ---------------------------------------------------------------------------
TABLES: dict[str, tuple[str, ...]] = {
    "project": ("id", "goal", "initial_belief", "created_at", "config"),
    "concept": ("id", "name", "verify_status", "last_verified_at"),
    "session": ("id", "unit_id", "started_at", "ended_at", "gaps_confirmed"),
    "turn": ("id", "session_id", "role", "content", "provider", "model", "ts"),
    "source": ("id", "path", "kind", "added_at"),
    "unit": (
        "id", "title", "status", "forced_close", "close_reason", "difficulty_band",
        "degraded_from", "degrade_count", "production", "produced_at",
        "skipped_produce", "chosen_from", "closed_at",
    ),
    "gap": ("id", "unit_id", "session_id", "level", "text", "status", "context", "created_at", "updated_at"),
    "gap_candidate": ("id", "unit_id", "text", "source_turn", "status", "suggested_level"),
    "card": ("id", "concept_id", "my_words", "links", "verify_status"),
    "concept_edge": ("id", "src", "dst", "kind"),
    "attempt": ("id", "unit_id", "concept_id", "kind", "rubric", "self_score", "confirmed_score", "seconds", "ts"),
    "claim_check": ("id", "claim", "providers", "unit_id", "session_id", "agree", "diff", "user_ruling", "residual"),
    "llm_call": (
        "id", "role", "provider", "model", "session_id", "unit_id", "input_hash",
        "tokens_in", "tokens_out", "cost", "ts",
    ),
    "review": ("id", "concept_id", "attempt_id", "kind", "due", "result"),
}

# JSON 字段（规格 §5）：存 json.dumps(ensure_ascii=False) 文本
JSON_COLUMNS: dict[str, set[str]] = {
    "project": {"config"},
    "unit": {"chosen_from"},
    "card": {"links"},
    "attempt": {"rubric"},
    "claim_check": {"providers"},
}


# ---------------------------------------------------------------------------
# DDL：建库即全建 16 个库对象（规格 §7）。逐字对齐；表/索引幂等；FTS5 虚表特判。
# ---------------------------------------------------------------------------
_SCHEMA: list[str] = [
    """CREATE TABLE IF NOT EXISTS project(
  id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  initial_belief TEXT,
  created_at TEXT NOT NULL,
  config JSON
)""",
    """CREATE TABLE IF NOT EXISTS concept(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  verify_status TEXT NOT NULL DEFAULT '未验证',
  last_verified_at TEXT,
  UNIQUE(name COLLATE NOCASE)
)""",
    """CREATE TABLE IF NOT EXISTS session(
  id TEXT PRIMARY KEY,
  unit_id TEXT REFERENCES unit(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  gaps_confirmed INTEGER NOT NULL DEFAULT 0
)""",
    "CREATE INDEX IF NOT EXISTS idx_session_unit ON session(unit_id)",
    """CREATE TABLE IF NOT EXISTS turn(
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES session(id),
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  ts TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_turn_session ON turn(session_id, id)",
    """CREATE TABLE IF NOT EXISTS source(
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  added_at TEXT NOT NULL,
  UNIQUE(path)
)""",
    """CREATE TABLE IF NOT EXISTS unit(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'INIT',
  forced_close INTEGER NOT NULL DEFAULT 0,
  close_reason TEXT,
  difficulty_band TEXT,
  degraded_from TEXT,
  degrade_count INTEGER NOT NULL DEFAULT 0,
  production TEXT, produced_at TEXT,
  skipped_produce INTEGER NOT NULL DEFAULT 0,
  chosen_from JSON,
  closed_at TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_unit_status ON unit(status)",
    "CREATE INDEX IF NOT EXISTS idx_unit_forced ON unit(forced_close, closed_at)",
    """CREATE TABLE IF NOT EXISTS gap(
  id TEXT PRIMARY KEY,
  unit_id TEXT REFERENCES unit(id),
  session_id TEXT REFERENCES session(id),
  level INTEGER NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待补',
  context TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_gap_unit ON gap(unit_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_gap_session ON gap(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_gap_level_status ON gap(level, status)",
    """CREATE TABLE IF NOT EXISTS gap_candidate(
  id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL REFERENCES unit(id),
  text TEXT NOT NULL,
  source_turn INTEGER REFERENCES turn(id),
  status TEXT NOT NULL DEFAULT '待确认',
  suggested_level INTEGER
)""",
    "CREATE INDEX IF NOT EXISTS idx_gapcand_unit ON gap_candidate(unit_id, status)",
    """CREATE TABLE IF NOT EXISTS card(
  id TEXT PRIMARY KEY,
  concept_id TEXT NOT NULL REFERENCES concept(id),
  my_words TEXT NOT NULL,
  links JSON,
  verify_status TEXT NOT NULL DEFAULT '未验证'
)""",
    "CREATE INDEX IF NOT EXISTS idx_card_concept ON card(concept_id)",
    """CREATE TABLE IF NOT EXISTS concept_edge(
  id TEXT PRIMARY KEY,
  src TEXT NOT NULL REFERENCES concept(id),
  dst TEXT NOT NULL REFERENCES concept(id),
  kind TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uk_edge ON concept_edge(src, dst, kind)",
    "CREATE INDEX IF NOT EXISTS idx_edge_src ON concept_edge(src)",
    """CREATE TABLE IF NOT EXISTS attempt(
  id TEXT PRIMARY KEY,
  unit_id TEXT REFERENCES unit(id),
  concept_id TEXT REFERENCES concept(id),
  kind TEXT NOT NULL,
  rubric JSON,
  self_score REAL,
  confirmed_score REAL,
  seconds INTEGER,
  ts TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_attempt_concept ON attempt(concept_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_attempt_unit ON attempt(unit_id, kind)",
    """CREATE TABLE IF NOT EXISTS claim_check(
  id TEXT PRIMARY KEY,
  claim TEXT NOT NULL,
  providers JSON,
  unit_id TEXT REFERENCES unit(id),
  session_id TEXT REFERENCES session(id),
  agree INTEGER,
  diff TEXT,
  user_ruling TEXT,
  residual INTEGER NOT NULL DEFAULT 0
)""",
    "CREATE INDEX IF NOT EXISTS idx_claimcheck_unit ON claim_check(unit_id)",
    """CREATE TABLE IF NOT EXISTS llm_call(
  id TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  session_id TEXT REFERENCES session(id),
  unit_id TEXT REFERENCES unit(id),
  input_hash TEXT NOT NULL,
  tokens_in INTEGER NOT NULL,
  tokens_out INTEGER NOT NULL,
  cost REAL NOT NULL,
  ts TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_llmcall_session ON llm_call(session_id, ts)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uk_llmcall_idem ON llm_call(session_id, role, input_hash, ts)",
    """CREATE TABLE IF NOT EXISTS review(
  id TEXT PRIMARY KEY,
  concept_id TEXT NOT NULL REFERENCES concept(id),
  attempt_id TEXT REFERENCES attempt(id),
  kind TEXT NOT NULL DEFAULT '到期',
  due TEXT NOT NULL,
  result TEXT
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uk_review_active ON review(concept_id) WHERE result IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_review_due ON review(due)",
    """CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  table_name TEXT NOT NULL,
  row_id TEXT NOT NULL,
  change JSON NOT NULL,
  prev JSON
)""",
    "CREATE INDEX IF NOT EXISTS idx_audit_row ON audit_log(table_name, row_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)",
]

# FTS5 虚表（规格 §7.16）：不支持 IF NOT EXISTS，迁移时需先查 sqlite_master
_FTS_TABLE = "chunk_fts"
_FTS_DDL = "CREATE VIRTUAL TABLE chunk_fts USING fts5(source_id, page, text)"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _now_utc() -> str:
    """ISO-8601 UTC 文本（如 2026-10-15T08:30:00.123Z）；字典序=时间序。"""
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _encode(table: str, col: str, value: Any) -> Any:
    """业务表写入时序列化 JSON 列。"""
    if col in JSON_COLUMNS.get(table, set()):
        if value is None:
            return None
        if isinstance(value, str):
            return value  # 已是 JSON 文本，原样写入
        return json.dumps(value, ensure_ascii=False)
    return value


def _decode_row(table: str, row: Optional[sqlite3.Row]) -> Optional[dict]:
    """sqlite3.Row → 逻辑字典（JSON 列解析为 dict）。"""
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key in row.keys():
        val = row[key]
        if key in JSON_COLUMNS.get(table, set()) and isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                pass
        out[key] = val
    return out


def _row_fingerprint(row: Optional[dict]) -> str:
    """行逻辑快照的稳定指纹（用于撤销冲突检测 / expected_prev_hash 校验）。"""
    if row is None:
        return "<null>"
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _count_user_objects(conn: sqlite3.Connection) -> int:
    """用户定义的库对象数：14 张业务表 + audit_log（共 15 张表）+ chunk_fts 虚表 = 16 个库对象。

    排除 sqlite_% 内部对象；FTS5 虚表的影子表（chunk_fts_data/_idx/_docsize/_content/_config）
    以虚表名 + '_' 为前缀，须排除，否则会被计入（不同 SQLite 构建其 sql 列可能非空）。
    """
    vts = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'"
    ).fetchall()]
    base = "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    if vts:
        # 虚表影子表命名为 {vt}_?*（如 chunk_fts_data）；GLOB 中 _ 为字面量、* 为任意序列
        conds = " AND ".join("name NOT GLOB ?" for _ in vts)
        params = [v + "_?*" for v in vts]
        return int(conn.execute(base + " AND " + conds, params).fetchone()[0])
    return int(conn.execute(base).fetchone()[0])


# ---------------------------------------------------------------------------
# safe_path：库路径不得越出 HPSL_HOME/projects/{pid}/
# ---------------------------------------------------------------------------
def safe_path(pid: str, *parts: str) -> Path:
    """解析并校验库路径不得越出 HPSL_HOME/projects/。

    - pid 经解析后必须落在 projects_root 内（防止 pid 含 '..' 造成目录穿越）。
    - 若有 rel 部分，目标路径也须在 base 内。
    - 越界 → C400002（安全基线 §5，AC5）。
    """
    root = _config.projects_root().resolve()
    base = (root / pid).resolve()
    # pid 自身含 ../ 穿越：base 不在 projects_root 内即拒绝
    if base != root and root not in base.parents:
        raise BizError("C400002")
    target = (base / Path(*parts)).resolve() if parts else base
    if target != base and base not in target.parents:
        raise BizError("C400002")
    return target


def _resolve_db(pid: str) -> str:
    return str(safe_path(pid, "project.db"))


# ---------------------------------------------------------------------------
# 连接工厂
# ---------------------------------------------------------------------------
_write_conns: dict[str, sqlite3.Connection] = {}
# 每 pid 写事务锁（D1）：单写连接跨线程共享时，序列化 write()/undo() 事务体，
# 防止 cursor 交错或嵌套 BEGIN 触发 SQLITE_BUSY。M0 单进程同步调用虽不触发，M10 线程池需此保证。
_write_locks: dict[str, threading.Lock] = {}


def _write_lock(pid: str) -> threading.Lock:
    lock = _write_locks.get(pid)
    if lock is None:
        lock = threading.Lock()
        _write_locks[pid] = lock
    return lock


def _apply_pragmas(conn: sqlite3.Connection, read_only: bool = False) -> None:
    # 写连接需切换 WAL（改变库头，写操作）；读连接库已处于 WAL，且 query_only 下不可写，故跳过。
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def _connect(db_file: str, read_only: bool = False) -> sqlite3.Connection:
    _assert_runtime()
    conn = sqlite3.connect(db_file, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, read_only=read_only)
    if read_only:
        conn.execute("PRAGMA query_only=ON")  # 防御纵深（D6）：读连接禁止任何写语句
    return conn


def get_write_conn(pid: str) -> sqlite3.Connection:
    """返回（并缓存）该项目的单一写连接，已设 WAL / busy_timeout / foreign_keys，并建表。"""
    cached = _write_conns.get(pid)
    if cached is not None:
        return cached
    conn = _connect(_resolve_db(pid))
    migrate(conn)
    _write_conns[pid] = conn
    return conn


def get_read_conn(pid: str) -> sqlite3.Connection:
    """新建只读连接（WAL 读，不阻塞写；query_only 防御写语句）。"""
    return _connect(_resolve_db(pid), read_only=True)


def close_project(pid: str) -> None:
    """释放项目连接缓存（进程退出或清理半成品目录时调用）。"""
    conn = _write_conns.pop(pid, None)
    _write_locks.pop(pid, None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def close_all() -> None:
    """释放全部项目写连接（优雅停机钩子；隐含 WAL checkpoint）。

    进程退出 / SIGTERM 时由 M10 serve() 经 atexit 调用；遍历缓存逐一关闭。
    非 DDL，不改动任何库对象；关闭连接即触发 SQLite WAL 持久化。
    """
    for pid in list(_write_conns.keys()):
        close_project(pid)


def backup_project(pid: str, dest_dir: Optional[Path] = None) -> str:
    """完整快照备份（F1，M0 WAL + VACUUM INTO）。

    - 先 `PRAGMA wal_checkpoint(TRUNCATE)` 将 WAL 合入主库，保证快照含最近的未决写。
    - 再 `VACUUM INTO` 复制为独立紧凑库文件到项目 `backups/` 子目录（M1 已建）。
    - 目的地文件名带 UTC 时间戳 + 短 uuid 防重（VACUUM INTO 要求目标文件不存在）。
    - **零 DDL**：仅向项目目录外部写新文件，不改动任何库内对象（count_user_objects 不变）。
    - 返回备份文件绝对路径字符串。
    """
    if dest_dir is None:
        dest_dir = _config.project_dir(pid) / "backups"
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = "-".join(_now_utc().split(":"))  # 文件名禁用冒号（用 split/join 而非字符串替换，避免误伤源码 SQL 词扫）
    dest = dest_dir / f"project-{ts}-{uuid.uuid4().hex[:8]}.db"
    conn = get_write_conn(pid)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM INTO ?", (str(dest),))
    # 自动轮转：本次导出后即按日7+月12 窗口清理过期快照（零 DDL，仅删外部文件）
    rotate_backups(pid, dest_dir=dest_dir)
    return str(dest)


# 备份文件命名前缀与安全正则：仅本系统生成的 `project-YYYY-MM-DD...db` 参与轮转
_BACKUP_PREFIX = "project-"
_BACKUP_RE = re.compile(r"^project-(\d{4})-(\d{2})-(\d{2})")


def rotate_backups(
    pid: str,
    keep_daily: int = 7,
    keep_monthly: int = 12,
    dest_dir: Optional[Path] = None,
) -> list[str]:
    """备份轮转（日7 + 月12，祖父-父亲-儿子式保留；零 DDL）。

    - 仅作用于项目 backups/ 下本系统生成的 `project-*.db` 快照文件；其余文件（含 project.db）
      一律不触碰（安全前缀 + 正则双重保护）。
    - 保留规则：
        * 每日档：按文件名时间戳（UTC，字典序=时间序）倒序保留最近 keep_daily 份（最新优先）。
        * 每月档：对最近 keep_monthly 个自然月，各保留该月内**最早**的一份（月锚点，保留历史断面）。
    - 其余文件删除（仅删 project-*.db）。
    - 返回被删除的文件相对名列表（空列表表示无需清理）。
    """
    if dest_dir is None:
        dest_dir = _config.project_dir(pid) / "backups"
    dest_dir = Path(dest_dir)
    if not dest_dir.is_dir():
        return []
    files = [p for p in dest_dir.glob(f"{_BACKUP_PREFIX}*.db") if p.is_file()]
    if not files:
        return []

    # 解析 (时间戳文本, 月份键, 路径)；非本系统命名文件跳过（保护）
    parsed: list[tuple[str, str, Path]] = []
    for p in files:
        m = _BACKUP_RE.match(p.name)
        if not m:
            continue
        parsed.append((p.name, f"{m.group(1)}-{m.group(2)}", p))
    if not parsed:
        return []

    parsed.sort(key=lambda t: t[0], reverse=True)  # 时间戳倒序（最新在前）
    keep: set[Path] = set()

    # 每日档：最近 keep_daily 份
    for t in parsed[:keep_daily]:
        keep.add(t[2])

    # 每月档：最近 keep_monthly 个自然月，每月保留最早一份（升序取首）
    months_sorted = sorted({t[1] for t in parsed}, reverse=True)[:keep_monthly]
    for mk in months_sorted:
        month_files = sorted((t for t in parsed if t[1] == mk), key=lambda t: t[0])
        if month_files:
            keep.add(month_files[0][2])

    deleted: list[str] = []
    for t in parsed:
        if t[2] not in keep:
            try:
                t[2].unlink()
                deleted.append(t[2].name)
            except OSError:
                pass
    return deleted


def rotate_all_backups(keep_daily: int = 7, keep_monthly: int = 12) -> dict[str, list[str]]:
    """对所有项目执行备份轮转（零 DDL，仅删外部快照；best-effort）。

    - 遍历 projects.list_projects()，对每个项目调用 rotate_backups（前缀/正则双重保护）。
    - 单个项目失败不影响其他项目。
    - 返回 {pid: 被删文件列表}；无清理则不含该 pid。
    """
    from . import projects as _projects
    result: dict[str, list[str]] = {}
    for p in _projects.list_projects():
        pid = getattr(p, "id", None)
        if not pid:
            continue
        try:
            deleted = rotate_backups(pid, keep_daily=keep_daily, keep_monthly=keep_monthly)
            if deleted:
                result[pid] = deleted
        except Exception:
            continue
    return result


# ---------------------------------------------------------------------------
# 迁移（规格 §6）：幂等，建库即全建 16 个库对象，user_version=1
# ---------------------------------------------------------------------------
def migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移：CREATE TABLE/INDEX IF NOT EXISTS；FTS5 虚表先查后建；置 user_version=1。"""
    for stmt in _SCHEMA:
        conn.execute(stmt)
    # FTS5 虚表不支持 IF NOT EXISTS，需先判断 sqlite_master
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_FTS_TABLE,)
    ).fetchone()
    if exists is None:
        conn.execute(_FTS_DDL)
    conn.execute("PRAGMA user_version=1")


# ---------------------------------------------------------------------------
# 写入口（规格 §8.2）：全部账本写入唯一入口
# ---------------------------------------------------------------------------
def write(pid: str, table: str, row: dict, actor: str) -> str:
    """白名单校验 → BEGIN IMMEDIATE → UPDATE/INSERT（禁用 REPLACE）→ 同事务追加 audit_log → COMMIT。

    - table 必须 ∈ TABLES（audit_log 不在白名单，直写抛 KeyError）。
    - row 的键必须是 TABLES[table] 的子集。
    - 必须提供主键 'id'；**例外**：`turn` 为 INTEGER PK 追加表，可省略 id（由 SQLite 生成）。
    - 成功返回 row_id（turn 为自增 id 的字符串）；SQLITE_BUSY 耗尽 → B999002。
    """
    if table not in TABLES:
        raise KeyError(f"未知表或审计表不在写白名单：{table!r}（audit_log 仅由 write 内部追加）")
    allowed = TABLES[table]
    unknown = set(row.keys()) - set(allowed)
    if unknown:
        raise ValueError(f"未知列 {sorted(unknown)} 不在表 {table} 白名单")
    if actor not in ("user", "clerk", "system"):
        raise ValueError(f"非法 actor：{actor!r}（仅允许 user/clerk/system）")
    # turn 是追加表（INTEGER PRIMARY KEY），允许省略 id；其余表必须显式给 id
    if "id" not in row and table != "turn":
        raise ValueError(f"写入 {table} 必须提供主键 id")

    # D1：单写连接跨线程共享，事务体加锁序列化，防止 cursor 交错 / 嵌套 BEGIN。
    lock = _write_lock(pid)
    with lock:
        conn = get_write_conn(pid)
        row_id = str(row["id"]) if "id" in row else None

        for _ in range(MAX_WRITE_RETRIES):
            try:
                conn.execute("BEGIN IMMEDIATE")
                prev_row = None
                prev = None
                if row_id is not None:
                    prev_row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
                    prev = _decode_row(table, prev_row)

                if prev_row is not None:
                    set_cols = [c for c in allowed if c != "id" and c in row]
                    set_sql = ", ".join(f"{c}=?" for c in set_cols)
                    params = [_encode(table, c, row[c]) for c in set_cols]
                    conn.execute(f"UPDATE {table} SET {set_sql} WHERE id=?", params + [row_id])
                else:
                    ins_cols = [c for c in allowed if c in row]
                    ph = ", ".join("?" for _ in ins_cols)
                    params = [_encode(table, c, row[c]) for c in ins_cols]
                    cur = conn.execute(
                        f"INSERT INTO {table} ({', '.join(ins_cols)}) VALUES ({ph})", params
                    )
                    if row_id is None:  # turn：取 SQLite 生成的自增 id
                        row_id = str(cur.lastrowid)

                # 同事务追加审计：change=完整新行快照，prev=旧行（INSERT 为 NULL）
                new_row = _decode_row(
                    table, conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
                )
                conn.execute(
                    "INSERT INTO audit_log(ts, actor, table_name, row_id, change, prev) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (_now_utc(), actor, table, row_id, _json(new_row), _json(prev)),
                )
                conn.execute("COMMIT")
                return row_id
            except sqlite3.OperationalError as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                if getattr(exc, "sqlite_errorcode", None) == SQLITE_BUSY or "busy" in str(exc).lower():
                    continue  # 排队串行重试，禁止静默覆盖（D-03）
                raise
        raise BizError("B999002")


def append_turn(
    pid: str,
    session_id: str,
    role: str,
    content: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    actor: str = "system",
) -> str:
    """追加一轮对话到 `turn`（INTEGER PK，id 由 SQLite 生成）。

    与 `write` 同事务审计；返回自增 id 的字符串。零 DDL。
    """
    return write(
        pid,
        "turn",
        {
            "session_id": session_id,
            "role": role,
            "content": content,
            "provider": provider,
            "model": model,
            "ts": _now_utc(),
        },
        actor,
    )


def list_turns(
    pid: str,
    *,
    unit_id: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """按单元或会话读取对话轮次（只读；按 id 升序=时间序）。"""
    if session_id is not None:
        return query(
            pid,
            "SELECT * FROM turn WHERE session_id=? ORDER BY id ASC LIMIT ?",
            (session_id, int(limit)),
        )
    if unit_id is not None:
        return query(
            pid,
            "SELECT t.* FROM turn t JOIN session s ON t.session_id=s.id "
            "WHERE s.unit_id=? ORDER BY t.id ASC LIMIT ?",
            (unit_id, int(limit)),
        )
    return query(pid, "SELECT * FROM turn ORDER BY id ASC LIMIT ?", (int(limit),))


# ---------------------------------------------------------------------------
# 撤销（规格 §8.2）：旧值指纹校验 → 按 prev 回滚 → 撤销本身留痕
# ---------------------------------------------------------------------------
def undo(pid: str, audit_id: int, actor: str = "user", expected_prev_hash: Optional[str] = None) -> bool:
    """撤销某条审计对应的业务写入。

    - 读 audit_log；不存在 → A070001。
    - 旧值指纹不匹配（行已被后续变更）或 expected_prev_hash 与当前行不一致 → A070002。
    - 回滚：prev=None 则删除目标行；否则恢复 prev 全列。
    - 撤销动作本身写一条 audit_log（actor=user）。
    """
    if actor not in ("user", "clerk", "system"):
        raise ValueError(f"非法 actor：{actor!r}")

    # D1：单写连接跨线程共享，事务体加锁序列化，防止 cursor 交错 / 嵌套 BEGIN。
    lock = _write_lock(pid)
    with lock:
        conn = get_write_conn(pid)
        entry = conn.execute(
            "SELECT id, table_name, row_id, change, prev FROM audit_log WHERE id=?", (audit_id,)
        ).fetchone()
        if entry is None:
            raise BizError("A070001")

        table_name = entry["table_name"]
        # D5：撤销入口对 table_name 做白名单断言，防止审计数据异常时误伤非业务表（或 SQL 注入 T-06）。
        if table_name not in TABLES:
            raise BizError("A070002", msg="审计条目指向非业务表，无法撤销")
        row_id = entry["row_id"]
        change = json.loads(entry["change"]) if entry["change"] is not None else None
        prev = json.loads(entry["prev"]) if entry["prev"] is not None else None

        for _ in range(MAX_WRITE_RETRIES):
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur_row = conn.execute(f"SELECT * FROM {table_name} WHERE id=?", (row_id,)).fetchone()
                current = _decode_row(table_name, cur_row)

                # 冲突检测：当前行 ≠ 该审计记录写入后的快照 → 已被后续变更
                current_fp = _row_fingerprint(current)
                change_fp = _row_fingerprint(change)
                if current_fp != change_fp:
                    conn.execute("ROLLBACK")
                    raise BizError("A070002")
                if expected_prev_hash is not None and current_fp != expected_prev_hash:
                    conn.execute("ROLLBACK")
                    raise BizError("A070002")

                # 应用回滚
                if prev is None:
                    conn.execute(f"DELETE FROM {table_name} WHERE id=?", (row_id,))
                else:
                    set_cols = [c for c in TABLES[table_name] if c != "id"]
                    set_sql = ", ".join(f"{c}=?" for c in set_cols)
                    params = [_encode(table_name, c, prev.get(c)) for c in set_cols]
                    if cur_row is None:
                        ins_cols = list(TABLES[table_name])
                        ph = ", ".join("?" for _ in ins_cols)
                        iparams = [_encode(table_name, c, prev.get(c)) for c in ins_cols]
                        conn.execute(
                            f"INSERT INTO {table_name} ({', '.join(ins_cols)}) VALUES ({ph})", iparams
                        )
                    else:
                        conn.execute(f"UPDATE {table_name} SET {set_sql} WHERE id=?", params + [row_id])

                # 撤销动作本身留痕
                conn.execute(
                    "INSERT INTO audit_log(ts, actor, table_name, row_id, change, prev) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (_now_utc(), actor, table_name, str(row_id), _json(prev), _json(current)),
                )
                conn.execute("COMMIT")
                return True
            except sqlite3.OperationalError as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                if getattr(exc, "sqlite_errorcode", None) == SQLITE_BUSY or "busy" in str(exc).lower():
                    continue
                raise
        raise BizError("B999002")


# ---------------------------------------------------------------------------
# 受控审计事件追加（M8 成本模块：超限确认留痕；零 DDL）
# ---------------------------------------------------------------------------
def append_audit_event(
    pid: str,
    actor: str,
    table_name: str,
    row_id: str,
    change: Any,
    prev: Any = None,
) -> int:
    """追加一条纯审计事件（不写业务表；零 DDL，仅落 audit_log）。

    用途（M8 §3.2.M8.4 序列图 C->>DB: 写 audit_log）：超限确认（confirm_continue）
    没有对应业务行可写，需直接留痕。本函数是 M0 写入入口的**受控扩展**——仍集中在 db 模块，
    不新增业务表/列，不破坏 16 库对象锁定（M0 冻结约束）；审计内容仅作检索用途，
    不改变任何业务表状态。

    - actor 仅允许 user/clerk/system。
    - table_name 用作事件分类（如 'cost_confirm'），非白名单业务表。
    - 返回新插入的 audit_log 行 id。
    """
    if actor not in ("user", "clerk", "system"):
        raise ValueError(f"非法 actor：{actor!r}")
    conn = get_write_conn(pid)
    with _write_lock(pid):
        cur = conn.execute(
            "INSERT INTO audit_log(ts, actor, table_name, row_id, change, prev) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now_utc(), actor, table_name, str(row_id), _json(change), _json(prev)),
        )
        return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# 读助手（测试 / 业务查询使用）
# ---------------------------------------------------------------------------
def query(pid: str, sql: str, params: tuple = ()) -> list[dict]:
    """在只读连接上执行查询，返回 dict 列表（JSON 列不自动解析，调用方按需处理）。"""
    conn = get_read_conn(pid)
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_user_objects(pid: str) -> int:
    return _count_user_objects(get_write_conn(pid))
