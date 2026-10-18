# HPSL 剩余开发清单（Roadmap）

> 日期：2026-10-18
> 基线：M0–M27 已交付，全量 **408 passed**；真实 DeepSeek 联调通过（M28）
> 依据：[REF-2 偏差清单](REF-文档与代码偏差清单.md)、[M24](M24-交付报告.md)–[M27](M27-交付报告.md) 交付报告、本次联调实测

---

## 0. 已经不用做的（避免重复投入）

- 主链：建项目 → 建单元 → 产出 → 记录尝试 → 闭环 → 复测入队（M24）
- 角色调用 + 成本记账 + turn 落库 + 会话回放（M24/M26/M28）
- 安全红线：密钥口令/随机 salt/fail-fast、五维限流、Host/Origin 精确匹配、关闭 docs（M25）
- 撤销、账本/审计/成本 REST、角色契约、输入预览（M27）
- 前端收尾：设置保存、配额弹层、空态样式、错误 toast、HTMX 本地化、graph 转义（M24/M27）

---

## 1. P0（阻断/红线）——已清零

暂无。P0-01~08 全部关闭（见 REF-2）。

---

## 2. P1（影响可用性或与设计不一致）

| # | 事项 | 现状 | 为什么需要 | 预估 |
|---|---|---|---|---|
| P1-01 | **补齐 `pricing.yaml`** | 仅 3 个模型有价；`deepseek-flash`、`deepseek-reasoner`、qwen/glm/kimi/doubao 全部 `cost=0` | 预算三态与 M8 成本治理对多数模型失效；本次联调 cost=0 即此因 | 0.2 人日（需官网价格） |
| P1-02 | **验证判分组端点** | 由 `/attempts` + `/close` 组合替代；`self-score`/`confirm-score`/`force-close`/`critic`/`rubric`/`restatement`/`boundary`/`followup` 未拆出 | 设计中的"判定一屏"与 A050001（评分要点作答前不可见）目前只有引擎守卫、没有界面时序 | 1 人日 |
| P1-03 | **`providers/*/chat` 与流式（SSE）** | 无；适配器 `stream=True` 仅占位；`sse_concurrency` 闸无消费方 | F4.2 要求流式输出；流式也是"不让人干等"的核心体验 | 1–1.5 人日 |
| P1-04 | **每日自动快照 + 恢复 CLI** | 只有手动导出、导出即轮转、启动/6h 轮转；无"每日快照" | 冻结项 #7 的 RPO≤24h 目前无载体；恢复流程只有文档 | 0.5 人日 |
| P1-05 | **卡片/概念图写入** | `/cards`、`/graph` 只读；无 `POST /cards`、`POST /concept-edges` | 「整合」是论文六阶段之一，现在用户无法自己建卡片/连线 | 0.5 人日 |
| P1-06 | **turn 纳入 Markdown 导出** | 导出 4 类均不含对话 | 学习过程不可归档；用户换机/复盘缺上下文 | 0.3 人日 |
| P1-07 | **打包与发布** | `pyproject.toml` 的 packages/package-data 已修（M28）；未做 `pipx install` 实测、无依赖锁定 | 当前源码可跑，但 `pip install` 出来的 wheel 可能缺 templates/static 或子包 | 0.3 人日 |
| P1-08 | **LICENSE 文件缺失** | 仓库无 `LICENSE`，`pyproject` 声明 Apache-2.0 | GitHub 上无许可证正文，法律上不完整 | 0.1 人日（贴标准全文） |

---

## 3. P2（v1 能力，设计已排期但未实现）

| # | 事项 | 现状 | 备注 |
|---|---|---|---|
| P2-01 | 知识库：FTS5 + jieba + `source` 挂载 | `chunk_fts` 空表、`source` 零写入 | A-M7 / F14.6 |
| P2-02 | 空白上下文模式 | 未实现 | F14.7；用于检查记忆偏向 |
| P2-03 | 三异构答案机 + 交叉验证 | v0 单答案机，规则③显式关闭 | F14.3；需多 key |
| P2-04 | 挑错多模型并行 + 要点二厂商核对 | 未实现 | F14.4 / O5 / O6 |
| P2-05 | `suggest_band` 难度建议 | 未实现（注释留 v1） | F14.2 / O3 |
| P2-06 | 验证状态衰减 | `last_verified_at` 只写不判定 | F14.1 |
| P2-07 | 周盘点一屏批量裁决 | 引擎有 `confirm_gap_candidates`，无页面 | F14.8 |
| P2-08 | 启动时交互式口令；keyring 实测 | 只读 `HPSL_MASTER_PASSPHRASE`；keyring 未验证 | 设计提到"启动时输入" |
| P2-09 | CSRF 签名 cookie | 仍是随机 token 双提交（`httponly=False`） | 有记录的取舍；签名需自实现 HMAC（无 itsdangerous） |
| P2-10 | `sessions` / `degrade` 端点 | 会话隐式创建；降级走 `move(LEARN→PICK)` | 设计有显式端点 |
| P2-11 | 前端可访问性 | `.tip` 不可聚焦、弹层无 `role=dialog`/ESC/焦点陷阱、导航无 `aria-current` | 与"面向非技术用户"目标相关 |
| P2-12 | graph 节点 `onclick` 内联 id | id 目前为 uuid hex，风险低但仍是字符串拼接 | 可改事件委托（需同步改两条测试断言） |

---

## 4. 工程质量债

| # | 事项 | 说明 |
|---|---|---|
| E-01 | 测试缺口 | M12（UI token）与 M22（8 厂商）无专项文件；`serve()` / WAL checkpoint / FTS 检索 / 前端 JS 执行均无测试 |
| E-02 | 脆弱断言 | 前端里程碑多靠 HTML 字符串/色值/私有符号断言；`test_isolation` 扫源码找 `REPLACE ` |
| E-03 | 环境 | `.venv` 已腐化（base 3.13.12 不存在）；README 的 `pytest` 命令在当前机器不可直接跑 |
| E-04 | 提交 | 本地领先 origin 2–3 个提交，网络恢复后需 `git push`；密钥已在对话中出现，**请轮换** |
| E-05 | 文档 | 三套 M 编号映射已补（REF-1）；delivery 基线已走 CHG-05/06；后续改动仍需 CHG |

---

## 5. 建议的下一批（一个迭代内可完成）

1. **P1-01 价格表** + **P1-06 导出对话**（半天，直接提升成本治理与复盘价值）
2. **P1-02 验证判分组**（1 天，把"判定一屏"做实，A050001 才真正可见）
3. **P1-04 每日快照 + 恢复 CLI**（半天，兑现 RPO 承诺）
4. **P1-03 流式**（1–1.5 天，体验分水岭；同时让 `sse_concurrency` 生效）

完成这四项后，v0 的产品闭环与论文机制基本对齐；P2 属于 v1 范围，可另立里程碑。
