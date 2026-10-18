# HPSL · 本地个人学习管家

HPSL（项目制学习框架工具，Apache-2.0）是一个**本地运行的个人学习管家**：把学习目标拆成单元，按「学 → 练 → 考 → 复习」带流程推进，并记账（AI 花费）、记卡片、画概念图，用治理度量呈现学习进展。当前为 v0，已推进至 **M1–M23**。

## 技术栈（实现期冻结）

- **FastAPI + HTMX + Jinja2 SSR**（`app.py` 为完整 FastAPI 入口，`hpsl serve` 启动；前端即 `templates/` + `static/`，无独立前端构建步骤）
- **SQLite 3.45+（WAL）+ 手写 SQL，不引入任何 ORM**
- 写纪律：每项目**单一写连接** + `busy_timeout=5000ms` + 每连接 `foreign_keys=ON`
- **禁用 REPLACE**：写路径只用 UPDATE/INSERT，REPLACE 会触发外键删除语义且不进审计
- 审计：`db.write(table, row, actor)` 是账本写入**唯一入口**，同事务追加 `audit_log`
- 模型接入：httpx 直连，支持 **8 家厂商**（OpenAI / Anthropic / Gemini / DeepSeek / 通义千问 / 智谱 GLM / Moonshot(Kimi) / 豆包），接入协议 3 类（`openai_compat` / `anthropic` / `gemini`）；由 M22 从初始双 Provider 扩展而来

## 工程结构

```
hpsl/
├── app.py              # FastAPI 入口：hpsl serve（uvicorn 单 worker，默认 127.0.0.1:8765）
├── config.py           # HPSL_HOME / 端口 / ConfigVO 默认值
├── db.py               # 连接工厂、幂等迁移、审计写入（db.write 唯一入口）、撤销
├── projects.py         # 项目创建/列出/切换/配置合并
├── common/             # dto / result / enums / errors（不依赖业务模块）
├── models/             # 模型适配层：base / openai_compat / anthropic / gemini / connectivity / registry + models.yaml / pricing.yaml
├── engine/             # 无界面引擎（headless）：state / grading / claim_check / review_queue / ledger / governance / cost / role_llm
├── roles/              # 六角色契约（contracts / assembler / prompts）
├── secrets/            # 密钥保险库（keystore：keyring 优先，Fernet+Argon2id 降级）
├── routes/             # Web 接入：wizard / unit / review / graph / stats / quota / settings_keys / backup / export / projects / ledger / governance / security
├── templates/          # Jinja2 页面与片段（含 macros.html 中文翻译宏）
└── static/             # app.css / app.js / help.html（站内操作手册）
```

## 安装与运行

```bash
# 创建隔离环境（托管 Python 3.13）
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -e ".[test]"

# 必需：设置密钥库口令（不落盘；丢失则已存 API key 不可恢复）
export HPSL_MASTER_PASSPHRASE="换成一个足够长的口令"      # bash
# $env:HPSL_MASTER_PASSPHRASE = "..."                     # PowerShell

# 启动本地服务（默认 127.0.0.1:8765；端口可经 --port 或环境变量 HPSL_PORT 覆盖）
hpsl serve
# 浏览器打开 http://127.0.0.1:8765

# 运行全量测试（M0–M27，约 408 例，含零 DDL 守护）
pytest -q
```

> 若系统安装了 `keyring`（macOS/Windows 凭据管理器），密钥会优先存入系统钥匙环，
> 此时可不设 `HPSL_MASTER_PASSPHRASE`；否则文件保险库要求口令，缺失会明确报错（A040003）。
> `/docs`、`/redoc`、`/openapi.json` 已关闭（安全设计 §4.2.2）。

## 关键约束（实现期不可变更）

- 路径：`HPSL_HOME/projects/{pid}/project.db`，项目目录权限 0700，每项目独立库物理隔离。
- 16 个库对象：14 张业务表 + `audit_log`（共 15 张表）+ `chunk_fts` FTS5 虚表；`user_version=1`，迁移幂等。
- `audit_log` **不在写白名单**，业务代码不得直写；`undo` 撤销动作本身也留痕。
- 时间字段统一 ISO-8601 UTC 文本；JSON 字段 `json.dumps(ensure_ascii=False)` 存 TEXT。
- 安全：本地单机单用户、回环监听；Host/Origin **精确匹配**（含端口）+ 写请求 CSRF 双提交 + 五维限流（process/route/llm_freq/sse_concurrency/write_freq）；无账号体系、无多租户；`/docs` 等文档端点关闭。
- 密钥：keyring 优先；否则 `HPSL_MASTER_PASSPHRASE` + Argon2id(64MiB/3/4) + **每文件随机 salt**，无口令即拒绝（不降级明文），口令不落盘、不入日志/导出。

## 当前能力（M1–M23）

- 多项目数据底座与审计（M0）
- 单元八步状态机（INIT → … → CLOSED）+ 复测队列（2/7/21 天）（M2 / M6）
- 六角色契约与隔离（M3）
- 验证三件套 + 判分协议与闭环判据（M3–M5）
- 挑错者与异构策略（M5）
- 模型接入与密钥：8 家厂商、密钥静态加密持久化、连通性自检（M4 / M11 / M22）
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
