"""P2-01：本地知识库索引与 FTS5 检索（A-M7 / F14.6）。

职责（零 DDL，只使用既有 `source` 表与 `chunk_fts` FTS5 虚表）：
- `tokenize(text)`：优先 jieba 分词；不可用时退化为「中文 2 字 bigram + 英文/数字按词」的
  确定性兜底。**查询表达式的构造与（未来）分词索引必须共用本函数**，保证同一分词口径。
- `index_source(pid, directory)`：校验目录 → 递归收集 md/txt/markdown（不跟随目录外符号链接）
  → 按空行/段落切块（每块 ≤2000 字符）→ `db.write` 落 `source` 行 → 写锁内替换该 source 的
  `chunk_fts` 行。返回 `{source_id, files, chunks}`。
- `search(pid, query, limit)`：FTS5 `MATCH`（查询 token 经 `tokenize` 生成）；未命中时退化为
  `LIKE %query%`（中文整串兜底）。返回 `[{source_id, page, text, score}]`，`text` 为**原文**。
- `list_sources(pid)` / `delete_source(pid, source_id)`。

存储方案（P2-01 最终口径）：
  `chunk_fts.text` 存**原文**，确保检索结果可直接向用户展示命中原文（paper §M7：
  「检索显示原文或『未检索到』」）。FTS5 默认 unicode61 分词器对连续中文按整段成词，
  与 `tokenize` 的 jieba/bigram 口径不同，故中文查询可能 MATCH 不中；此时用 `LIKE` 整串
  兜底。英文/数字因 unicode61 与兜底分词器都按词切分，MATCH 可正常命中。

写入纪律（M0 冻结约束）：
- `source` 行走 `db.write`（同事务 audit_log）。
- `chunk_fts` 是检索专用虚表，按 M0 说明允许独立写入入口：在 `db._write_lock(pid)` 内用
  `db.get_write_conn(pid)` 直接 DELETE/INSERT，同一事务提交。
- `delete_source` 需删除 `source` 行，而 `db.write` 只支持 INSERT/UPDATE；故在同一写锁事务内
  直接 DELETE `source` + `chunk_fts`，并**同事务**追加 audit_log（prev=被删行快照），
  与 M0 撤销/审计语义保持一致。全程零 DDL，`count_user_objects` 恒为 16。

错误码：不新增，仅复用 C400001（参数/路径校验）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable

from .. import db
from ..common.errors import BizError

# ---------------------------------------------------------------------------
# 常量（冻结口径，测试断言依赖）
# ---------------------------------------------------------------------------
INDEX_EXTENSIONS: tuple[str, ...] = (".md", ".txt", ".markdown")
MAX_FILE_BYTES: int = 1_000_000
MAX_CHUNK_CHARS: int = 2000
DEFAULT_LIMIT: int = 5
MAX_LIMIT: int = 50

# jieba 为可选依赖：缺失时走确定性兜底（vendor python 环境默认未安装）
try:  # pragma: no cover - 取决于运行环境
    import jieba as _jieba  # type: ignore
except ImportError:  # pragma: no cover
    _jieba = None

# 英文/数字词 + 连续中文段；中文段在兜底分词器中按 2 字 bigram 展开
_TOKEN_SPAN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
# 至少含一个字母/数字/汉字才算有效 token（过滤纯标点）
_MEANINGFUL_RE = re.compile(r"[0-9A-Za-z_\u3400-\u4dbf\u4e00-\u9fff]")


# ---------------------------------------------------------------------------
# 分词（索引/查询同一口径）
# ---------------------------------------------------------------------------
def tokenize(text: str) -> list[str]:
    """把文本切成检索 token（jieba 优先；缺失时中文 bigram + 英文/数字按词）。

    - 英文/数字统一小写，保证大小写无关。
    - 过滤空白与纯标点 token。
    - 兜底实现为纯确定性算法（不依赖外部词典），保证索引与查询可复现。
    """
    if not isinstance(text, str) or not text:
        return []
    if _jieba is not None:
        try:
            raw: Iterable[str] = _jieba.lcut(text)
        except Exception:  # pragma: no cover - jieba 内部异常时退回兜底
            raw = _fallback_cut(text)
    else:
        raw = _fallback_cut(text)
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        tok = (tok or "").strip().lower()
        if not tok or not _MEANINGFUL_RE.search(tok):
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _fallback_cut(text: str) -> list[str]:
    """确定性兜底切词：中文连续段按 2 字 bigram，英文/数字按词。"""
    out: list[str] = []
    for m in _TOKEN_SPAN_RE.finditer(text):
        span = m.group(0)
        if span[0].isascii():
            out.append(span)
            continue
        if len(span) <= 2:
            out.append(span)
        else:
            out.extend(span[i : i + 2] for i in range(len(span) - 1))
    return out


def _match_expression(query: str) -> str:
    """由查询构造 FTS5 MATCH 表达式：每个 token 用双引号包裹后 OR 连接。

    引号包裹可避免 FTS5 把 `-`、`:`、`*` 等当语法符号；内部双引号按 FTS5 规则双写转义。
    """
    tokens = tokenize(query)
    if not tokens:
        tokens = [query.strip()] if query.strip() else []
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _like_pattern(query: str) -> str:
    """LIKE 兜底模式：转义 `%` `_` `\\`（配合 ESCAPE '\\' 使用）。"""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ---------------------------------------------------------------------------
# 目录校验与文件收集
# ---------------------------------------------------------------------------
def _resolve_directory(directory: str) -> Path:
    """校验并解析知识库根目录；非法/不存在/非目录 → C400001。"""
    if isinstance(directory, os.PathLike):  # 便于测试/调用方直接传 Path
        directory = os.fspath(directory)
    if not isinstance(directory, str) or not directory.strip():
        raise BizError("C400001", msg="知识库目录不能为空")
    if "\x00" in directory:
        raise BizError("C400001", msg="知识库目录路径非法")
    raw = Path(directory).expanduser()
    # 仅接受绝对路径：相对路径会以进程 cwd 为基准解析，存在「路径越界」歧义，直接拒绝
    if not raw.is_absolute():
        raise BizError("C400001", msg="知识库目录必须为绝对路径")
    try:
        root = raw.resolve()
    except (OSError, RuntimeError) as exc:  # 非法/循环符号链接等
        raise BizError("C400001", msg=f"知识库目录路径非法：{directory}") from exc
    if not root.exists():
        raise BizError("C400001", msg=f"知识库目录不存在：{directory}")
    if not root.is_dir():
        raise BizError("C400001", msg=f"知识库路径不是目录：{directory}")
    return root


def _inside(root: Path, target: Path) -> bool:
    """target 是否仍在 root 内（root 自身视为在内）。"""
    return target == root or root in target.parents


def _iter_index_files(root: Path):
    """遍历 root 下受支持的文本文件（不跟随指向 root 外的符号链接，不越界）。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            sub = base / name
            try:
                if sub.is_symlink():
                    continue  # 目录符号链接一律不跟随（防越界/循环）
                resolved = sub.resolve()
            except OSError:
                continue
            if not _inside(root, resolved):
                continue
            kept.append(name)
        dirnames[:] = kept  # 原地裁剪，阻止 os.walk 进入越界目录

        for name in filenames:
            fp = base / name
            try:
                if fp.is_symlink():
                    target = fp.resolve()
                    if not _inside(root, target):
                        continue  # 指向目录外的符号链接：跳过，不读取
                if fp.suffix.lower() not in INDEX_EXTENSIONS:
                    continue
                st = fp.stat()
                if not os.path.isfile(fp):
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    continue  # 超大文件跳过（保护索引体积与耗时）
            except OSError:
                continue
            yield fp


