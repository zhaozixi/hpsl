"""模型注册表与异构判定（F8 / BD-7，零三方依赖）。

- 读取同目录 models.yaml（用户可维护的模型清单）与 pricing.yaml（价格表）。
- ModelConfig：provider / family / derived_from / supports_json / is_fallback_for。
- heterogeneous(a, b)：两模型是否**异源**（用于 M5 备选触发四规则里的"异源降级"判断）。
  判定：取各自"源集合" = {family} ∪ ({derived_from} 若非空)；集合相交 → 同源 → 返回 False；
  不相交 → 异源 → 返回 True。
- provider_kind(provider)：Provider → 接入协议类别（openai_compat / anthropic / gemini），
  由 models.yaml 的 `kind` 字段声明；适配器工厂与连通性探活均按 kind 路由，
  新增厂商（如国内大模型）只需注册 provider + kind，无需改路由分支。
- default_base_url(provider)：Provider → 默认接入点（读 models.yaml 的 `base_url`；M10 真实接线用）。
- build_adapter(model_id, ...)：依 provider 的 kind 构造对应适配器（E-01 / E-02 / O7 占位）。

纪律（冻结）：不得被 common 反向依赖；YAML 解析用自研 _mini_yaml（环境无 PyYAML）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ._mini_yaml import load_yaml_file
from .anthropic import AnthropicAdapter
from .base import ProviderAdapter
from .gemini import GeminiAdapter
from .openai_compat import OpenAICompatAdapter

_MODELS_YAML = Path(__file__).parent / "models.yaml"
_PRICING_YAML = Path(__file__).parent / "pricing.yaml"

# 仅作最终兜底（models.yaml 未声明 base_url 时）。所有厂商接入点以 YAML 为单一事实来源。
_DEFAULT_BASE_URLS: dict[str, str] = {}


@dataclass(frozen=True)
class ModelConfig:
    """单模型配置（逐字对齐 models.yaml 字段）。"""

    id: str
    provider: str
    family: str
    derived_from: Optional[str]
    supports_json: bool
    is_fallback_for: Optional[str]


class _Registry:
    """惰性加载的模型注册表（模块级单例）。"""

    def __init__(self):
        self._models: dict[str, ModelConfig] = {}
        self._providers: dict[str, str] = {}
        self._pricing: dict[str, dict] = {}
        self._loaded = False

    def _ensure(self) -> None:
        if self._loaded:
            return
        raw = load_yaml_file(_MODELS_YAML)
        for prov, meta in (raw.get("providers") or {}).items():
            # 保留完整 provider 元数据（display / kind / base_url），供路由与展示使用。
            self._providers[prov] = meta or {}
        for m in raw.get("models") or []:
            cfg = ModelConfig(
                id=m["id"],
                provider=m["provider"],
                family=m["family"],
                derived_from=m.get("derived_from"),
                supports_json=bool(m.get("supports_json", False)),
                is_fallback_for=m.get("is_fallback_for"),
            )
            self._models[cfg.id] = cfg
        try:
            praw = load_yaml_file(_PRICING_YAML)
            self._pricing = praw.get("models") or {}
        except FileNotFoundError:
            self._pricing = {}
        self._loaded = True

    # -- 查询 -------------------------------------------------------------
    def get_model(self, model_id: str) -> ModelConfig:
        self._ensure()
        if model_id not in self._models:
            raise KeyError(f"未注册的模型：{model_id!r}（检查 models.yaml）")
        return self._models[model_id]

    def list_models(self) -> list[ModelConfig]:
        self._ensure()
        return list(self._models.values())

    def fallback_for(self, provider: str) -> Optional[str]:
        """返回该 provider 的备选 provider（models.yaml 里 is_fallback_for 反查）。"""
        self._ensure()
        for cfg in self._models.values():
            if cfg.is_fallback_for == provider:
                return cfg.provider
        return None

    def pricing(self, model_id: str) -> Optional[dict]:
        self._ensure()
        return self._pricing.get(model_id)

    def default_base_url(self, provider: str) -> str:
        """Provider → 默认接入点。优先取 models.yaml 的 base_url；否则走兜底。"""
        self._ensure()
        meta = self._providers.get(provider) or {}
        if meta.get("base_url"):
            return meta["base_url"]
        return _DEFAULT_BASE_URLS.get(provider, f"https://api.{provider}.example/v1")

    def provider_kind(self, provider: str) -> str:
        """Provider → 接入协议类别（openai_compat / anthropic / gemini）。

        决定适配器构造与连通性探活分支；YAML 未声明 kind 时回退 openai_compat。
        """
        self._ensure()
        meta = self._providers.get(provider) or {}
        return meta.get("kind", "openai_compat")

    def list_providers(self) -> list[str]:
        """返回全部已知 Provider（以 models.yaml 声明为准，排序去重）。"""
        self._ensure()
        return sorted(self._providers)

    def provider_display(self, provider: str) -> str:
        """Provider 人读展示名（YAML display 缺失时回退为 provider 本身）。"""
        self._ensure()
        return (self._providers.get(provider) or {}).get("display", provider)

    # -- 异构判定 ---------------------------------------------------------
    @staticmethod
    def _sources(cfg: ModelConfig) -> set[str]:
        s = {cfg.family}
        if cfg.derived_from:
            s.add(cfg.derived_from)
        return s

    def heterogeneous(self, a: ModelConfig, b: ModelConfig) -> bool:
        """两模型是否异源（True=异源可互为备选；False=同源，避免同源偏误）。"""
        return not (self._sources(a) & self._sources(b))

    # -- 适配器工厂 -------------------------------------------------------
    def build_adapter(
        self,
        model_id: str,
        *,
        key_resolver: Optional[Callable[[], str]] = None,
        base_url: Optional[str] = None,
    ) -> ProviderAdapter:
        cfg = self.get_model(model_id)
        url = base_url or self.default_base_url(cfg.provider)
        # 按接入协议类别（kind）路由，而非字面 provider 名——新增厂商（如国内大模型）
        # 只需在 models.yaml 声明 kind，即可复用既有适配器，无需新增路由分支。
        kind = self.provider_kind(cfg.provider)
        if kind == "anthropic":
            return AnthropicAdapter(
                name=cfg.provider,
                base_url=url,
                model_id=cfg.id,
                key_resolver=key_resolver,
                provider=cfg.provider,
            )
        if kind == "gemini":
            return GeminiAdapter(name=cfg.provider, model_id=cfg.id)
        # 默认 / openai_compat：OpenAI 兼容协议（OpenAI、DeepSeek、通义、智谱、Kimi、豆包等）
        return OpenAICompatAdapter(
            name=cfg.provider,
            base_url=url,
            model_id=cfg.id,
            key_resolver=key_resolver,
            provider=cfg.provider,
            supports_json=cfg.supports_json,
        )


# 模块级单例
REGISTRY = _Registry()


def get_model(model_id: str) -> ModelConfig:
    return REGISTRY.get_model(model_id)


def list_models() -> list[ModelConfig]:
    return REGISTRY.list_models()


def heterogeneous(a: ModelConfig, b: ModelConfig) -> bool:
    return REGISTRY.heterogeneous(a, b)


def default_base_url(provider: str) -> str:
    return REGISTRY.default_base_url(provider)


def list_providers() -> list[str]:
    return REGISTRY.list_providers()


def provider_display(provider: str) -> str:
    return REGISTRY.provider_display(provider)


def provider_kind(provider: str) -> str:
    return REGISTRY.provider_kind(provider)


def pricing(model_id: str) -> Optional[dict]:
    return REGISTRY.pricing(model_id)


def build_adapter(model_id: str, *, key_resolver=None, base_url=None) -> ProviderAdapter:
    return REGISTRY.build_adapter(model_id, key_resolver=key_resolver, base_url=base_url)


def _secrets_dir_path() -> Path:
    """密钥目录（与 routes/settings_keys 一致：HPSL_HOME/secrets）。按调用时解析。"""
    from .. import config as _config

    return _config.home() / "secrets"


def provider_has_key(provider: str) -> bool:
    """该厂商当前是否有可用密钥（锁定态视为没有；异常一律按没有处理）。"""
    from ..secrets import keystore

    try:
        return bool(keystore.key_status(provider, secrets_dir=_secrets_dir_path()).get("has_key"))
    except Exception:
        return False


# 可显式指定模型的角色（user 是学习者本人，不对应任何模型）。
# 与 common.enums.Role 保持一致；此处用字面量避免 models 层反向依赖业务枚举文件。
CONFIGURABLE_ROLES: tuple[str, ...] = (
    "answer_machine",
    "questioner",
    "boundary_setter",
    "critic",
    "clerk",
    "topic_picker",
)

_ROLE_LABELS: dict[str, str] = {
    "answer_machine": "答案机",
    "questioner": "提问者",
    "boundary_setter": "出题者",
    "critic": "挑错者",
    "clerk": "书记员",
    "topic_picker": "选题助手",
}


def resolve_role_model(role: str, role_models: Optional[dict] = None) -> str:
    """解析某角色**实际**会用的模型（M39）。

    优先级：项目配置 `role_models[role]` → 自动选择（`pick_default_model`）。

    与 `pick_default_model` 的差异：显式配置**不允许静默降级**。如果指定的模型未注册、
    或它的厂商没有密钥，直接抛 A040003 并给出两条出路（去填密钥 / 改回自动），
    而不是悄悄换成别家的模型——那会让用户在不知情的情况下换模型、换价格。
    """
    from ..common.errors import BizError

    requested = None
    if isinstance(role_models, dict):
        requested = role_models.get(role)
    if not requested:
        return pick_default_model(role)
    try:
        cfg = REGISTRY.get_model(str(requested))
    except KeyError:
        raise BizError(
            "A040003",
            msg=f"角色「{_ROLE_LABELS.get(role, role)}」指定的模型不可用：{requested}"
            "（未在 models.yaml 注册或已下线），请到「设置」把它改回自动选择",
        )
    if not provider_has_key(cfg.provider):
        raise BizError(
            "A040003",
            msg=f"角色「{_ROLE_LABELS.get(role, role)}」指定用 {cfg.id}，但厂商"
            f"「{REGISTRY.provider_display(cfg.provider)}」没有可用密钥；"
            "请到「密钥」页填写，或到「设置」把该角色改回自动选择",
        )
    return cfg.id


def describe_role_models(role_models: Optional[dict] = None) -> list[dict]:
    """设置页展示用：每个角色「配了什么 / 实际用什么 / 为什么用不了」。

    只读、不抛异常——配置坏了也要能在页面上看见原因，而不是整页 500。
    """
    from ..common.errors import BizError

    out: list[dict] = []
    for role in CONFIGURABLE_ROLES:
        requested = (role_models or {}).get(role) if isinstance(role_models, dict) else None
        item = {
            "role": role,
            "label": _ROLE_LABELS.get(role, role),
            "requested": requested or "",
            "effective": "",
            "source": "explicit" if requested else "auto",
            "error": "",
        }
        try:
            item["effective"] = resolve_role_model(role, role_models)
        except BizError as exc:
            item["error"] = exc.msg
        out.append(item)
    return out


def pick_default_model(role: Optional[str] = None) -> str:
    """选择角色的默认模型（P0 主链接线用，零配置可跑）。

    优先级：
    1. 环境变量 `HPSL_ROLE_MODEL_<ROLE>`（role 大写；role 为空时 `HPSL_DEFAULT_MODEL`）；
    2. 首个「已配置密钥」厂商的第一个已注册模型；
    3. 跳过 gemini（当前 `GeminiAdapter` 为占位，调用即 A040002）。

    无可用密钥 → A040003（与 keystore 同一语义），错误文案指出下一步动作。
    说明：未在项目配置里显式指定角色模型时，本函数即"自动选择"的实现；
    显式指定走 `resolve_role_model`。
    """
    import os

    from ..common.errors import BizError

    override = None
    if role:
        override = os.environ.get(f"HPSL_ROLE_MODEL_{str(role).upper()}")
    override = override or os.environ.get("HPSL_DEFAULT_MODEL")
    if override:
        return override

    # 密钥目录与 routes/settings_keys 保持一致：HPSL_HOME/secrets（不在源码包内）
    REGISTRY._ensure()
    for provider in REGISTRY.list_providers():
        if REGISTRY.provider_kind(provider) == "gemini":
            continue
        if not provider_has_key(provider):
            continue
        for cfg in REGISTRY.list_models():
            if cfg.provider == provider:
                return cfg.id
    raise BizError(
        "A040003",
        msg="没有可用的模型密钥：请到「密钥」页填写 DeepSeek / 千问 / 智谱 任一家的 API 密钥",
    )
