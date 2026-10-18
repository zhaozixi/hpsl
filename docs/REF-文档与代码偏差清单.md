# REF-2 · 文档 vs 代码偏差清单

> 文档性质：审计/对照（非基线的派生文档，可随代码演进更新）
> 编写日期：2026-10-18
> 对照基准：`D:/workspace/TLF/.workbuddy/delivery/`（锁定基线，只读）+ `D:/workspace/TLF/hpsl/docs/`（实现期文档）
> 对照对象：`D:/workspace/TLF/hpsl/hpsl/`（实现代码）
> 配套文档：[REF-1 · 三套 M 编号映射表](REF-M编号映射表.md)

---

## 0. 本次核对的可复现步骤与结论

| # | 动作 | 结果 |
|---|---|---|
| 1 | `python .workbuddy/delivery/verify_baseline.py` | **8/8 `OK`，`BASELINE INTACT`，退出码 0**（基线未被意外修改） |
| 2 | `PYTHONPATH=.venv/Lib/site-packages D:\soft\WorkBuddy\resources\vendor\python\python.exe -m pytest -q` | **`336 passed, 2 warnings in 38.54s`**（文档口径被复现） |
| 3 | 清点路由装饰器 | **31 个 `/api/v1` + 15 个页面 + `/healthz` `/readyz`**；设计口径为 **42 API + 11 页面** |
| 4 | 探查 `hpsl.db` 元数据 | `TABLES` 白名单 **14 张**，`_SCHEMA` 中 `CREATE TABLE` **15 条**（14 + `audit_log`），+ `chunk_fts` = **16 库对象** |
| 5 | 探查 `app.routes` | `/docs`、`/redoc`、`/openapi.json` **默认开放** |
| 6 | 全仓 grep | `RoleLLM` / `record_call` / `enqueue_reviews` **无任何路由调用方**；`chunk_fts` 仅建表；`turn` / `source` 零写入 |

> 说明：第 2 步之所以绕道 vendor python，是因为 `.venv/Scripts/python.exe` 已损坏（见 ENV-01）。这不是代码缺陷，是环境腐化。

**判定口径**：`已一致` 不单列；只列偏差。等级：**P0** 阻断主链或触碰安全红线 / **P1** 能力缺失或用户可见缺陷 / **P2** 范围收缩或无痛偏差 / **DOC** 文档自身错误 / **ENV** 环境问题。

**统计**：P0 = 8｜P1 = 15｜P2 = 15｜DOC = 15｜ENV = 3，合计 **56 项**。

---

## 1. P0：阻断主链 / 触碰安全红线（8 项）

> **修复状态（2026-10-18）**：**M24 P0-A/B 修复 P0-01~04**（建单元/产出/尝试/闭环 + 角色调用与记账，5 端点 + 22 例）；**M25 修复 P0-05~08**（密钥口令 + 随机 salt + fail-fast、五维限流补齐、Host/Origin 精确匹配、关闭 `/docs`），裁决为「A：改代码对齐文档」；**M26 补齐 turn 落库与会话回放**。全量 **393 passed**。详见 [M24](M24-交付报告.md) / [M25](M25-交付报告.md) / [M26](M26-交付报告.md) 交付报告。

