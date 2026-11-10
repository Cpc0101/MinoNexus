"""MinoNexus 服务端中枢。

这一层只做一件事：**修掉 `python mino_nexus/cli.py` 这种启动方式的路径污染**。

为什么必须有这段：本包里有 `mino_nexus/platform/`（架构 2 层）。直接跑
`python mino_nexus/cli.py` 时 Python 把脚本所在目录（也就是本包目录）放进
`sys.path[0]`，于是任何模块里的 `import platform` 都会命中**本包的同名子包**
而不是标准库。SQLAlchemy 在 import 阶段就调 `platform.python_implementation()`，
直接 `AttributeError: module 'platform' has no attribute 'python_implementation'`。

`python -m mino_nexus.cli` 不会有这个问题（`sys.path[0]` 是仓库根），所以它藏了很久。
这里在包初始化时把「本包目录」从 sys.path 摘掉、换回仓库根，两种启动方式等价。
"""
from __future__ import annotations

import os
import sys


def _unshadow_stdlib() -> None:
    package_dir = os.path.dirname(os.path.abspath(__file__))
    # sys.path[0] 是脚本目录时才会污染；-m / 已安装的包不会走到这里。
    while sys.path and os.path.abspath(sys.path[0] or os.getcwd()) == package_dir:
        sys.path.pop(0)
    root = os.path.dirname(package_dir)
    if root and root not in sys.path:
        sys.path.insert(0, root)


_unshadow_stdlib()
