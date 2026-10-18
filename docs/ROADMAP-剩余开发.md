# HPSL 剩余开发清单（Roadmap）

> 更新：2026-10-18（M33 收尾批之后）
> 基线：M0–M32 已交付 + M33 收尾批；全量 **576 passed / 3 skipped**
> **P0 / P1 / P2 均已清零**；工程债 E-01～E-09 全部闭合或标注为环境受限。

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

## 2. 工程债（M31 后）

| # | 事项 | 状态 |
|---|---|---|
| E-01 | M12 / M22 专项测试 | ✅ 已完成（M31：token/模板继承 + 厂商矩阵/工厂/异构） |
| E-02 | `serve()` / WAL checkpoint / FTS 边界 | ✅ 部分完成（M31：checkpoint/close_all/检索边界；`serve()` 仍靠 CLI 间接覆盖） |
| E-03 | 前端行为测试 | ✅ 已完成（M33：`tests/test_m33_frontend.py` —— 抽出模板内联 `<script>` 交 `node --check` 真语法校验、渲染后校验注入 JSON 可解析、`innerHTML`/`tojson`/本地 HTMX 结构不变量；无 Node 时显式 skip，不伪装通过） |
| E-04 | 环境与依赖 | ✅ 已完成（M33：`.venv` 重建为「系统盘 venv + 目录联接」，绕开 vendor python 作 base 的 venvlauncher 故障；`scripts/check_package.py` 离线构建 wheel 自检 PASS：98 条目 / 24 模板 / 6 提示 / 4 静态 / 2 YAML） |
| E-05 | 知识库页面 | ✅ 已完成（M31：`/knowledge` 挂载/检索/删除 + 导航；**增量索引**按 mtime/size，`force` 可全量） |
| E-06 | 衰减页面化 | ✅ 已完成（M31：卡片页显示衰减后状态与天数） |
| E-07 | 周盘点增强 | ✅ 已完成（M31：推迟到下周 / 前置说明 / 取消推迟 / 一次提交，存 `gap.context`，零 DDL） |
| E-08 | 真实 keyring / Anthropic key | ◐ 部分完成（M33：`tests/test_m33_keystore_e2e.py` 用子进程做**跨进程**密钥往返、密文 `HPSL2` 头与无明文断言、错口令显式失败、无口令拒绝写入、keyring 链路"有则用/无则明确回落"；真实钥匙环与真实 Anthropic key 仍受环境限制，且当前注册表已不启用国外厂商） |
| E-09 | 文档一致性 | REF-2 保留历史描述行；delivery 按 CHG 维护 |

---

## 3. 发布前检查清单

1. `pytest -q` ≥ 576 passed（3 skipped 为无内联脚本的模板，属预期）；
2. `python .workbuddy/delivery/verify_baseline.py` → `BASELINE INTACT`；
3. `python scripts/check_package.py` → `结果: PASS`（验证 wheel 打全了模板/静态/提示/YAML）；
4. `python scripts/live_check.py --model deepseek-flash --role clerk --stream` → 退出码 0；
5. 设好 `HPSL_MASTER_PASSPHRASE`，`hpsl serve` 后走一遍：建项目 → 建单元 → 产出 → 尝试 → 闭环 → 复测 → 周盘点 → 撤销；
6. 轮换测试用 API key；确认仓库无 `.fernet`/`*.db`/key。

---

## 4. M33 变更摘要（2026-10-18）

| 类别 | 内容 |
|---|---|
| 版本与文档 | `pyproject` 0.0.0 → **0.1.0**（description/依赖/可选 extras 补齐）；README 头部与能力清单对齐 M1–M32 |
| 启用范围 | `models.yaml` **只保留 DeepSeek / 千问 / 智谱 GLM**（统一 `openai_compat`）；OpenAI/Anthropic/Gemini/Moonshot/豆包 移除，适配器代码保留未启用 |
| 价格表 | 11 个在用模型全部登记（含 GLM-4-Plus / GLM-4-Flash 免费额度记 0），换算口径与来源写入文件头 |
| 中文化 | 状态按钮「推进到「验证中」」、残留实质错误→是/否徽标、强制闭环→徽标、审计对象/操作者→中文、知识源类型→中文 |
| 备份页 | 导出改为「结果片段 + OOB 同步列表与计数」，不再整页刷新（原实现会吞掉成功提示） |
| 测试 | 新增 `test_m33_frontend.py`（12 例）与 `test_m33_keystore_e2e.py`（5 例）；因启用范围收敛调整 8 个既有测试文件的模型常量 |