| # | 文档依据 | 代码现状（证据） | 影响 | 建议 |
|---|---|---|---|---|
| P0-01 | 系统设计 §7 端点清单（`POST /api/v1/units`）；F4.1 选题；US-3 | 31 个 API 中**无** `POST /units`；全仓 grep `INSERT INTO unit` 无匹配；单元行只由测试/种子脚本写入 | 新用户 `/unit` 永远是空态；M23 手册教的"点推进到下一步"没有对象可推 | 补建单元端点（含 topic_picker 三候选）或最小"手动建单元" |
| P0-02 | F3/F4/F5；端点 `/roles/{role}/call`、`/roles/contracts`、`/units/{uid}/input-preview` | `RoleLLM` 仅在 `engine/__init__.py` 导出与 `tests/test_role_llm.py` 中引用；`routes/` 无 `roles` 导入 | 六角色契约、隔离、提示词全部落地但**从未被使用**；WEB 端没有任何 AI 能力 | 增加角色调用端点 + 会话/轮次落库 |
| P0-03 | F9.1；B-M8；US-1/AC-1.2 | `cost.record_call` 仅导出与测试引用，`llm_call` 无生产写入点 | `/budget` 恒空、`/stats` 花费恒 0、A080001（预算超限）永不触发 | 在适配器出口统一调用 `record_call` |
| P0-04 | F10.1；B-M6 | `review` 行的**创建**只发生在 `enqueue_reviews`，而后者仅测试调用（`start_review`/`submit_review_by_id` 只更新已存在的行） | 复测队列永远为空，`/review` 页面永远是"无到期" | `grading.close_unit` 成功后入队（概念来自卡片/尝试） |
| P0-05 | 安全设计 §6（Argon2id 内存≥64MiB/迭代≥3/并行度 4；**每文件 16 字节随机 salt**；口令仅内存、fail-fast） | `keystore.py`：`MASTER_SALT` 为**硬编码常量**；`_default_passphrase` = `sha256(secrets_dir + ":hpsl-v1")`；Argon2id `time_cost=2 / parallelism=8`；`cryptography` 缺失时**静默**降级 `PlaintextCipher`（仅 RuntimeWarning） | 静态加密的实际"秘密"= 源码 + 路径；§2.1.2 的"口令强度/轮换"实现上无对象；缺库时落明文，违 L4 红线字面 | 引入启动口令 + 随机 salt + 无口令 fail-fast；或明确记录"本地单机威胁模型下接受"，并从红线表移除对应承诺 |
| P0-06 | 冻结项 #5；T-17（50/20/10/2/5 五维） | `security.py::_check_rate` 只消费 `process` / `route` / `write_freq`；`llm_freq`、`sse_concurrency` 仅存在于 `config.DEFAULT_RATE_LIMITS`，**全仓无调用点** | 五维限流实际三维；LLM 频次与 SSE 并发无保护（SSE 本身也未实现） | 在 LLM 出口与 SSE 入口接线，或在文档中降级为"三维" |
| P0-07 | T-01/T-02；部署设计 C3（`ALLOWED_HOSTS={127.0.0.1:PORT,...}`，fail-fast） | `security.py::check_host`：空白名单 `return True`（默认全放行）；额外 `host.startswith("127.")` 放行任意 `127.*` 域名；返回 **400/C400003**（文档为 403/C403001）。`check_origin` 只判回环主机、**不比端口** | `Host: 127.evil.com` 可过；任意本机端口的页面可发起写请求；与"fail-fast"相反 | 精确匹配 `host:port`；移除 `127.` 前缀；空白名单 fail-fast；统一错误码 |
| P0-08 | 安全设计 §4.2.2（生产关闭 docs/redoc）；T-15 | `app.py` 中 `FastAPI(title="hpsl", version="0.0.0")` 未设 `docs_url=None`；实测 `/docs`、`/redoc`、`/openapi.json` 均 200 | 暴露完整 API 结构；与红线"生产关 docs"相反 | 关闭三端点（或按环境开关） |

---

## 2. P1：能力缺失 / 用户可见缺陷（15 项）

> **P1 批次状态（2026-10-18 · M29）**：P1-01~08 **全部关闭**——价格表（DeepSeek 官方价 + `unpriced_models`）、验证判分组（5 端点）、Provider 直连 + 真 SSE、每日快照 + 恢复 CLI、概念/卡片/边写入、对话导出、requirements/打包测试、LICENSE。
> 其中 **P1-02 为部分拆分**：`restatement` / `boundary-answer` / `followup` 三类作答仍由 `/attempts` 的 `kind` 承载（语义重合，见 M29 D-M29-3）。
> P1-11/12 已随 M27 修复；P1-06 的 CSRF 签名部分仍是取舍，见 P2-09。

> **修复状态（2026-10-18）**：M24 P0-B 修复 P1-08 / 09 / 10 / 13 / 14 / 15；**M27 修复 P1-11（HTMX 本地化）与 P1-12（graph 转义）**。详见 [M27-交付报告](M27-交付报告.md)。

