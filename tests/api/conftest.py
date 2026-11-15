"""tests/api 共享配置:把 tests/agents 目录加入 sys.path 以复用 stubs 模块。

tests 目录不带 __init__.py(pytest rootdir 插入模式),跨目录导入通过路径注入完成。
"""

import sys
from pathlib import Path

_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))
