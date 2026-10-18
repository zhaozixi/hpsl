# PLAN-P0 · 主链最小闭环改造方案

> 文档性质：改造方案（待实施；实施后转为 `docs/M24-*` 里程碑文档）
> 编写日期：2026-10-18
> 依据：[REF-2 偏差清单](REF-文档与代码偏差清单.md) 的 P0-01 ~ P0-04（主链断点）
> 约束：**零 DDL**（16 库对象不变）、全部写入经 `db.write`、隔离不破、不新增错误码、engine 保持 headless

---

## 0. 一句话目标

让一个新用户能在浏览器里走完：**建项目 → 建单元 → 提交产出 → 记录重述/边界题得分 → 闭环 → 到期复测**，并且期间**真实调用过 LLM 且被记账**。

验收（DoD）：

1. `POST /api/v1/units` 可建单元，`/unit` 列表出现该单元；
2. `POST /units/{uid}/production` 写产出后，`/units/{uid}/move` 能进入 VERIFY（无产出时被 A020003 拒绝）；
3. `POST /units/{uid}/attempts` 写入重述/边界题得分；
4. `POST /units/{uid}/close` 达标自动闭环，并**自动入队一条 review**（2 天后到期）；
5. `POST /roles/{role}/call` 返回模型文本，`llm_call` 新增一行，`/budget` 能看到花费；
6. 全量测试 ≥ 356 passed（336 + 新增 ~20），`count_user_objects == 16` 不变。

---

## 1. 为什么现在是断的（复述证据）

| 断点 | 证据 |
|---|---|
| 无建单元 | 31 个 API 无 `POST /units`；全仓无 `INSERT INTO unit` |
| 无产出录入 | 只有 `POST /units/{uid}/move`，`production` 字段无写入路径 |
| 无角色调用 | `/roles/*` 端点不存在；`RoleLLM` 仅测试引用 |
| 无记账 | `cost.record_call` 无生产调用方；`llm_call` 空表 |
| 复测空转 | 建 `review` 行的只有 `enqueue_reviews`，仅测试调用 |
| 无会话 | `session` 表只被 `ledger.confirm_gap_candidates` 更新 `gaps_confirmed`，从未创建行 |

---

## 2. 设计决策（先定这五条，避免实现摇摆）

| 编号 | 决策 | 理由 |
|---|---|---|
| D-P0-1 | **不新建表、不加列**：单元用现有 `unit` 表，产出写 `unit.production`，得分写 `attempt`，会话用 `session`，记账用 `llm_call` | 遵守 16 库对象锁；`M0-实现规格` §7 已含全部所需字段 |
| D-P0-2 | **新建 6 个 REST 端点，页面先做 2 个最小表单** | 端点复用现有 `write_security`/`read_security` 与双模（`HX-Request`）约定；不引入前端框架 |
| D-P0-3 | **不写 `turn` 表**（P0 阶段） | `turn.id` 是 INTEGER PK，而 `db.write` 强制要求行内含 `id`；写它需在写锁内 `SELECT max(id)+1`，风险大于收益，留 P1（见 §8） |
| D-P0-4 | **角色→模型采用"首个已配密钥厂商的首个模型"**，可用 `HPSL_ROLE_MODEL_<ROLE>` 覆盖 | 系统尚未实现 F8.2 的"自动分配角色"；此规则零配置可用，且不新增配置文件 |
| D-P0-5 | **复测入队在闭环成功后触发**，失败只记日志不回滚闭环 | 闭环是用户已确认的事实；入队失败最坏是少一次复测，不应让用户重做判定 |

---

## 3. 新增接口契约（6 个）

统一前缀 `/api/v1`；写操作走 `write_security`（Origin + CSRF + 限流）；返回 `ok(...)` / `BizError`。

### 3.1 建单元

```
POST /api/v1/units
{ "title": "用 pandas 做销售汇总", "difficulty_band": "拉伸" }
→ 200 { "id": "<uuid hex>", "status": "INIT", "allowed_next": ["PICK"] }
```
- `title` 1–200 字符；`difficulty_band ∈ {稳妥, 拉伸, 回补, null}`；非法 → `C400001`。
- 写入：`db.write(pid, "unit", {id, title, status: "INIT", difficulty_band}, actor="user")`。
- 无激活项目 → `A010001`。

### 3.2 提交产出（回归主线）

```
POST /api/v1/units/{uid}/production
{ "production": "…文本…" }
→ 200 { "unit_id": "...", "produced_at": "2026-…Z" }
```
- 空文本 → `C400001`；单元不存在 → `A020001`。
- 写入 `unit.production`（`db.write`，actor=user）。**不**自动 move；由既有 `/move` 推进，保持状态机唯一入口。

### 3.3 记录一次尝试（重述 / 边界）