| # | 文档依据 | 代码现状（证据） | 影响 | 建议 |
|---|---|---|---|---|
| P1-01 | 系统设计 §7：42 个 `/api/v1` | 实测 **44 个**（M24 +5、M27 +8、其中 contracts/preview 为新增能力）。数量已超，但**构成不同**：仍缺验证判分组（restatement/boundary/followup/self-score/confirm-score/force-close/critic/rubric）、`providers/*/chat`、`units/{uid}/sessions`、`degrade`；多出 graph/stats/budget/export/wizard/roles-contracts/preview。设计中的 `transition` 以 `/units/{uid}/move` 落地（命名不同） | 主链与治理读写均已可用；剩余为"细粒度判定端点" | 按需补 |
| P1-02 | 冻结项 #7（日快照 7 + 月快照 12，RPO≤24h）；部署设计 C4 | 无调度器；实际为"手动导出 + 导出即轮转 + 启动即轮转 + 6h daemon 轮转 + CLI"。**没有"每日自动生成快照"** | RPO≤24h 无载体；用户不点导出就没有当天快照 | 在 `serve()` 增加每日快照任务，或把 RPO 改为"手动快照"并回写文档 |
| P1-03 | F8.1（8 家，P0/MVP） | `GeminiAdapter.complete` 直接 `raise BizError("A040002")`；向导可填 Gemini、连通性测试也支持 gemini 分支 | 按向导配置 Gemini 的用户，第一次真实调用必然失败 | 要么实现 Gemini，要么在 UI 标"暂不支持" |
| P1-04 | F9.1；`pricing.yaml` 由用户维护 | 只登记 3 个模型（gpt-4o-mini / deepseek-chat / claude-3-5-haiku）；其余 5 家 11 个模型 `cost=0.0` | qwen/GLM/Kimi/豆包/gpt-4o 用户的花费恒为 0，预算闸门形同虚设 | 补齐价格表；未知模型给"未登记价格"提示而非静默 0 |
| P1-05 | 安全设计 §7（aggs：`logs/access.jsonl` 含 reason、`slow/traces/alerts`、热 30/90 天、SE-01/02 告警） | 全仓无 jsonl 写入点；仅 `audit_log` 表（在库内） | 拒绝记录不可追溯；E3 应急流程（按日志取证）无法执行 | 至少落 `access.jsonl`（拒绝原因）；其余按需 |
| P1-06 | 安全设计 §2.1.3（Cookie：HttpOnly + SameSite=Strict + Secure + 时间戳签名） | `security.py` 用无签名随机 token 双提交，`httponly=False`、`samesite="lax"`、无 `Secure`；无 `itsdangerous` | 双提交是合理取舍，**但文档未记录该取舍**，审计时会被判为缺陷 | 在文档补"取舍记录"，或加签名 |
| P1-07 | 冻结项 #9（七类测试守护，含 `test_contracts`）；F8 模型版本固定后重跑 | `tests/` 下**无** `test_contracts`（38 个文件均无） | 冻结承诺缺一类；换模型版本无回归门 | 补慢测试（真实 API，手动跑）或从冻结项中移除 |
| P1-08 | 设置页应能保存阈值（F1.2、M10B1） | `settings.html` 用 `htmx.ajax('PUT', url, {headers, body})`：htmx 1.9 返回 **Promise**，`xhr.addEventListener` 会抛 TypeError；且 context 的 `body` 不是受支持键（应为 `values`）。无 body → 后端 `_parse_body` 解析空 JSON → C400001 | "保存中…"永久停留／保存失败，设置页实际不可用 | 改 `fetch` + `X-CSRF-Token`，或普通 `<form hx-put>` |
| P1-09 | A080001 弹层应可关闭（M10B6） | `_quota_modal.html` 的 `confirmed` 分支仍渲染 `<div class="modal">`（`position:fixed; inset:0; z-index:100`），无关闭按钮 | 确认后整页被遮罩盖死 | `confirmed` 分支去掉 `modal` 类或加关闭 |
| P1-10 | 空态/成功提示应有视觉反馈 | 模板大量使用 `<p class="ok">`；`app.css` 只有 `.result.ok` / `.banner.ok` / `.badge.ok`，**无 `p.ok`** | 所有空态/成功提示渲染为普通黑字 | 补 `.ok`/`.notice` 通用段样式 |
| P1-11 | 本地单机 / 离线可用（README、M13 自述"离线优先"） | `base.html` 从 `unpkg.com` 加载 HTMX 1.9.12，无 integrity、阻塞渲染；注释自认"离线需拷贝本地" | 断网时所有 `hx-*` 失效，但内联 `onclick` 仍工作，交互行为分裂 | 把 `htmx.min.js` 落到 `static/` |
| P1-12 | T-07/T-14（提示词/数据最小化）、输出转义 | `graph.html` 的 tooltip/详情面板用 `innerHTML` 拼接概念名、关系类型、验证时间（数据来自 DB/LLM） | 存储型 XSS 面（当前单用户，风险有限但红线相关） | 改 `textContent` 或转义 |
| P1-13 | /backup 页应展示最新快照（M10B4） | `backup.html` 的 `hx-target="#backup-result"` 只替换结果区；`#backup-list` 与 `历史快照（N）` 计数不刷新 | 导出成功后列表仍显示旧数据，用户以为没备份 | 片段同时 OOB 更新列表与计数 |
| P1-14 | F12 错误体验；M23 易用性（"点不动"是最差体验） | `BizError` 处理器返回 **JSON + 4xx**；HTMX 默认不对 4xx swap，且全站无 `htmx:responseError` 钩子 | 状态机冲突、CSRF 失败、备份失败等场景**界面毫无提示** | 加全局 `htmx:responseError` → toast |
| P1-15 | M23（"界面用日常语言，不暴露内部术语"） | `unit_index.html` 输出 `强制闭环：{{ u.forced_close }}`（True/False）；`unit_detail.html` 标题带"② 工作流（M2）③ 缺口（M7）④ 挑错（M5）"；`_unit_cc_row.html` 输出 `residual=`；`review.html` 输出英文 `kind/due` | 与 M23 自我定位冲突，非技术用户仍会看到内部术语 | 用 `macros.html` 补 `bool_badge` / 修订标题 |

