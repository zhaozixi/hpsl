"""支持 `python -m hpsl ...`，与 `hpsl ...` 命令完全等价。

存在理由：`hpsl` 命令由 `[project.scripts]` 生成，只有执行过
`pip install -e .`（或 `pip install .`）之后才存在于 `Scripts/`；
在未安装、或 venv 重建后忘记重装的场景下，`python -m hpsl` 是唯一可直接使用的入口。
"""

import sys

from hpsl.app import main

if __name__ == "__main__":
    sys.exit(main())
