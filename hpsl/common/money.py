"""货币展示：美元记账、人民币折算同时显示（M38）。

背景：全系统按美元记账（`llm_call.cost_usd`、`pricing.yaml` 均为 USD），但使用者在
国内按人民币感知花费，"$0.0123" 这个数字看不出贵不贵。本模块只做**展示层**折算：

- 记账口径不动：库里的 cost 仍是 USD，预算上限仍按 USD 存储与比较；
- 汇率来自项目配置 `usd_cny_rate`（默认 7.2，设置页可改），不联网、不引入外部依赖；
- 所有函数都是纯函数，便于单测；模板通过 `templating.make_templates` 注入的
  `usd` / `cny` / `both` 过滤器调用，汇率由路由传入（不做进程级全局状态）。
"""

from __future__ import annotations

from typing import Optional

# 默认汇率：只作为「没配置过」时的兜底，界面上始终把实际使用的汇率写出来，
# 避免用户把展示值当成实时牌价。
DEFAULT_USD_CNY = 7.2


def _usd_digits(value: float) -> int:
    """美元位数：≥1 元两位（$10.00），≥0.01 四位（$0.0123），更小六位。"""
    a = abs(value)
    if a == 0:
        return 2          # 零花费显示 $0.00，而不是一串 0
    if a >= 1:
        return 2
    return 4 if a >= 0.01 else 6


def _cny_digits(value: float) -> int:
    """人民币位数：≥0.01 两位（¥0.09 / ¥72.00），更小四位；零显示两位。"""
    a = abs(value)
    if a == 0:
        return 2
    return 4 if a < 0.01 else 2


def fmt_usd(value: Optional[float], digits: Optional[int] = None) -> str:
    """美元展示：`$0.0123`。负数（理论不存在）按原样带符号。"""
    if value is None:
        return "—"
    v = float(value)
    d = _usd_digits(v) if digits is None else digits
    return f"${v:.{d}f}"


def _coerce_rate(rate) -> float:
    """把上下文里的汇率安全转成 float；None / Undefined / 脏值一律回落默认值。"""
    try:
        r = float(rate)
    except Exception:  # None / Undefined / 脏字符串，一律回落默认值
        return DEFAULT_USD_CNY
    return r if r > 0 else DEFAULT_USD_CNY


def fmt_cny(value: Optional[float], rate: Optional[float] = None, digits: Optional[int] = None) -> str:
    """人民币折算展示：`¥0.0886`。rate 缺省用 DEFAULT_USD_CNY。"""
    if value is None:
        return "—"
    r = _coerce_rate(rate)
    v = float(value) * r
    d = _cny_digits(v) if digits is None else digits
    return f"¥{v:.{d}f}"


def fmt_both(value: Optional[float], rate: Optional[float] = None) -> str:
    """双币展示：`$0.0123（约 ¥0.09）`。前端表格里最常用的形态。"""
    if value is None:
        return "—"
    return f"{fmt_usd(value)}（约 {fmt_cny(value, rate)}）"


def fmt_rate(rate: Optional[float] = None) -> str:
    """汇率说明：`1 美元 ≈ ¥7.20`（页脚注明口径，避免被当成实时汇率）。"""
    r = _coerce_rate(rate)
    return f"1 美元 ≈ ¥{r:.2f}"