---

## 3. P2：范围收缩 / 无痛偏差（15 项）

> **P2 批次状态（2026-10-18 · M30）**：P2-01 ~ P2-12 **全部关闭**（知识库/空白上下文、多模型交叉验证/多挑错/要点核对、难度建议/衰减/周盘点、启动口令/keyring 链路/CSRF 签名/Anthropic 流式、可访问性、graph 事件委托）。
> 已知取舍：jieba 未安装（bigram 兜底）、keyring 仅 fake 模块验证、Anthropic 流式无真实 key 联调；知识库与衰减目前只有 API、无页面。详见 [M30-交付报告](M30-交付报告.md)。

| # | 文档依据 | 代码现状 | 判定 |
|---|---|---|---|
| P2-01 | A-M7：FTS5 + jieba 检索（F14.6） | `chunk_fts` 仅建表，全仓无写入/查询；`source` 表零写入 | 未实现（v1 范围，已收缩） |
| P2-02 | A-M7：空白上下文模式（F14.7） | 无实现 | 未实现 |
| P2-03 | A-M6：三异构答案机 + 交叉验证（F14.3） | 单答案机；触发规则③ `_ENABLE_ANSWER_DISAGREEMENT_V0=False` | 未实现（v1） |
| P2-04 | A-M6：建议难度区间 `suggest_band`（O3） | `governance.py` 注释明确"v1，本期待留"，无函数 | 未实现 |
| P2-05 | A-M6：验证状态随时间衰减（F14.1） | `last_verified_at` 只写不读衰减；无衰减计算 | 未实现 |
| P2-06 | A-M6：周盘点一屏批量裁决（F14.8） | 无页面；`ledger.confirm_gap_candidates` 有引擎无路由 | 未实现（引擎就绪） |
| P2-07 | F3.3 输入预览（"本次发给模型的内容"） | **已实现**（M27）：`GET /api/v1/units/{uid}/input-preview?role=`，复用 `assembler.preview` | 已实现 |
| P2-08 | F2.3/F2.4 卡片库、概念图应可写 | `/cards`、`/graph` 只读；无 `POST /cards`、`POST /concept-edges`（仅在设计中） | 只读 |
| P2-09 | C-M0/M7 撤销"可借助审计日志撤销" | `db.undo` 已实现且有测试，但**无 HTTP 端点**（设计有 `POST /audit/{id}/undo`） | 引擎就绪，无入口 |
| P2-10 | 论文/方案：每 3–5 单元重构概念图 | 无机制（无提醒、无周期计算） | 未实现 |
| P2-11 | 方案 §4 进度平面含"产出完成率""整合周期达成率" | `CountsVO` 无这两项（有 `unit_completion`/`gap_clear_ratio`/`l2_gap_backlog`） | 指标口径收缩 |
| P2-12 | 论文风险平面："多模型一致且无人标注不确定" | `compute_counts` 无该信号 | 未实现（M9 报告列为 nice-to-have） |
| P2-13 | 系统设计 §4 `project` 表列（id/goal/initial_belief/created_at/config） | `switch` 把 `last_active_at` 写进 `config`（`routes/projects.py`）；`ConfigVO` 过滤掉它 | 契约外的额外键（有审计留痕用途，可接受，需登记） |
| P2-14 | 方案 §8.2 `undo(expected_prev_hash)` 应为"旧值指纹" | `db.undo` 实际拿**当前行**指纹与 `expected_prev_hash` 比较（与 `change` 快照校验重复），语义与命名不符 | 语义偏差（无实际危害，仅冗余） |
| P2-15 | 系统设计 §4.3 存储清单（库 + backups + exports + logs） | 新增 `graph_layout.json`（M15 坐标持久化）与 `secrets/*.fernet`（M11），未回写系统设计存储清单 | 文档滞后（实现已记 D-M15-1） |