```
POST /api/v1/units/{uid}/attempts
{ "kind": "重述", "self_score": 0.8, "confirmed_score": null, "seconds": 180, "concept_id": null }
→ 200 { "attempt_id": "...", "kind": "重述" }
```
- `kind ∈ {重述, 边界, 追问, 复测}`；分数 ∈ [0,1] 或 null；非法 → `C400001`。
- 写入 `attempt`（`db.write`，actor=user）。`rubric` 由调用方可选传入（边界题要点，作答后才有意义）。

### 3.4 闭环

```
POST /api/v1/units/{uid}/close
{ "confirmed_score": 0.75, "force_close": false, "close_reason": null,
  "is_keypoint": false, "same_source": null, "explicit_confirm": false }
→ 200 { "status": "INTEGRATE", "allowed_next": ["PICK", "CLOSED"], "reviews_enqueued": 1 }
```
- 委托 `grading.close_unit(...)`（已有全部门控：A050002/3/4、A020001、BD-03）。
- 成功后：`enqueue_reviews(pid, concept_ids, now)`；`concept_ids` 取该单元 `attempt.concept_id` 去重非空值；为空则 `reviews_enqueued=0`。

### 3.5 角色调用

```
POST /api/v1/roles/{role}/call
{ "unit_id": "...", "session_id": null, "learner_input": "…",
  "context": { "topic": "…", "learner_restatement": "…", "question": "…" } }
→ 200 { "text": "…", "provider": "deepseek", "model": "deepseek-chat",
        "tokens_in": 812, "tokens_out": 233, "cost_usd": 0.00012,
        "session_id": "...", "cost_status": "ok" }
```
- `role ∈ {topic_picker, answer_machine, questioner, boundary_setter, critic, clerk}`，未知 → `A030001`。
- 上下文**只**由 `assembler.assemble(role, context)` 组装（`answer_machine_output` 永不可见）。
- 无 `session_id` 时先创建 `session`（`unit_id` + `started_at`，actor=user）。
- 适配器：`registry.build_adapter(model_id)`；模型按 D-P0-4 选择；无可用密钥 → `A040003`；调用失败 → `A040001`。
- 记账：`cost.record_call(role, reply, session_id, unit_id)`（内部经 `db.write` 写 `llm_call`）。
- 返回 `cost_status`（`ok/warn/blocked`）供页面提示 A080001。

### 3.6 创建/结束会话（可选，P0-B）

```
POST /api/v1/units/{uid}/sessions        → { "session_id": "..." }
POST /api/v1/sessions/{sid}/end           → { "ended_at": "..." }
```
P0-A 只保留"角色调用时隐式创建"；显式会话留 P0-B。

---

## 4. 数据流（新增链路）

```
用户建单元 ──write unit(INIT)──► /unit 列表
   │
   ├─ 角色调用 ──assemble(allow)──► adapter.complete ──► Reply
   │                                   └─ record_call ──► llm_call(审计)
   │                                   └─ session(隐式创建)
   ├─ 提交产出 ──write unit.production──► /move(LEARN→PRODUCE→VERIFY)
   ├─ 记录尝试 ──write attempt(重述/边界, self/confirmed)
   └─ 闭环 ──evaluate_close(门控)──► write unit.closed_at ──► move(JUDGE→INTEGRATE)
                                            └─ enqueue_reviews(concept_ids, closed_at+2d)
                                                  └─ /review 到期列表
```

---

## 5. 文件级改动清单

| 文件 | 改动 | 类型 |
|---|---|---|
| `hpsl/routes/unit.py` | 新增 3 个端点（`POST /units`、`/production`、`/close`）；`POST /attempts` 建议同文件或新 `routes/attempt.py` | 改 |
| `hpsl/routes/roles.py` | **新增**：`POST /roles/{role}/call` | 新增 |
| `hpsl/routes/__init__.py` / `hpsl/app.py` | 挂载 `roles` 路由（中间件顺序不变） | 改 |
| `hpsl/engine/role_llm.py` | 增加 `call_with_billing(role, context, *, session_id, unit_id)`（组装 → 适配 → 记账 → 返回） | 改 |
| `hpsl/models/registry.py` | 新增 `pick_default_model()`：遍历 `list_providers()`，取首个 `key_status().has_key` 的厂商的首个模型；支持 `HPSL_ROLE_MODEL_<ROLE>` | 改 |
| `hpsl/engine/grading.py` | `close_unit(...)` 增加可选 `enqueue=True`，成功后调 `enqueue_reviews`（try/except 记日志） | 改 |
| `hpsl/templates/unit_index.html` | 顶部加"新建单元"表单（`hx-post /api/v1/units` → 刷新列表） | 改 |
| `hpsl/templates/unit_detail.html` | 加"产出 / 尝试 / 闭环"三块表单 | 改 |
| `hpsl/tests/test_p0_units.py` | 新增 ~12 例 | 新增 |
| `hpsl/tests/test_p0_roles.py` | 新增 ~8 例（注入 fake adapter） | 新增 |

