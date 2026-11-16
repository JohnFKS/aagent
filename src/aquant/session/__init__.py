"""会话与策略版本管理:多轮对话、版本快照、回退与变更对比。

对外暴露 SessionStore(文件型持久化)与 diff_codes(行级 diff),
API 层把 store 挂在 app.state 上,便于测试注入临时目录。
"""

from aquant.session.diff import diff_codes
from aquant.session.store import SessionStore

__all__ = ["SessionStore", "diff_codes"]