---

## 4. DOC：文档自身错误（15 项）

> **修复状态（2026-10-18 · CHG-05）**：DOC-01 / 02 / 03 / 04 / 05 / 06 / 08 / 09 / 10 / 12（部分）/ 13 / 14 / 15 已修复，5 份基线文件指纹已重算，`verify_baseline.py` 复跑 **8/8 OK / `BASELINE INTACT`**，全量测试 **336 passed**。
> 保留项：**DOC-07**（`bf-02.svg` 未重渲染，需 mermaid CLI，登记为遗留）；**DOC-11**（`tenantId` 不改名——系统设计 §8.2.2 已明写 `tenantId = project_id`，语义无歧义，改名收益低于风险）；**DOC-12 余下**（`$6` 为研究推断的目标值、`$10` 为硬上限，两者非同一概念，保留并已补「目标值/硬上限」区分）。

| # | 位置 | 问题 | 建议修正 |
|---|---|---|---|
| DOC-01 | UserStory §3.2 | 称功能清单"共 28 项"，实际 **40 项**（P0 24 / P1 6 / P2 10），且同节自称"逐行一致" | 改为 40 |
| DOC-02 | 系统设计 §1.2 / §4.1 / 各里程碑报告 | "十六张表" vs "15 表 + FTS5"；而代码是 **14 张业务表 + audit_log = 15 表**，+ FTS = 16 对象；`db.py` 注释"15 业务表"亦差一 | 统一为"14 业务表 + audit_log + chunk_fts = 16 库对象" |
| DOC-03 | UserStory §6.2 vs 部署设计 §3.3.5 | P99 ≤1200ms vs ≤1000ms；分位口径不互补 | 取值并统一 |
| DOC-04 | UserStory §6.3 / 部署设计 | Python 3.11+ / 3.12 / 3.13 三个口径 | 统一为 3.13（venv 实际 3.13.14） |
| DOC-05 | BASELINE §六 | **CHG-03 出现两行**（编号重复，内容互补） | 合并或改 CHG-03/03b |
| DOC-06 | BASELINE §一 | 登记行数与实际差 1：README 56/55、系统设计 3943/3942、UserStory 866/865（`verify_baseline.py` 不校验行数，故未暴露） | 同步行数或改为不登记行数 |
| DOC-07 | CHG-02 §五 | 自陈 `pic/uml/bf-02.svg` 未重渲染，图面标签与已修订正文不一致 | 重渲染 |
| DOC-08 | material_digest §2 | 称"论文未随附、相关引用无法核实"，但评审逐条引用论文 3.2/4.2.1/4.3/7.3，`paper/` 下确有该文 | 补记论文进入时间与来源 |
| DOC-09 | 部署设计 §6.2.5 | RB-03 已改 8 厂商，但同节诊断步骤（603–604 行）仍只列 `api.openai.com` / `api.anthropic.com` | 补全 8 家域名 |
| DOC-10 | 高层架构 / UserStory | 无 ADR 编号、无 QS-xx 质量场景编号，而部署设计反向引用 QS-02/BD-01–03 | 补编号或改引用 |
| DOC-11 | 系统设计 / 部署设计 | `tenantId` 与"无多租户"定位相悖（注为 =project_id） | 改名 `project_id` |
| DOC-12 | 调研/部署 §8 | 预算口径三处：V4 目标 ≤$6、L1 默认 $10、合计 ≤$6–13/月 | 明确"目标/上限/合计"三个词的区别 |
| DOC-13 | UserStory US-4 AC-4.3 | 只断言 concept/attempt，未断言 review 状态；而同段文字已由 CHG-02 改为"保持活跃" | 补 `review.result IS NULL` 断言 |
| DOC-14 | 错误码表 vs `errors.py` | 设计 25 条含 `C404001`；实现 25 条**缺 C404001、多 C400003** | 对齐集合 |
| DOC-15 | 系统设计 §1.2 | 引用"D1 §8.2 十六张表"，而 D1 原文实为 15 表 + FTS（见 DOC-02） | 同步 |

