# HPSL 剩余开发清单（Roadmap）

> 更新：2026-10-18（M30 之后）
> 基线：M0–M30 已交付，全量 **515 passed**；真实 DeepSeek 非流式 + 流式通过
> **P0 / P1 / P2 均已清零**；本清单只剩工程债与少量"页面化/实测"补强。

---

## 0. 已交付

| 范围 | 内容 |
|---|---|
| 数据与主链 | 16 库对象、`db.write` 审计/撤销、建项目/单元/产出/尝试/闭环/复测、会话回放 |
| 角色与模型 | 六角色隔离、8 家厂商/3 类协议、角色调用、Provider 直连、SSE 流式（OpenAI 兼容） |
| 治理 | 成本三态与价格表、四平面 12 指标、软提示、难度建议、验证衰减、周盘点、账本/审计/成本 REST |
| 安全 | 口令+salt+fail-fast、五维限流、Host/Origin 精确匹配、关 docs、CSRF 签名、启动口令提示 |
| 知识库 | `source` 挂载、FTS5 索引与检索（jieba 可选）、空白上下文 |
| 多模型 | 交叉验证、多挑错、评分要点二厂商核对 |
| 体验 | 单元工作台、卡片/概念图（含写入）、统计、暗色、HTMX 本地化、可访问性、graph 委托 |
| 交付工程 | 每日快照 + 恢复 CLI、对话导出、LICENSE、依赖锁定、打包配置、`live_check.py` |

---

## 1. P0 / P1 / P2：无待办

- P0（主链/红线）：M24/M25 清零。
- P1（可用性/设计对齐）：M29 清零。
- P2（v1 能力）：M30 清零。已知取舍：jieba 未安装（bigram 兜底）、keyring 仅 fake 验证、Anthropic 流式无真实 key 验证。

---

## 2. 工程债（建议下一批）

| # | 事项 | 说明 | 预估 |
|---|---|---|---|
| E-01 | M12 / M22 专项测试 | UI token 体系、8 厂商 `kind` 路由与 `models.yaml` 解析无专项文件 | 0.5 人日 |
| E-02 | `serve()` / WAL checkpoint / FTS 边界 | 目前 `serve()` 仅被 CLI 测试间接覆盖；FTS 特殊字符、超长查询未测 | 0.5 人日 |
| E-03 | 前端行为测试 | 当前多为 HTML 字符串断言；可引入轻量浏览器测试（或至少 JS 语法 + 关键 DOM 结构） | 1 人日 |
| E-04 | 环境与依赖 | `.venv` 已腐化；用 `requirements.txt` 重建并跑 `pipx install .` 实测 | 0.3 人日 |
| E-05 | 知识库页面化 | 目前仅 API：索引目录、检索结果页（"未检索到"提示） | 0.5 人日 |
| E-06 | 衰减页面化 | `/concepts/decay` 结果未进卡片/概念图界面 | 0.3 人日 |
| E-07 | 周盘点增强 | 目前只改状态；可加"分配到下周/标记补前置" | 0.3 人日 |
| E-08 | 真实 keyring / Anthropic key | keyring 仅 fake；Anthropic 流式无真实联调 | 视凭据 |
| E-09 | 文档一致性 | REF-2 仍有少量历史行描述旧状态；delivery 基线按 CHG 流程维护 | 0.3 人日 |

---

## 3. 发布前检查清单

1. `pytest -q` ≥ 515 passed；
2. `python .workbuddy/delivery/verify_baseline.py` → `BASELINE INTACT`；
3. `python scripts/live_check.py --model deepseek-flash --role clerk --stream` → 退出码 0；
4. 设好 `HPSL_MASTER_PASSPHRASE`，`hpsl serve` 后走一遍：建项目 → 建单元 → 产出 → 尝试 → 闭环 → 复测 → 周盘点 → 撤销；
5. 轮换测试用 API key；确认仓库无 `.fernet`/`*.db`/key。
