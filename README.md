# HPSL · 本地个人学习管家

HPSL（项目制学习框架工具，Apache-2.0）是一个**本地运行的个人学习管家**：把学习目标拆成单元，按「学 → 练 → 考 → 复习」带流程推进，并记账（AI 花费）、记卡片、画概念图，用治理度量呈现学习进展。当前为 v0.1，已推进至 **M0–M40**。

## 技术栈（实现期冻结）

- **FastAPI + HTMX + Jinja2 SSR**（`app.py` 为完整 FastAPI 入口，`hpsl serve` 启动；前端即 `templates/` + `static/`，无独立前端构建步骤）
- **SQLite 3.45+（WAL）+ 手写 SQL，不引入任何 ORM**
- 写纪律：每项目**单一写连接** + `busy_timeout=5000ms` + 每连接 `foreign_keys=ON`
- **禁用 REPLACE**：写路径只用 UPDATE/INSERT，REPLACE 会触发外键删除语义且不进审计
- 审计：`db.write(table, row, actor)` 是账本写入**唯一入口**，同事务追加 `audit_log`
- 模型接入：httpx 直连，**当前仅接入 3 家国产厂商**（DeepSeek / 通义千问 / 智谱 GLM），统一走 `openai_compat` 协议；`anthropic` / `gemini` 适配器代码保留但**未在注册表启用**（`models.yaml` 已移除对应 provider，向导与设置页不会出现）

## 工程结构

```
hpsl/
├── app.py              # FastAPI 入口：hpsl serve（uvicorn 单 worker，默认 127.0.0.1:8765）
├── config.py           # HPSL_HOME / 端口 / ConfigVO 默认值
├── db.py               # 连接工厂、幂等迁移、审计写入（db.write 唯一入口）、撤销
├── projects.py         # 项目创建/列出/切换/配置合并
├── common/             # dto / result / enums / errors（不依赖业务模块）
├── models/             # 模型适配层：base / openai_compat / anthropic / gemini / connectivity / registry + models.yaml / pricing.yaml（仅启用 DeepSeek/Qwen/GLM）
├── engine/             # 无界面引擎（headless）：state / grading / claim_check / review_queue / ledger / governance / cost / role_llm
├── roles/              # 六角色契约（contracts / assembler / prompts）
├── secrets/            # 密钥保险库（keystore：keyring 优先，Fernet+Argon2id 降级）
├── routes/             # Web 接入：wizard / unit / review / graph / stats / quota / settings_keys / backup / export / projects / ledger / governance / security
├── templates/          # Jinja2 页面与片段（含 macros.html 中文翻译宏）
└── static/             # app.css / app.js / help.html（站内操作手册）
```

## 安装与运行

```bash
# 创建隔离环境（Python ≥3.11；本机实测 3.13.14）
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -r requirements.txt
pip install -e .              # 生成 hpsl 命令（不装则只能用 python -m hpsl）
pip install pytest            # 仅跑测试时需要

# 必需：设置密钥库口令（不落盘；丢失则已存 API key 不可恢复）
export HPSL_MASTER_PASSPHRASE="换成一个足够长的口令"      # bash
# $env:HPSL_MASTER_PASSPHRASE = "..."                     # PowerShell

# 启动本地服务（默认 127.0.0.1:8765；端口可经 --port 或环境变量 HPSL_PORT 覆盖）
hpsl serve
# 等价写法：python -m hpsl serve（未装 `pip install -e .`、或 venv 重建后都可用）
# 浏览器打开 http://127.0.0.1:8765

# 启动时默认「恢复到上次所在项目」（读各项目 config 里的 last_active_at，只读不写）。
# 想要每次从「未选择项目」开始，用任一方式关掉：
hpsl serve --no-restore
# export HPSL_NO_RESTORE_ACTIVE=1                       # bash
# $env:HPSL_NO_RESTORE_ACTIVE = "1"                     # PowerShell

# 运行全量测试（M0–M40：754 例通过 + 4 例跳过，含零 DDL 守护）
.venv/Scripts/python.exe -m pytest -q

# 浏览器 E2E（默认不跑：真起服务 + 真开 Chromium，约 25 秒；缺 node/playwright 会自动 skip）
HPSL_E2E=1 .venv/Scripts/python.exe -m pytest tests/e2e -q
```