# ---------------------------------------------------------------------------
# 切块
# ---------------------------------------------------------------------------
def _chunk_text(text: str) -> list[str]:
    """按空行/段落切块，每块 ≤ MAX_CHUNK_CHARS；超长段落硬切。"""
    norm = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", norm)]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if not para:
            continue
        if len(para) > MAX_CHUNK_CHARS:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(para), MAX_CHUNK_CHARS):
                piece = para[i : i + MAX_CHUNK_CHARS].strip()
                if piece:
                    chunks.append(piece)
            continue
        if buf and len(buf) + 2 + len(para) > MAX_CHUNK_CHARS:
            chunks.append(buf)
            buf = para
        else:
            buf = para if not buf else f"{buf}\n\n{para}"
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def _collect_chunks(root: Path) -> tuple[int, list[tuple[str, str]]]:
    """读取目录下文件并切块；返回 (成功读取文件数, [(page, 原文 chunk)])。"""
    files = 0
    rows: list[tuple[str, str]] = []
    for fp in _iter_index_files(root):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # 单文件不可读：跳过，不中断整库索引
        files += 1
        try:
            page = fp.relative_to(root).as_posix()
        except ValueError:  # 理论上不可达（已在 root 内）
            page = fp.name
        if not page:
            page = "1"
        for chunk in _chunk_text(text):
            rows.append((page, chunk))
    return files, rows


