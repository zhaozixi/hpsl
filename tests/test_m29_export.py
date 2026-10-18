"""M29 / P1-06 验收：Markdown 导出新增「对话记录」（conversations）类型。

- AC1：POST /api/v1/export/conversations → 200，文件落到项目 exports/ 目录；
  内容含 `# 对话记录导出`、会话标题与轮次。
- AC2：每个 session 一节（`## 会话 <id> · <started_at>`），会话按 started_at 升序；
  角色名映射为中文（user→我、answer_machine→答案机…），未知角色原样保留；
  内容经 `_scrub()` 脱敏。
- AC3：无对话记录时导出成功且输出「（无对话记录）」，不报错。
- AC4：导出前后 `db.count_user_objects(pid)` 均为 16（零 DDL）。

纪律：仅新增 kind，不动既有四种导出的断言；只读 db.query，零 DDL。
"""

from hpsl import config as cfg
from hpsl import db


def _create_and_switch(client, csrf_headers, name="M29导出项目", goal="目标g"):
    r = client.post("/api/v1/projects", headers=csrf_headers, json={"name": name, "goal": goal})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    client.post(f"/api/v1/projects/{d['id']}/switch", headers=csrf_headers)
    return d["id"]


def _add_session(pid: str, sid: str, started_at: str) -> None:
    db.write(
        pid,
        "session",
        {"id": sid, "unit_id": None, "started_at": started_at},
        "user",
    )


def _seed_conversations(pid: str) -> None:
    """两个会话：后插入的会话 started_at 更早，用于验证按 started_at 升序。"""
    _add_session(pid, "sess-late", "2026-02-01T00:00:00.000Z")
    _add_session(pid, "sess-early", "2026-01-01T00:00:00.000Z")

    # 轮次按 id 升序 = 追加顺序
    db.append_turn(
        pid, "sess-early", "user", "我想弄清 groupby 的执行顺序 sk-abcdefgh12345678",
        provider="fake", model="fake-model", actor="user",
    )
    db.append_turn(
        pid, "sess-early", "answer_machine", "先分组再聚合。",
        provider="fake", model="fake-model", actor="system",
    )
    db.append_turn(pid, "sess-early", "questioner", "那过滤在哪一步？", provider="fake", model="fake-model")
    db.append_turn(pid, "sess-early", "boundary_setter", "请举一个反例。", provider="fake", model="fake-model")
    db.append_turn(pid, "sess-early", "critic", "你没有考虑空分组。", provider="fake", model="fake-model")
    db.append_turn(pid, "sess-early", "clerk", "候选缺口 A。", provider="fake", model="fake-model")
    db.append_turn(pid, "sess-early", "topic_picker", "下一个选题：窗口函数。", provider="fake", model="fake-model")
    db.append_turn(pid, "sess-early", "mystery_role", "未知角色内容。", provider="fake", model="fake-model")

    db.append_turn(pid, "sess-late", "user", "第二个会话的问题。", provider="fake", model="fake-model", actor="user")


def _export(client, csrf_headers, kind: str = "conversations"):
    r = client.post(f"/api/v1/export/{kind}", headers=csrf_headers, json={})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# AC1：导出成功且文件落到 exports/
# ---------------------------------------------------------------------------
def test_export_conversations_success_writes_file(client, csrf_headers):
    pid = _create_and_switch(client, csrf_headers)
    _seed_conversations(pid)

    body = _export(client, csrf_headers)
    assert body["code"] == "0"

    fpath = cfg.project_dir(pid) / "exports" / body["data"]["file"]
    assert fpath.is_file()
    assert fpath.parent.name == "exports"
    assert fpath.name.startswith("conversations-") and fpath.suffix == ".md"

    content = fpath.read_text(encoding="utf-8")
    assert "# 对话记录导出" in content
    assert body["data"]["bytes"] == len(content)

    # 导出页同步出现第 5 个入口
    page = client.get("/export")
    assert page.status_code == 200
    assert "conversations" in page.text


# ---------------------------------------------------------------------------
# AC2：会话/轮次结构、中文角色名、内容与脱敏
# ---------------------------------------------------------------------------
def test_export_conversations_roles_content_and_order(client, csrf_headers):
    pid = _create_and_switch(client, csrf_headers)
    _seed_conversations(pid)

    body = _export(client, csrf_headers)
    content = (cfg.project_dir(pid) / "exports" / body["data"]["file"]).read_text(encoding="utf-8")

    # 每个 session 一节；按 started_at 升序（sess-early 先出现）
    early_heading = "## 会话 sess-early · 2026-01-01T00:00:00.000Z"
    late_heading = "## 会话 sess-late · 2026-02-01T00:00:00.000Z"
    assert early_heading in content
    assert late_heading in content
    assert content.index(early_heading) < content.index(late_heading)

    # 角色中文映射
    for label in ("我", "答案机", "提问者", "出题者", "挑错者", "书记员", "选题助手"):
        assert f"**{label}**" in content, f"缺少角色中文名：{label}"
    # 未知角色原样输出
    assert "**mystery_role**" in content

    # 轮次元信息：时间 + provider/model
    assert "fake/fake-model" in content
    # 内容以引用块承载
    assert "> 我想弄清 groupby 的执行顺序" in content
    assert "> 先分组再聚合。" in content

    # 脱敏：sk- 类密钥被替换
    assert "sk-abcdefgh12345678" not in content
    assert "[REDACTED]" in content


# ---------------------------------------------------------------------------
# AC3：无对话记录 → 成功且给出提示
# ---------------------------------------------------------------------------
def test_export_conversations_empty_outputs_note(client, csrf_headers):
    pid = _create_and_switch(client, csrf_headers, name="M29空对话", goal="无记录")
    assert db.count_user_objects(pid) == 16

    body = _export(client, csrf_headers)
    assert body["code"] == "0"
    content = (cfg.project_dir(pid) / "exports" / body["data"]["file"]).read_text(encoding="utf-8")
    assert "（无对话记录）" in content
    assert "# 对话记录导出" in content


# ---------------------------------------------------------------------------
# AC4：零 DDL 守护
# ---------------------------------------------------------------------------
def test_export_conversations_zero_ddl(client, csrf_headers):
    pid = _create_and_switch(client, csrf_headers)
    _seed_conversations(pid)
    assert db.count_user_objects(pid) == 16

    body = _export(client, csrf_headers)
    assert body["code"] == "0"
    assert db.count_user_objects(pid) == 16