> **本机 venv 说明**：若项目位于非系统盘（如 `D:`），在项目目录内建 venv 会让
> SQLite 写入慢约 10 倍（实测单文件测试 2.6s → 19.3s）。本机因此把 venv 实际建在
> 系统盘，再用目录联接挂回项目：
> ```powershell
> python -m venv C:\path\to\envs\hpsl
> New-Item -ItemType Junction -Path .venv -Target C:\path\to\envs\hpsl
> ```
> 这样 `.venv/Scripts/python.exe` 照常可用，速度与系统盘一致。

> 若系统安装了 `keyring`（macOS/Windows 凭据管理器），密钥会优先存入系统钥匙环，
> 此时可不设 `HPSL_MASTER_PASSPHRASE`；否则文件保险库要求口令，缺失会明确报错（A040003）。
> `/docs`、`/redoc`、`/openapi.json` 已关闭（安全设计 §4.2.2）。

### 真实模型自检（可选）

```bash
# 密钥只从环境变量读取，不写入仓库；临时 HPSL_HOME 用完即删
export DEEPSEEK_API_KEY="sk-..."
python scripts/live_check.py --model deepseek-flash --role clerk
```

脚本会真实完成：建项目 → 存密钥 → 建单元 → 角色调用 → 记账 → 对话落库，并打印 tokens/花费/计数；
退出码 0 表示全链路通过。

## 关键约束（实现期不可变更）

- 路径：`HPSL_HOME/projects/{pid}/project.db`，项目目录权限 0700，每项目独立库物理隔离。
- 16 个库对象：14 张业务表 + `audit_log`（共 15 张表）+ `chunk_fts` FTS5 虚表；`user_version=1`，迁移幂等。
- `audit_log` **不在写白名单**，业务代码不得直写；`undo` 撤销动作本身也留痕。
- 时间字段统一 ISO-8601 UTC 文本；JSON 字段 `json.dumps(ensure_ascii=False)` 存 TEXT。
- 安全：本地单机单用户、回环监听；Host/Origin **精确匹配**（含端口）+ 写请求 CSRF 双提交 + 五维限流（process/route/llm_freq/sse_concurrency/write_freq）；无账号体系、无多租户；`/docs` 等文档端点关闭。
- 密钥：keyring 优先；否则 `HPSL_MASTER_PASSPHRASE` + Argon2id(64MiB/3/4) + **每文件随机 salt**，无口令即拒绝（不降级明文），口令不落盘、不入日志/导出。

## 当前能力（M0–M40）

