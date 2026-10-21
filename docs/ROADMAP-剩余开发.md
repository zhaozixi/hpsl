# HPSL 剩余开发清单（Roadmap）

> 更新：2026-10-21（M40 项目归档/删除 + 浏览器 E2E 之后）
> 基线：M0–M40 已交付；全量 **754 passed / 4 skipped**（其中 1 skipped 为默认关闭的 E2E）
> P0 / P1 / P2（含 C9/C12）已清零；剩余仅 `pipx` 实测与真实凭据项。
> 入口现状：`hpsl` 命令与 `python -m hpsl` 两条路均可用（见 `FIX-启动入口与安装说明.md`）。

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

## 2. 工程债（M36 后）

| # | 事项 | 状态 |
|---|---|---|
| E-01 | M12 / M22 专项测试 | ✅ M31 |
| E-02 | `serve()` / WAL / FTS 边界 | ✅ M31/M36（`_prepare_serve` 已测；WAL/检索边界已测） |
| E-03 | 前端行为测试 | ✅ M40（`tests/e2e/` 浏览器剧本进套件，`HPSL_E2E=1` 开启，默认 skip） |
| E-04 | 环境与依赖 | ✅ M33（C 盘 junction）；`pipx install .` 未实测 |
| E-05 | 知识库页面 | ✅ M31（含增量索引） |
| E-06 | 衰减页面化 | ✅ M31 |
| E-07 | 周盘点增强 | ✅ M31 |
| E-08 | 真实 keyring / 第二厂商 | ◐ 跨进程密钥已测；真实钥匙环/真实第二 key 受环境限制 |
| E-09 | 文档一致性 | ✅ M36（REF-1/REF-2/README/M23 回填 + CHG-07） |
| **C9** | **严格 CSP / 内联 JS 收敛** | ✅ 已完成（M36：nonce CSP + 22 handler 清零 + theme.js + allowEval=false） |
| **C12** | **论文能力落差（最小闭环）** | ✅ 已完成（M36：固定三问落库/回显 + 自动三模型 cross-check）；5 项分析指标仍为后续可选 |

---

## 3. 发布前检查清单

1. `pytest -q` ≥ 754 passed（4 skipped = 3 个无内联脚本的模板 + 1 个默认关闭的 E2E，属预期）；
   - 本机注意：safe-delete hook 会让进程退出码为 1（清理 `pytest-of-*` 临时目录被 fail-closed 拒绝），
     判读以 `--junit-xml` 报表或「进度条无 `F`/`E`」为准，非测试失败；
2. `HPSL_E2E=1 pytest tests/e2e -q` → 1 passed（浏览器真点击，约 25s；缺 node/playwright 自动 skip）；
3. `python .workbuddy/delivery/verify_baseline.py` → `BASELINE INTACT`；
4. `python scripts/check_package.py` → `结果: PASS`（验证 wheel 打全了模板/静态/提示/YAML）；
5. `hpsl --help` 与 `python -m hpsl --help` 均可用（安装步骤必须含 `pip install -e .`，否则只有后者可用）；
6. `python scripts/live_check.py --model deepseek-flash --role clerk --stream` → 退出码 0；
7. 设好 `HPSL_MASTER_PASSPHRASE`，`hpsl serve` 后走一遍：建项目 → 建单元 → 产出 → 尝试 → 闭环 → 复测 → 周盘点 → 撤销；
8. 轮换测试用 API key；确认仓库无 `.fernet`/`*.db`/key。

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

---

## 7. M38 变更摘要（2026-10-19，密钥入口与双币）

