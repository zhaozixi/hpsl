"""极简块式 YAML 解析器（M4 专用，零三方依赖）。

为何自研：运行环境未安装 PyYAML（连同 cryptography / keyring / argon2 均缺失），
而 registry 须读取用户可维护的 models.yaml / pricing.yaml。本解析器覆盖本项目
YAML 用到的全部语法（块式、缩进驱动）：

- 注释：整行 `# ...`（行首空白后 `#`）。
- 映射：`key: value`；`key:` 后接更深缩进块（嵌套映射或序列）。
- 序列：`- scalar` 或 `- key: value`（映射项，后续同级键以 `缩进+2` 续写）。
- 标量：int / float / bool(true|false) / null(~ 或 null) / 字符串（支持 " 与 ' 引号）。

不处理流式语法（`[a, b]` / `{a: b}`）、锚点、多文档等。registry 的模型清单结构简单，
本解析器足够且确定。

纪律：不得被 common 反向依赖；纯函数，无副作用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s == "" or s == "~":
        return None
    if (s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'"):
        return s[1:-1]
    low = s.lower()
    if low == "null":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    # 去引号后的裸字符串若含 ' #' 视为内联注释，截断
    if " #" in s:
        s = s.split(" #", 1)[0].strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _split_kv(text: str):
    if ":" not in text:
        return None
    idx = text.index(":")
    key = text[:idx].strip()
    rest = text[idx + 1:].strip()
    return key, rest


def _tokenize(lines: list[str]) -> list[tuple[int, str]]:
    toks: list[tuple[int, str]] = []
    for ln in lines:
        stripped = ln.rstrip("\n").rstrip()
        if not stripped.strip():
            continue
        if stripped.lstrip().startswith("#"):
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        toks.append((indent, stripped.strip()))
    return toks


def _parse_node(toks: list[tuple[int, str]], i: int, ind: int):
    if i >= len(toks):
        return None, i
    _, text = toks[i]
    if text.startswith("- "):
        return _parse_seq(toks, i, ind)
    return _parse_map(toks, i, ind)


def _parse_map(toks, i, ind):
    result: dict[str, Any] = {}
    while i < len(toks):
        ci, text = toks[i]
        if ci < ind:
            break
        if ci > ind:
            break
        kv = _split_kv(text)
        if kv is None:
            break
        key, rest = kv
        if rest == "":
            if i + 1 < len(toks) and toks[i + 1][0] > ind:
                val, i = _parse_node(toks, i + 1, toks[i + 1][0])
                result[key] = val
            else:
                result[key] = None
                i += 1
        else:
            result[key] = _parse_scalar(rest)
            i += 1
    return result, i


def _parse_seq(toks, i, ind):
    items: list[Any] = []
    while i < len(toks):
        ci, text = toks[i]
        if ci != ind or not text.startswith("- "):
            break
        item_text = text[2:].strip()
        if ":" in item_text and not item_text.startswith("["):
            key, rest = _split_kv(item_text)
            item: dict[str, Any] = {}
            if rest == "":
                if i + 1 < len(toks) and toks[i + 1][0] > ind:
                    val, i = _parse_node(toks, i + 1, toks[i + 1][0])
                    item[key] = val
                    items.append(item)
                    continue
                item[key] = None
                i += 1
                items.append(item)
                continue
            item[key] = _parse_scalar(rest)
            i += 1
            child_indent = ind + 2
            while i < len(toks) and toks[i][0] == child_indent and not toks[i][1].startswith("- "):
                k2, r2 = _split_kv(toks[i][1])
                if r2 == "":
                    if i + 1 < len(toks) and toks[i + 1][0] > child_indent:
                        v2, i = _parse_node(toks, i + 1, toks[i + 1][0])
                        item[k2] = v2
                        continue
                    item[k2] = None
                    i += 1
                    continue
                item[k2] = _parse_scalar(r2)
                i += 1
            items.append(item)
            continue
        items.append(_parse_scalar(item_text))
        i += 1
    return items, i


def load_yaml(text: str) -> Any:
    """解析 YAML 文本（块式，见模块说明）。返回 dict / list / 标量。"""
    toks = _tokenize(text.splitlines())
    if not toks:
        return {}
    value, _ = _parse_node(toks, 0, 0)
    return value


def load_yaml_file(path: str | Path) -> Any:
    return load_yaml(Path(path).read_text(encoding="utf-8"))