# ---------------------------------------------------------------------------
# chunk_fts 独立写入入口（写锁 + 单事务）
# ---------------------------------------------------------------------------
def _replace_fts(pid: str, source_id: str, rows: list[tuple[str, str]]) -> None:
    """写锁内替换某 source 的全部 FTS 行（DELETE 旧行 + 批量 INSERT 新行，单事务）。

    `chunk_fts` 为检索专用虚表（M0 说明允许独立写入入口）；`source` 行已由 `db.write` 落库，
    本事务只维护检索索引，不触碰任何业务表，零 DDL。
    """
    conn = db.get_write_conn(pid)
    with db._write_lock(pid):
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM chunk_fts WHERE source_id=?", (source_id,))
            if rows:
                conn.executemany(
                    "INSERT INTO chunk_fts(source_id, page, text) VALUES (?, ?, ?)",
                    [(source_id, page, text) for page, text in rows],
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def index_source(pid: str, directory: str) -> dict:
    """索引本地知识库目录（幂等：同一路径复用同一 source 行并整体替换其 FTS 行）。

    步骤：
    1. 校验目录存在且为目录（否则 C400001）；仅接受绝对路径。
    2. 递归读取 `.md/.txt/.markdown`（≤1MB，UTF-8 errors="replace"；不跟随目录外符号链接）。
    3. 按空行/段落切块（每块 ≤2000 字符），`page` 取文件相对路径（无则 "1"）。
    4. `db.write(pid, "source", {id,path,kind:"folder",added_at}, "user")`。
    5. 写锁内 DELETE 该 source 旧 FTS 行并批量 INSERT 新行（单事务）。

    返回 `{source_id, files, chunks}`。
    """
    root = _resolve_directory(directory)
    files, rows = _collect_chunks(root)

    stored_path = str(root)
    existing = db.query(pid, "SELECT id FROM source WHERE path=?", (stored_path,))
    source_id = existing[0]["id"] if existing else uuid.uuid4().hex
    db.write(
        pid,
        "source",
        {
            "id": source_id,
            "path": stored_path,
            "kind": "folder",
            "added_at": db._now_utc(),
        },
        "user",
    )
    _replace_fts(pid, source_id, rows)
    return {"source_id": source_id, "files": files, "chunks": len(rows)}


def search(pid: str, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """检索知识库，返回 `[{source_id, page, text, score}]`（text 为索引中的**原文**）。

    - 空查询（或全为标点）→ []。
    - 先走 FTS5 MATCH（查询 token 由 `tokenize` 生成，OR 连接）；未命中再走 LIKE 整串兜底
      （中文连续串在 unicode61 下与 tokenizer 口径不同，需此兜底）。
    - `score`：FTS 命中为 `-rank`（越大越相关）；LIKE 兜底为 0.0。
    - 只返回仍存在于 `source` 表中的来源（防御历史孤儿 FTS 行）。
    """
    if not isinstance(query, str) or not query.strip():
        return []
    try:
        top = int(limit)
    except (TypeError, ValueError):
        top = DEFAULT_LIMIT
    if top <= 0:
        return []
    top = min(top, MAX_LIMIT)

    expr = _match_expression(query)
    items: list[dict] = []
    if expr:
        try:
            rows = db.query(
                pid,
                "SELECT source_id, page, text, rank FROM chunk_fts "
                "WHERE chunk_fts MATCH ? AND source_id IN (SELECT id FROM source) "
                "ORDER BY rank LIMIT ?",
                (expr, top),
            )
        except sqlite3.OperationalError:
            rows = []  # 极端查询语法错误时退回 LIKE，不向上抛
        for r in rows:
            rank = r.get("rank")
            items.append(
                {
                    "source_id": r["source_id"],
                    "page": r["page"],
                    "text": r["text"],
                    "score": float(-rank) if rank is not None else 0.0,
                }
            )
    if items:
        return items

    # LIKE 兜底（转义 %/_/\\；中文整串可命中）
    rows = db.query(
        pid,
        "SELECT source_id, page, text FROM chunk_fts "
        "WHERE text LIKE ? ESCAPE '\\' AND source_id IN (SELECT id FROM source) "
        "ORDER BY source_id, page LIMIT ?",
        (_like_pattern(query.strip()), top),
    )
    for r in rows:
        items.append(
            {
                "source_id": r["source_id"],
                "page": r["page"],
                "text": r["text"],
                "score": 0.0,
            }
        )
    return items


def list_sources(pid: str) -> list[dict]:
    """列出已挂载来源（按添加时间倒序），附各来源的 FTS chunk 数。"""
    rows = db.query(pid, "SELECT id, path, kind, added_at FROM source ORDER BY added_at DESC, id")
    counts = {
        r["source_id"]: int(r["c"])
        for r in db.query(pid, "SELECT source_id, count(*) AS c FROM chunk_fts GROUP BY source_id")
    }
    return [
        {
            "id": r["id"],
            "path": r["path"],
            "kind": r["kind"],
            "added_at": r["added_at"],
            "chunks": counts.get(r["id"], 0),
        }
        for r in rows
    ]


def delete_source(pid: str, source_id: str) -> dict:
    """删除来源及其 FTS 索引（source 行 + chunk_fts 行，同事务并留痕 audit_log）。

    `db.write` 不支持 DELETE，故在 `_write_lock(pid)` 内直接删除：同事务删除 `source` 行、
    `chunk_fts` 行，并追加 audit_log（prev=被删 source 行快照）。零 DDL。
    不存在 → C400001（复用参数校验码，不新增错误码）。
    """
    if not isinstance(source_id, str) or not source_id.strip():
        raise BizError("C400001", msg="source_id 不能为空")
    sid = source_id.strip()
    conn = db.get_write_conn(pid)
    with db._write_lock(pid):
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, path, kind, added_at FROM source WHERE id=?", (sid,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise BizError("C400001", msg=f"知识库来源不存在：{sid}")
            prev = {k: row[k] for k in row.keys()}
            chunks = conn.execute(
                "SELECT count(*) FROM chunk_fts WHERE source_id=?", (sid,)
            ).fetchone()[0]
            conn.execute("DELETE FROM chunk_fts WHERE source_id=?", (sid,))
            conn.execute("DELETE FROM source WHERE id=?", (sid,))
            conn.execute(
                "INSERT INTO audit_log(ts, actor, table_name, row_id, change, prev) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    db._now_utc(),
                    "user",
                    "source",
                    sid,
                    json.dumps({"deleted": True, "chunks": int(chunks)}, ensure_ascii=False),
                    json.dumps(prev, ensure_ascii=False),
                ),
            )
            conn.execute("COMMIT")
        except BizError:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    return {"source_id": sid, "deleted": True, "chunks": int(chunks)}