| 类别 | 内容 |
|---|---|
| 独立密钥入口 | 新增 `/keys`（导航「设置」右侧），不依赖激活项目；厂商状态 / 保存 / 测试连接；锁定态解释原因并禁用保存；完整密钥永不进 HTML |
| 双币展示 | 记账仍美元（`cost_usd` / `pricing.yaml` / 预算判断全不动），人民币按项目配置 `usd_cny_rate`（默认 7.2）只做展示折算；预算表、超限弹层、统计、项目详情、角色回执统一 `$x（约 ¥y）` |
| 工程 | 新增 `common/money.py` 与 `templating.make_templates()`（注册 usd/cny/both/rate 过滤器），12 个路由模块统一走工厂 |
| 验证 | 715 passed / 3 skipped；浏览器 9 项 PASS、0 console error、375px 无溢出 |

---

## 8. M39 变更摘要（2026-10-19，数据与配置能力）

| 类别 | 内容 |
|---|---|
| 密钥清除 | `secrets.delete_api_key` + `DELETE /api/v1/settings/keys/{provider}`；**删除不解密**（锁定态也能撤销填错的密钥）；遍历 keyring + 文件两个后端；幂等 |
| 快照删除 | `DELETE /api/v1/backup/{name}`，复用 basename + `db.list_backups` 白名单；每行加「删除」+ 二次确认 |
| 恢复反馈 | 去掉 `HX-Refresh`，改结果片段 + OOB 列表/计数（提示不再被整页刷新冲掉） |
| 角色绑定模型 | `ConfigVO.role_models`（JSON 键，零 DDL）+ `registry.resolve_role_model`；设置页六角色下拉 + 「实际会用什么」列；指定模型无密钥时**报错不静默降级** |
| 交叉验证预检 | `multi.py::_ensure_keys` 在创建 session 前预检；自动模式只挑已配密钥的厂商；不足 2 家区分「0 家 / 只有 1 家」并写明未产生费用 |
| 更正 | 「快照无限堆积」判断有误——`rotate_backups`（日 7 + 月 12）与 `cleanup_pre_restore`（留 3）一直存在；本批只补「手动删单份」 |
| 验证 | 739 passed / 3 skipped（新增 24 例）；BASELINE INTACT；打包 PASS；浏览器 12 项 PASS、0 console error |
| 未做 | 项目归档/删除、三问进度量、浏览器 E2E 进套件、导出双币（对账口径，有意不做） |
| 后续销账 | 项目归档/删除与浏览器 E2E **已在 M40 完成**；剩余：三问进度量、导出双币（仍有意不做） |

---

## 9. M40 变更摘要（2026-10-21，项目归档/删除 + 浏览器 E2E）

| 类别 | 内容 |
|---|---|
| 归档 | `projects.set_archived`：写 `config.archived_at`（零 DDL）；**幂等**（重复归档不刷新时间）；归档 active 项目则清空 active 句柄；`restore_last_active` 跳过归档项 |
| 列表口径 | `list_projects(include_archived=True)` **默认含归档**（备份/轮转 CLI 依赖，归档不能等于丢备份）；页面显式传 False，拆「活跃列表 + 已归档折叠区」 |
| 删除 | 要求 `confirm_name` 逐字等于项目名；**不 rmtree**，整目录 `rename` 到 `HPSL_HOME/trash/{pid}-{UTC}`（原子、可恢复）；先 `close_project` 释放 Windows 文件占用 |
| 页面 | 抽 `_project_panel.html`（整页与 HTMX 片段共用，防口径漂移）；错误返回**完整面板 + `.banner.err`**，不用错误片段顶掉列表；详情页操作走 `HX-Redirect` |
| 顺带修复 | `_read_project_row` 先判库文件存在，不存在 → `A010001`（此前 sqlite 抛 `unable to open database file` 冒成 500） |
| E2E | `tests/e2e/`：子进程起真实服务（避免 autouse `isolated_home` 把同进程服务带偏）+ Playwright 剧本；`HPSL_E2E=1` 开启、默认 skip；缺 node/playwright 自动 skip；剧本 15 步 |
| 验证 | 754 passed / 4 skipped（新增 14 例）；E2E 1 passed / 23s；BASELINE INTACT；打包 PASS（108 条目） |
| 未做 | trash 不自动清理（何时算"真不要了"只有用户知道）；界面化回收站；三问进度量 |
