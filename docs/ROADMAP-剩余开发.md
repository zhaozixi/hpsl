# HPSL 剩余开发清单（Roadmap）

> 更新：2026-10-18（M29 之后）
> 基线：M0–M29 已交付，全量 **456 passed**；真实 DeepSeek 非流式 + 流式联调通过
> **P0 与 P1 已全部清零**；本清单现只保留 P2（v1 能力）与工程债。

---

## 0. 已交付（无需再做）

| 范围 | 内容 | 里程碑 |
|---|---|---|
| 数据与主链 | 16 库对象、`db.write` 审计/撤销、建项目/单元/产出/尝试/闭环/复测 | M0/M2/M7/M24 |
| 角色与模型 | 六角色契约与隔离、8 家厂商/3 类协议、角色调用、直连对话、**SSE 流式** | M3/M4/M22/M24/M29 |
| 治理 | 成本三态、四平面 12 指标、软提示、撤销 UI、账本/审计/成本 REST | M8/M9/M27 |
| 安全 | 口令+salt+fail-fast、五维限流、Host/Origin 精确匹配、关 docs、CSRF 双提交 | M25 |
| 体验 | 单元工作台、会话回放、卡片/概念图（含写入）、统计、暗色、HTMX 本地化 | M10–M23/M26/M29 |
| 交付工程 | 每日快照 + 恢复 CLI、对话导出、LICENSE、依赖锁定、打包配置 | M29 |

---

## 1. P0：无

---

## 2. P1：已完成（M29）

| # | 事项 | 落点 |
|---|---|---|
| P1-01 | DeepSeek 官方价 + 未登记价格可见性 | `pricing.yaml`、`cost.unpriced_models`、`/budget` 提示 |
| P1-02 | 验证判分组（自评/确认/要点守卫/强行闭环/挑错） | `routes/judge.py`（三类作答仍走 `/attempts` kind） |
| P1-03 | Provider 直连 + 真 SSE 流式 | `routes/providers.py`、`OpenAICompatAdapter.stream_complete` |
| P1-04 | 每日快照 + 恢复 CLI | `db.daily_snapshot_all` / `restore_project`、`hpsl snapshot/restore` |
| P1-05 | 概念/卡片/概念边写入 + 页面表单 | `routes/graph.py`、`cards.html` |
| P1-06 | 对话记录导出（第 5 类） | `routes/export.py`、`export.html` |
| P1-07 | 依赖与打包 | `requirements.txt`、`tests/test_m29_packaging.py` |
| P1-08 | LICENSE（Apache-2.0） | `LICENSE` |

---

## 3. P2（v1 能力，尚未开始）

| # | 事项 | 现状 | 备注 |
|---|---|---|---|
| P2-01 | 知识库：FTS5 + jieba + `source` 挂载 | `chunk_fts` 空表、`source` 零写入 | A-M7 / F14.6 |
| P2-02 | 空白上下文模式 | 未实现 | F14.7 |
| P2-03 | 三异构答案机 + 交叉验证 | v0 单答案机，规则③显式关闭 | F14.3；需多 key |
| P2-04 | 挑错多模型并行 + 要点二厂商核对 | 未实现 | F14.4 / O5 / O6 |
| P2-05 | `suggest_band` 难度建议 | 未实现 | F14.2 / O3 |
| P2-06 | 验证状态衰减 | `last_verified_at` 只写不判定 | F14.1 |
| P2-07 | 周盘点一屏批量裁决 | 引擎有 `confirm_gap_candidates`，无页面 | F14.8 |
| P2-08 | 启动时交互式口令；keyring 实测 | 只读 `HPSL_MASTER_PASSPHRASE`；keyring 未验证 | — |
| P2-09 | CSRF 签名 cookie | 仍随机 token 双提交 | 有记录的取舍 |
| P2-10 | Anthropic 真流式 | 当前降级为一次性推送 | D-M29-4 |
| P2-11 | 前端可访问性 | `.tip` 不可聚焦、弹层无 dialog 语义/ESC、导航无 `aria-current` | 面向非技术用户 |
| P2-12 | graph 节点内联 `onclick` id | id 目前为 uuid hex，风险低但仍是字符串拼接 | 可改事件委托 |

---

## 4. 工程质量债

| # | 事项 | 说明 |
|---|---|---|
| E-01 | 测试缺口 | M12（UI token）与 M22（8 厂商）无专项文件；`serve()` / WAL checkpoint / FTS 检索 / 前端 JS 执行无测试 |
| E-02 | 脆弱断言 | 前端里程碑多靠 HTML 字符串/色值/私有符号断言；`test_isolation` 扫源码找 `REPLACE ` |
| E-03 | 环境 | `.venv` 已腐化（base 3.13.12 不存在）；当前须用 vendor python + `PYTHONPATH`；README 已给说明 |
| E-04 | 提交 | 本地领先 origin；网络恢复后 `git push`；联调 key 已在会话中出现，**请轮换** |
| E-05 | 文档 | 三套 M 编号映射见 REF-1；delivery 基线变更走 CHG-05/06；后续仍按 CHG 流程 |
| E-06 | 依赖 | DeepSeek 价为高峰价、汇率 7.2 写死；其他厂商价格未登记（会被 `unpriced_models` 标出） |

---

## 5. 建议的下一批（P2 优先级）

1. **P2-01 知识库（FTS5+jieba）**：论文里"知识库/本地文件夹"是 v1 的核心差异化，且 `chunk_fts` 已建表。
2. **P2-03/04 多模型挑错**：把"异构"从警告升级为真实交叉验证（需至少 2 家 key）。
3. **P2-07 周盘点**：引擎已就绪，只缺一屏批量裁决页面，投入小、闭环价值大。
4. **P2-08/09 启动口令与 CSRF 签名**：安全收尾，可与 keyring 实测一起做。
5. **P2-11/12 可访问性与 graph 事件委托**：面向非技术用户的最后一公里。