---

## 5. ENV：环境与可复现性（3 项）

| # | 现象 | 证据 | 建议 |
|---|---|---|---|
| ENV-01 | `.venv` 已损坏 | `pyvenv.cfg` 的 `home` 指向 `C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe`（不存在）；执行 `.venv\Scripts\python.exe` 退出码 **103** | 重建 venv（`python -m venv .venv`）或写清"用 vendor python + PYTHONPATH" |
| ENV-02 | README 的测试命令不可用 | README 写 `.venv/Scripts/python -m pytest -q`，实测失败（ENV-01） | 同上 |
| ENV-03 | 系统 python 无依赖 | `D:\soft\anaconda3\python.exe` 3.13.9 无 fastapi/httpx/jinja2 | 明确"必须用 .venv 或 vendor python" |

---

## 6. 建议处置顺序

1. **先补主链**（P0-01/02/03/04）：建单元 → 角色调用 → 记账 → 复测入队。这四项决定"工具是否可用"，其余都是打磨。**方案已出：[PLAN-P0-主链最小闭环.md](PLAN-P0-主链最小闭环.md)**（6 端点 / 5 条决策 / 零 DDL 论证 / 20 例测试 / 0.5–1 人日）。
2. **再修安全红线**（P0-05/06/07/08 + P1-05/06）：要么改代码，要么改文档（记录取舍）。**两者必须选一个**，不能留白。
3. **然后修三个高可见交互缺陷**（P1-08 设置保存 / P1-09 弹层 / P1-10 空态样式），半天可完成。
4. **文档侧一次性清理**：DOC-01/02/05/06/13/14 是"会被下次审计重复发现"的类型，一次改完最省事。
5. **Docker 与 CI 等超配项**：建议在部署设计中显式标注"本项在本地单机形态下不适用"，而不是留着让人误解。

---

_本清单只读对照，不修改任何源码与 `delivery/` 基线。复核基线：`python .workbuddy/delivery/verify_baseline.py` → `BASELINE INTACT`。_