- 多项目数据底座与审计（M0）
- 单元八步状态机（INIT → … → CLOSED）+ 复测队列（2/7/21 天）（M2 / M6）
- 六角色契约与隔离（M3）
- 验证三件套 + 判分协议与闭环判据（M3–M5）
- 挑错者与异构策略（M5）
- 模型接入与密钥：3 家国产厂商（DeepSeek / 千问 / GLM）、密钥静态加密持久化、连通性自检（M4 / M11 / M22）
- 成本治理与预算三态（ok / warn / blocked，默认 $10/月）（M8）
- 治理度量四平面 + 软提示（M9 / M10）
- Web 接入与本地安全（M1 / M10）
- 向导、卡片、概念图（含拖拽 / 自动布局 / 分层布局）、学习分析仪表盘、时间序列区间选择、概念验证时间线（M11 / M13 / M15 / M16 / M17 / M19–M21）
- 备份自动轮转、Markdown 导出（脱敏）、暗色模式（M10B / M11 / M13 / M14）
- 易用性改造：界面术语中文化、状态/花费彩色徽标、悬浮解释、站内操作手册（M23）
- 主链接线：建单元 / 提交产出 / 记录尝试 / 闭环并自动入队复测 + 六角色调用与成本记账（M24 P0-A）
- 页面主链：新建单元、产出/尝试/闭环表单、AI 辅助角色面板、错误 toast（M24 P0-B）
- 会话回放：角色调用写入 `turn`，单元详情按时间展示对话（M26）
- 运维接口：账本/审计/成本 REST + 一键撤销 + 角色契约与输入预览（M27）
- 离线可用：HTMX 1.9.12 本地内置（`static/htmx.min.js`，M27）
- 成本与判定：DeepSeek 官方价表、未登记价格提示、自评/确认得分/要点守卫/强行闭环/挑错端点（M29）
- 直连与流式：`/providers/{name}/chat` 与 SSE `/chat/stream`（M29）
- 备份与发布：每日快照 + `hpsl restore`、对话记录导出、Apache-2.0 LICENSE、requirements 锁定（M29）
- 知识库：`source` 挂载 + FTS5 检索（jieba 可选）、空白上下文模式（M30）
- 多模型：交叉验证、多挑错、评分要点二厂商核对（M30）
- 治理增强：难度建议、验证衰减、周盘点批量裁决（M30）
- 平台：CSRF 签名、启动口令提示、Anthropic SSE 流式、可访问性与 graph 事件委托（M30）
- 知识库页面与衰减展示：`/knowledge` 挂载/检索/删除；卡片页显示衰减后状态（M31）
- 新模型：智谱 GLM-5.3-Flash、千问 Qwen3.8-Flash（含图片 content 数组透传，M32）
- 收尾与工程债：版本 0.1.0、启用范围收敛为国内三家、11 个在用模型全部定价、界面术语中文化、备份页 OOB 局部刷新、前端脚本 `node --check` 真校验、密钥跨进程链路验证、打包自检脚本（M33）
- 浏览器实测修复：项目切换入口、HTMX 错误片段化（裸 JSON 不再顶掉面板）、闭环区随状态同步、概念图空态、向导横幅/文案就地刷新、宏空白控制（M34）
- 启动体验：恢复上次所在项目（只读 `last_active_at`，`--no-restore` 可关）、顶栏常驻「当前项目」、错误提示分层（中文结论 + 「技术细节」折叠）、建单元列表就地更新、向导原生表单校验、详情页配置改中文表格（M35）
- 回归修复（行为级）：预算闸门真实拦截（角色/直连调用前校验，超限 A080001）、页面可强行闭环并留理由、复测正常到期可进 21 天档、`serve --port` 与 Host 白名单对齐、概念图空态与布局后连线重算（M36 P0）
- 错误与交互收口：REST 保留自定义错误文案、`HX-Refresh` 改局部刷新、未捕获异常统一兜底、布尔表单解析、撤销指纹口径（M36 P1）
- 基线与契约收口：CHG-05/CHG-07 补登记（3 家厂商 / 66 端点 / A080002）、新增 `tests/test_contracts.py` 补齐七类测试守护、REF-1/REF-2/README/操作手册数字回填（M36 P2）
- 文案纠偏：无密钥报错统一指路「密钥」页（原指向不存在的设置区块），删除 M33 已移除厂商的残留说明（M37）
- 独立密钥入口：`/keys` 不依赖激活项目即可管理三家厂商密钥（保存 / 测试连接 / 尾号回显 / 锁定态解释）；**费用双币展示**——记账仍为美元（对账口径不动），人民币按项目配置 `usd_cny_rate` 仅做展示折算，预算表 / 超限弹层 / 统计 / 项目详情 / 角色回执统一 `$x（约 ¥y）`（M38）
- 数据与配置能力：密钥可清除（不解密，锁定态也能撤销填错的 key）、快照可单份删除（白名单校验 + 二次确认）、六角色可绑定具体模型（无密钥时报错不静默降级）、交叉验证前预检厂商密钥（不足两家区分 0 家 / 仅 1 家并说明未产生费用）（M39）
- 项目归档与删除：归档写 `config.archived_at`（零 DDL，幂等、不影响备份与统计、启动恢复跳过）收进列表折叠区；删除要求逐字输入项目名，整目录 rename 到 `HPSL_HOME/trash/`（可恢复，不做不可逆销毁）（M40）
- 浏览器 E2E 进套件：`tests/e2e/` 用子进程起真实服务 + Playwright 剧本，`HPSL_E2E=1` 开启、默认 skip；覆盖新建 → 归档 → 取消归档 → 错误名删除 → 正名删除、零 console error、375px 无溢出（M40）
