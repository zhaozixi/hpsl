# HPSL 剩余开发清单（Roadmap）

> 更新：2026-10-18（M35 启动体验批之后）
> 基线：M0–M32 已交付 + M33 收尾批 + M34 浏览器实测批 + M35 启动体验批；全量 **594 passed / 3 skipped**
> **P0 / P1 / P2 均已清零**；工程债 E-01～E-09 全部闭合或标注为环境受限。

---

## 0. 已交付

| 范围 | 内容 |
|---|---|
| 数据与主链 | 16 库对象、`db.write` 审计/撤销、建项目/单元/产出/尝试/闭环/复测、会话回放 |
| 角色与模型 | 六角色隔离、3 家国内厂商（DeepSeek / 千问 / GLM）/3 类协议、角色调用、Provider 直连、SSE 流式（OpenAI 兼容） |
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

1. `pytest -q` ≥ 594 passed（3 skipped 为无内联脚本的模板，属预期）；
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

---

## 5. M34 变更摘要（2026-10-18，浏览器实测）

| 类别 | 内容 |
|---|---|
| 致命 | 全站无项目切换入口（`_active_pid` 为进程内句柄，重启后项目失联）→ 首页/详情页补「切换到此项目」 |
| 严重 | HTMX 报错返回裸 JSON，`outerHTML` 目标锚点被顶掉 → BizError 按 `HX-Request` 分流为 HTML 片段（固定 200） |
| 严重 | 闭环区与状态脱节，推进到 JUDGE 后表单不出现 → 抽 `_unit_close_panel.html` + OOB 同步，表单常驻按状态禁用 |
| 体验 | 概念图空态 `svg` 为 null 抛异常、向导横幅不刷新、密钥结果泄露字段名、宏尾换行多空格、元信息残留 `1`/`0` 与 UTC ISO |
| 验证 | 576 passed / 3 skipped；13 页全 200、控制台零 error；状态机七态全程通过；过程中 2 例回归已修且未改动既有断言 |

---

## 6. M35 变更摘要（2026-10-18，启动体验）

| 类别 | 内容 |
|---|---|
| 启动恢复 | `projects.restore_last_active()`：读各项目 `config.last_active_at` 取最新者，**只读不写**、零 DDL、不引入第二个可写状态源；只在 `serve()` 显式调用（绝不 import 期执行，避免污染测试隔离） |
| 可关 | `hpsl serve --no-restore` 与 `HPSL_NO_RESTORE_ACTIVE=1`；启动日志明确打印「已恢复 / 已跳过 / 无历史记录」三种结果 |
| 可见 | `base.html` 顶栏常驻「当前项目：X」（中间件注入 `request.state.active_label`，只读进程内缓存零额外查库）；把 34 处依赖的隐式 active 上下文摆到台面上 |
| 错误分层 | `BizError` 增 `detail` 字段：`msg` 只留中文结论，上游英文原文进界面 `<details>技术细节</details>`；JSON 契约仍是四字段不变。覆盖 401/403/404/400/422 与 429/5xx、网络异常 |
| 建单元 | 去掉 `HX-Refresh` 整页刷新 → 结果区提示 + 列表区 OOB 就地更新（提示不再被冲掉） |
| 向导 | 步骤 1/2 输入纳入真 `<form>`，`required` 原生校验生效（JS 仅作 trim 兜底） |
| 详情页 | `<pre>{{ config }}</pre>` 开发态 dump → 9 行中文配置表 + 「查看原始配置（排障用）」折叠 |
| 测试 | 新增 `tests/test_m35_restore_and_ux.py`（18 例）；`test_m24_web` 建单元用例改断局部刷新；`test_m32_providers` 改断 `detail` 承载上游原文 |
| 验证 | **594 passed / 3 skipped**；BASELINE INTACT；浏览器复验 5/5 通过、13 页零 JS 错误 |
