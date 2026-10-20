# 启动入口与安装说明修补（2026-10-20）

> 范围：只修「按文档装不起来 / 起不来」，不涉及功能与数据。
> 影响文件：`hpsl/__main__.py`（新增）、`README.md`、`tests/test_m29_packaging.py`。

---

## 1. 问题（三项，层层相扣）

| # | 现象 | 根因 |
|---|---|---|
| F-01 | `hpsl serve` 命令不存在（`.venv/Scripts/` 下无 `hpsl.exe`） | README「安装与运行」只写了 `pip install -r requirements.txt`，**从未出现 `pip install -e .`**；而 `hpsl` 命令由 `[project.scripts]` 生成，不装包就永远不会有 |
| F-02 | `pip show hpsl` 显示 **0.0.0**，而 `pyproject.toml` 已是 **0.1.0** | M33 重建 venv 后未重装，残留旧 editable 安装（`__editable__.hpsl-0.0.0.pth`） |
| F-03 | `python -m hpsl` 报 `No module named hpsl.__main__` | 包内缺 `__main__.py`，没有第二条启动路径可兜底 |

后果：新机器 / venv 重建后照 README 执行，第一步卡死；实际能用的
`python -c "from hpsl.app import main; main()"` 没有写在任何文档里。

---

## 2. 修复

| 项 | 做法 |
|---|---|
| 新增 `hpsl/__main__.py` | `from hpsl.app import main` + `sys.exit(main())`，使 `python -m hpsl ...` 与 `hpsl ...` 完全等价；同时被 setuptools 自动纳入 wheel |
| 重装 editable | `.venv/Scripts/python.exe -m pip install -e . --no-deps`：卸载 0.0.0、安装 0.1.0、生成 `Scripts/hpsl.exe` |
| README 安装段 | 补 `pip install -e .`（并注明「不装则只能用 `python -m hpsl`」） |
| README 启动段 | `hpsl serve` 下补等价写法 `python -m hpsl serve` |
| README 过期数字 | `M1–M36` → `M0–M39`；测试数 `692` → `740`；能力清单补 M37 / M38 / M39 三条 |
| 测试守护 | `test_m29_packaging.py` 新增 `test_python_dash_m_entrypoint_is_wired`（AC4）：断言 `__main__.py` 存在且暴露 `main`，防止被误删 |

---

## 3. 验证

| 项 | 结果 |
|---|---|
| `hpsl --help` | 列出 `serve / rotate / backup / snapshot / restore` |
| `python -m hpsl --help` | 同上（`prog` 仍显示 `hpsl`） |
| `hpsl serve --port 8788` | HTTP **200** |
| `python -m hpsl serve --port 8789` | HTTP **200** |
| `pip show hpsl` | Version **0.1.0**，editable 指向工作区 |
| 全量测试（JUnit 校准） | `tests=743 failures=0 errors=0 skipped=3` → **740 passed / 3 skipped**（较 M39 基线 +1，即新增入口测试） |
| 基线守护 | `BASELINE INTACT`（16 库对象、零 DDL 不变） |
| 打包自检 | `结果: PASS`，**107 条目**（较 M39 的 106 +1，即 `hpsl/__main__.py`）；模板 28 / 提示词 6 / 静态 5 不变 |

---

## 4. 已知环境噪声（非本次引入）

本机全量 `pytest` 的**进程退出码为 1**，但 JUnit 报表为 `0 failures / 0 errors`：
收尾清理 `pytest-of-Administrator\tmp...` 时被沙箱的 safe-delete hook
以 `trash-failed / 找不到网络路径` 拒绝（fail-closed），覆盖了 pytest 自身的退出码。

判读方式：看 `--junit-xml` 报表，或确认进度条全部为 `.` 且无 `F` / `E`。此项属运行环境，不改代码。
