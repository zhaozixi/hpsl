# models/ — 模型适配层（BC-7 防腐层）

模型适配层，实现 httpx 直连的 Provider 适配，向上层（engine / roles）屏蔽厂商差异。冻结接口见基线 §9.1。

- `base.py`：Provider 协议、`Msg`、`Reply` 数据类；httpx 直连，支持 **8 家厂商**（OpenAI / Anthropic / Gemini / DeepSeek / 通义千问 / 智谱 GLM / Moonshot(Kimi) / 豆包），接入协议 **3 类**（`openai_compat` / `anthropic` / `gemini`）；由 M22 从初始双 Provider 扩展而来。
- `openai_compat.py` / `anthropic.py` / `gemini.py`：适配器实现（国内厂商统一复用 `openai_compat`）。
- `connectivity.py`：按协议分流的连通性自检（Bearer / x-api-key / key 参数差异）。
- `registry.py` + `models.yaml` / `pricing.yaml`：模型注册、异构判定（`heterogeneous`）、价格表（用户可维护）。
- 失败重试策略（指数退避）内置于适配器，逐次记账。

密钥管理在 `secrets/`（keyring 优先，Fernet+Argon2id 降级），本层不持有密钥。本层不得被 `common` 反向依赖。
