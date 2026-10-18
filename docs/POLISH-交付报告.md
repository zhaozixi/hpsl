# HPSL v0 · M10B 收尾打磨三项交付报告

> 范围：settings 空值打磨（D-M10B 低危）/ 备份自动轮转（日7+月12）/ 概念图交互增强。
> 纪律基线（冻结约束全守住）：零 DDL（16 库对象）、`db.write` 唯一写入口、无新错误码、引擎 headless 不变、app.py 中间件顺序不变、读/写安全分层正确。

## 一、settings 空值打磨

**问题**：项目 config 某些字段若落库为 `null`，原模板 `value="{{ config.x }}"` 会渲染字面量 `value="None"`，且保存时 `Number('')===0` 会把留空字段误写为 0 / None 污染配置。

**改动**：`hpsl/hpsl/templates/settings.html`（纯前端，无后端变更）
- 9 个数值 input 统一改用 `value="{{ '' if config.X is none else config.X }}"`：
  - 仅在值为 `None` 时渲染空串；合法的 `0.0`（如 `skipped_produce_max` 默认 0.0）仍正常显示为 `0.0`。
- `review_intervals` 改用 `value="{{ (config.review_intervals or []) | join(',') }}"`，避免 `None | join` 的未定义行为。
- 保存 JS `saveSettings`：数值字段与 `review_intervals` 若留空则**不参与提交**（omit），后端 `set_config` 合并语义保证「留空=保持原值/默认值」，杜绝误写 0 / None。
- 增加提示文案。

**验收（AC-S1/S2）**：`test_web_polish.py`
- `test_settings_null_threshold_renders_empty`：写入 `pass_threshold=None` 后 GET /settings → `name="pass_threshold" value=""`，且全页无 `value="None"`。
- `test_settings_zero_value_not_hidden`：合法 `0.0` 仍渲染为 `0.0`。
- `test_settings_save_omits_blank_keeps_others`：仅提交 `pass_threshold=0.75`，其余字段（如 `budget_cap_usd=10.0`、`session_minutes=25`）保持不变。

## 二、备份自动轮转（日7 + 月12）

**改动**：`hpsl/hpsl/db.py`（数据层，非 engine；零 DDL）
- 新增 `rotate_backups(pid, keep_daily=7, keep_monthly=12, dest_dir=None) -> list[str]`：
  - 仅作用于项目 `backups/` 下本系统生成的 `project-*.db` 快照（`_BACKUP_RE` 正则 + 前缀双重保护，绝不触碰 `project.db` 或他者文件）。
  - 保留规则（祖父-父亲-儿子式）：
    - 每日档：按文件名 UTC 时间戳倒序保留最近 `keep_daily`（7）份；
    - 每月档：对最近 `keep_monthly`（12）个自然月，各保留该月内**最早**的一份（月锚点，保留历史断面）。
  - 返回被删除文件相对名列表。
- `backup_project`：VACUUM INTO 之后自动调用 `rotate_backups`，实现**导出即轮转**（无需独立调度进程，契合本地单机运行形态）。

**偏差决策**
- D-P1：轮转采用「导出即轮转」而非独立后台调度线程——本地单机 `hpsl serve` 单 worker，避免引入后台定时器对优雅停机/WAL 持久化的耦合；行为确定、可单测。
- D-P2：保留窗口取「最近 7 日档 ∪ 近 12 月各最早月锚点」，而非简单「总保留 N 份」，确保历史月度断面不被日档滚动冲掉。

**验收（AC-R1~R4）**：`test_web_polish.py`
- `test_rotate_keeps_daily7_and_monthly12`：10 日档(10/01–10/10)+2 月锚点(09/15,08/15)+2 干扰文件，`rotate` 后精确删 10/02、10/03，保留 10/01 月锚点与 10/04–10/10 日档及两月锚点；非 `project-*.db` 文件（other-*.db / notes.txt）完好。
- `test_rotate_no_backups_is_noop`：无快照时返回 `[]`。
- `test_backup_project_auto_rotates`：预置 20 日档后调用 `backup_project`，导出并轮转后 `project-*.db` 恰好 8 份（7 日档 + 10/01 月锚点）。
- `test_zero_ddl_after_rotation`：`count_user_objects == 16` 不变。

## 三、概念图交互增强

**改动**：`hpsl/hpsl/templates/graph.html`（纯前端，路由/后端零变更，直接消费既有 `nodes`/`edge_views`/`concepts`）
- 视口组 `<g id="viewport">` 统一承载平移/缩放变换。
- **平移**：SVG 上 `pointerdown/move/up` 拖拽（指针捕获，`moved` 阈值区分点击与拖拽，避免误触选中）。
- **缩放**：滚轮以光标为锚点缩放（`scale` 限幅 0.2–6）。
- **节点点击高亮**：`selectNode(id)` 计算邻接集，非关联节点/边置 `.dim`，选中节点描边加粗；再次点击同一节点或点击空白取消。
- **详情面板** `#graph-detail`：注入 `NODES/EDGES/CONCEPTS` JSON，展示概念名、验证状态、最近验证时间、关联概念列表。
- **边类型标注**：每条边中点显示 `kind`（如 `prereq`）。
- **图例 + 重置视图**按钮（`resetView()` 复位变换与选中）。
- 空态（无节点）分支保留，脚本以 `NODES.length` 守卫不报错。

**验收（AC-G1/G2）**：`test_web_polish.py`
- `test_graph_interaction_hooks`：GET /graph 含 `id="concept-graph"`/`id="viewport"`/`id="graph-detail"`、`resetView`/`selectNode`/`selectNode('c1')`、`const NODES/EDGES/CONCEPTS` 注入、`prereq` 边标注、`概念节点（2）`；既有渲染契约（节点名、svg、空态）仍成立。
- `test_zero_ddl_after_graph_features`：`count_user_objects == 16` 不变。

## 四、Phase 5 白盒复核（主理人直接，6 项全 PASS）

| 项 | 结论 |
|----|------|
| 零 DDL（16 库对象） | PASS（3 处 `count_user_objects==16` 守护；轮转仅删外部文件，settings/graph 仅模板） |
| `db.write` 唯一写入口 | PASS（轮转删 OS 文件，不经账本；导出仍走 `backup_project`→VACUUM INTO） |
| 无新错误码 | PASS（本批次零新增；复用 A010001/B999001 既有语义） |
| 引擎 headless 未改 | PASS（仅 db.py 数据层 + 模板，engine/ 未触碰） |
| 中间件顺序未变 | PASS（app.py 未改，Security 在内 TraceId 在外） |
| 安全分层 | PASS（settings/graph 维持 read_security；轮转并入既有 write_security 备份路径） |

## 五、交付清单

- 新增/修改：`hpsl/hpsl/db.py`（rotate_backups + backup_project 自动调用）、`hpsl/hpsl/templates/settings.html`、`hpsl/hpsl/templates/graph.html`
- 新增测试：`tests/test_web_polish.py`（9 例，覆盖 AC-S1/S2、AC-R1~R4、AC-G1/G2）
- 测试终验：**277 passed, 1 skipped**（较 M10B 收口 268 +9，零回归；1 skipped 为 cryptography 缺失致加密用例跳过，非功能缺陷）

## 六、结论

M10B 收尾三项打磨全部完成，纪律红线全部守住，无 BLOCKER、无破坏性偏差。备份「日7+月12」窗口已自动生效；settings 不再出现字面 `None` 且保存留空不改值；概念图具备平移/缩放/点击高亮/详情能力。