**不改**：`db.py`（复用 `write`）、`state.py`（状态机不变）、`common/errors.py`（不新增码）、任何表结构。

---

## 6. 零 DDL 论证（字段映射）

| 需求 | 现有字段 | 表 |
|---|---|---|
| 建单元 | `id/title/status/difficulty_band` | `unit` |
| 产出 | `production` / `produced_at`（move 时写） | `unit` |
| 尝试得分 | `id/unit_id/concept_id/kind/rubric/self_score/confirmed_score/seconds/ts` | `attempt` |
| 闭环标记 | `closed_at/forced_close/close_reason` | `unit` |
| 会话 | `id/unit_id/started_at` | `session` |
| 记账 | `id/role/provider/model/session_id/unit_id/input_hash/tokens_in/tokens_out/cost/ts` | `llm_call` |
| 复测 | `id/concept_id/kind/due/result(NULL)` | `review` |

结论：**全部字段已存在**，`count_user_objects` 保持 16。

---

## 7. 测试计划（新增 ~20 例）

**`test_p0_units.py`（12）**
1. 建单元 → 200、status=INIT、审计 1 条；
2. 标题空/超长 → C400001；
3. 非法 difficulty_band → C400001；
4. 无激活项目 → A010001；
5. 提交产出 → production 落库、produced_at 为空（move 后才写）；
6. 产出为空 → C400001；
7. 无产出直接 move 到 VERIFY → A020003；
8. 有产出 move 到 VERIFY → produced_at 非空；
9. 记录重述尝试 → attempt 行 + 审计；
10. 记录非法分数 → C400001；
11. 闭环达标 → status=INTEGRATE、`closed_at` 非空、`reviews_enqueued>=1`；
12. 闭环后 `review` 表存在 `result IS NULL` 且 `due = closed_at+2d`；**且 `count_user_objects==16`**。

**`test_p0_roles.py`（8）**
1. 六角色参数化：调用成功返回 text；
2. 未知角色 → A030001；
3. `answer_machine_output` 即使放入 context 也不出现在发送给适配器的 messages（复用 `_RecordingAdapter`）；
4. 无密钥 → A040003；
5. 适配器失败 → A040001；
6. 调用后 `llm_call` 新增 1 行且 `tokens_in/out` 来自 usage；
7. 无 session_id 时自动建 `session` 行；
8. 未知 provider/模型 → 明确错误（不静默）。

**回归**：336 全绿；`test_governance_zero_ddl` 等零 DDL 守护不变。

---

## 8. 分期与工时

| 期 | 内容 | 预估 |
|---|---|---|
| P0-A | 后端 6 端点 + 测试（不含 UI） | 0.5–1 人日 |
| P0-B | `/unit` 与 `/unit/{uid}` 表单 + `/budget` 展示打通 + 显式会话/结束 | 1 人日 |
| P0-C（可选） | 写 `turn` 表（写锁内取 `max(id)+1`）与会话回放 | 0.5 人日 |
| P0-D（独立） | 安全红线 P0-05~08（密钥口令/限流维度/Host/关闭 docs） | 1 人日，且需先定取舍 |

---

## 9. 风险与回退

| 风险 | 缓解 |
|---|---|
| `record_call` 依赖 `active_project()`（进程级） | 路由一律从 `active_project()` 取 pid，并在调用前校验单元归属；多项目并发不在 v0 范围 |
| 角色→模型自动选择可能选到 Gemini（当前占位） | `pick_default_model` 跳过 `provider_kind == "gemini"`，或捕获 A040002 提示换厂商 |
| 记账失败导致 LLM 调用"白跑" | `record_call` 失败不回滚回复，但返回 `cost_usd=0` 并记 warning；P1 改为事务化 |
| 复测入队依赖 `attempt.concept_id`，可能为空 | 为空时 `reviews_enqueued=0`，页面提示"本单元未关联概念，暂无复测" |
| 新增端点被误用于跨项目 | 全部端点先 `db.safe_path(pid)` + 归属校验（复用 `_require_project` 模式） |

---

## 10. 明确不做（避免范围蔓延）

- 不做真正"六角色对话 UI"（只做 API + 最小表单）；
- 不做选题三候选（`topic_picker` 可调用，但不做候选卡片）；
- 不做 rubric 生成与 A050001 的界面时序（引擎守卫已有，UI 留 P1）；
- 不做检索（`chunk_fts` 继续空置）；
- 不改安全四件套（P0-05~08 单列，需先定"改代码还是改文档"）。

---

_本方案只描述改造，不修改代码；实施前建议按 `docs/M24-需求基线.md` 的形式先立基线，再按 P0-A → P0-B 分批提交。_
