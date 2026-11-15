"""Layer 1:策略代码的 AST 静态检查(未来函数风险 / 可复现性 / 时序 API 误用)。

设计原则(借鉴 qlib 的信号错位思想与 PA_Agent 的校验-分级实践):
- 规则只做"有把握的"模式识别:命中即拦截,没命中不代表没问题(报告页声明)。
- error = 结果不可信;warning = 需人工确认的模式。
"""

from __future__ import annotations

import ast

from aquant.validation.findings import Finding, RuleId, Severity

# 外部数据获取:归一化后的完整调用路径精确匹配(经 _ALIASES 别名归一化,
# `import pandas as pd` 与 `import pandas` 均命中)
_EXTERNAL_QUALNAMES: dict[str, str] = {
    "requests.get": "requests.get",
    "requests.post": "requests.post",
    "urllib.request.urlopen": "urlopen",
    "httpx.get": "httpx.get",
    "httpx.post": "httpx.post",
    "socket.socket": "socket",
    "pandas.read_csv": "pd.read_csv",
    "pandas.read_excel": "pd.read_excel",
    "pandas.read_sql": "pd.read_sql",
    "pandas.read_parquet": "pd.read_parquet",
    "pandas.read_hdf": "pd.read_hdf",
}
# 裸函数名(from x import y 风格):只收录足够独特、不会误报普通方法调用的名字。
# 注意:requests.get 之类绝不入此表——裸名 get 会误杀 dict.get/持仓.get 等合规调用。
_EXTERNAL_BARE: dict[str, str] = {
    "read_csv": "pd.read_csv",
    "read_excel": "pd.read_excel",
    "read_sql": "pd.read_sql",
    "read_parquet": "pd.read_parquet",
    "read_hdf": "pd.read_hdf",
    "urlopen": "urlopen",
}

_WALL_CLOCK_CALLS: dict[str, str] = {
    "datetime.datetime.now": "datetime.now",
    "datetime.datetime.today": "datetime.today",
    "datetime.datetime.utcnow": "datetime.utcnow",
    # from datetime import datetime 之后的写法
    "datetime.now": "datetime.now",
    "datetime.today": "datetime.today",
    "datetime.utcnow": "datetime.utcnow",
    "datetime.date.today": "date.today",
    "pandas.Timestamp.now": "pd.Timestamp.now",
    "pandas.Timestamp.today": "pd.Timestamp.today",
    "pandas.Timestamp.utcnow": "pd.Timestamp.utcnow",
    "time.time": "time.time",
    "time.localtime": "time.localtime",
}

# 常见导入别名 → 规范模块名(qualname 归一化用)
_ALIASES = {"pd": "pandas"}

# RQAlpha 策略生命周期入口;在这些函数体中做作用域敏感检查
_ENTRY_FUNCS = {"init", "before_trading", "open_auction", "handle_bar", "after_trading", "tick"}

# before_trading / open_auction 运行在当日行情产生之前,引用行情快照 = 未来函数
_PRE_MARKET_FUNCS = {"before_trading", "open_auction"}


def _qualname(node: ast.expr) -> str:
    """把 Attribute/Name 链转成点分名,并把常见别名归一化(pd→pandas)。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        head = _ALIASES.get(node.id, node.id)
        parts.append(head)
        return ".".join(reversed(parts))
    return ""


class _StrategyVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self._current_func: str | None = None

    # ---- 调用类规则 -------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        name = _qualname(node.func)
        bare = node.func.id if isinstance(node.func, ast.Name) else None
        label: str | None = None
        if name:
            label = _EXTERNAL_QUALNAMES.get(name)
        if label is None and bare is not None:
            label = _EXTERNAL_BARE.get(bare)
        if label is None and bare == "open":
            label = "open()(文件读取)"
        if label is not None:
            self.findings.append(
                Finding(
                    RuleId.AST_EXTERNAL_DATA,
                    Severity.ERROR,
                    f"策略代码中直接获取外部数据({label}):绕过平台统一数据口径,历史查询无法保证无未来数据且回测不可复现。"
                    "请改用平台数据 API(history_bars/get_price 等,经 bundle 复权口径)。",
                    detail={"call": name or bare, "line": node.lineno},
                )
            )
        for qn, label in _WALL_CLOCK_CALLS.items():
            if name == qn:
                self.findings.append(
                    Finding(
                        RuleId.AST_WALL_CLOCK,
                        Severity.ERROR,
                        f"使用墙钟时间({label})导致回测不可复现,且可能引入未来信息。请使用 context.now(回测时钟)。",
                        detail={"call": name, "line": node.lineno},
                    )
                )
                break
        # 全局拟合:在策略生命周期内用全样本训练(需滚动/走窗重训)
        if name.endswith("fit") or name.endswith("fit_transform"):
            self.findings.append(
                Finding(
                    RuleId.AST_GLOBAL_FIT,
                    Severity.WARNING,
                    f"检测到模型拟合调用({name}):若拟合用了整个回测期数据则为严重未来函数。"
                    "请仅使用截至当前 bar 的数据滚动重训,或把训练期严格限制在样本内并声明。",
                    detail={"call": name, "line": node.lineno},
                )
            )
        self.generic_visit(node)

    # ---- 下标/切片类规则 ---------------------------------------------------
    def visit_Subscript(self, node: ast.Subscript) -> None:
        sl = node.slice
        if isinstance(sl, ast.Slice) and sl.step is not None and isinstance(sl.step, ast.UnaryOp) and isinstance(
            sl.step.op, ast.USub
        ):
            self.findings.append(
                Finding(
                    RuleId.AST_REVERSE_SLICE,
                    Severity.WARNING,
                    "检测到序列反转切片 [::-1]:以'反转后取第一条'推断最新数据是脆弱模式,"
                    "容易在时间轴对齐错误时引入未来数据。请使用显式日期/iloc 定位。",
                    detail={"line": node.lineno},
                )
            )
        self.generic_visit(node)

    # ---- 作用域敏感规则 ----------------------------------------------------
    def _visit_entry(self, node: ast.FunctionDef) -> None:
        prev = self._current_func
        self._current_func = node.name
        if node.name in _PRE_MARKET_FUNCS:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == "bar_dict":
                    self.findings.append(
                        Finding(
                            RuleId.AST_BEFORE_TRADING_PRICE,
                            Severity.ERROR,
                            f"{node.name}() 中引用 bar_dict:{node.name} 运行于当日行情产生之前,"
                            "此处不存在任何当日 bar 数据,引用即未来函数。",
                            detail={"line": sub.lineno},
                        )
                    )
                if isinstance(sub, ast.Call):
                    n = _qualname(sub.func)
                    if n.endswith("current_snapshot") or n.endswith("get_snapshot"):
                        self.findings.append(
                            Finding(
                                RuleId.AST_BEFORE_TRADING_PRICE,
                                Severity.ERROR,
                                f"{node.name}() 中获取行情快照({n}):当日行情未发生,该数据在回测中必然来自未来。",
                                detail={"line": sub.lineno},
                            )
                        )
                    tail = n.rsplit(".", 1)[-1]
                    if tail in ("history_bars", "get_price"):
                        self.findings.append(
                            Finding(
                                RuleId.AST_BEFORE_TRADING_PRICE,
                                Severity.ERROR,
                                f"{node.name}() 中调用行情历史接口({n}):盘前拿到的当日/近期数据在实盘同一时点尚不存在,"
                                "口径错位即未来函数。历史数据请在 handle_bar 内按收盘后口径获取。",
                                detail={"line": sub.lineno},
                            )
                        )
        if node.name == "init":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    n = _qualname(sub.func)
                    if n.startswith("order") or n in ("buy", "sell"):
                        self.findings.append(
                            Finding(
                                RuleId.AST_ORDER_API_MISUSE,
                                Severity.WARNING,
                                f"init() 中调用下单接口({n}):RQAlpha 的 init 先于行情初始化,"
                                "此处下单行为与实盘时序不一致。请在 handle_bar/before_trading 之后交易。",
                                detail={"line": sub.lineno},
                            )
                        )
        self.generic_visit(node)
        self._current_func = prev

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name in _ENTRY_FUNCS:
            self._visit_entry(node)
        else:
            self.generic_visit(node)

    # 生成的策略可能是简单脚本,AsyncFunctionDef 同样处理
    visit_AsyncFunctionDef = visit_FunctionDef


class _ShiftDetector(ast.NodeVisitor):
    """检测 .shift(负数) — pandas 面板数据向未来取数,典型未来函数。"""

    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "shift" and node.args:
            arg = node.args[0]
            is_negative = False
            if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                is_negative = True
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
                is_negative = arg.value < 0
            if is_negative:
                self.findings.append(
                    Finding(
                        RuleId.AST_SHIFT_NEGATIVE,
                        Severity.ERROR,
                        "检测到 .shift(负数):pandas 序列向未来方向取数,是典型未来函数。"
                        "如需'未来收益'仅可用于样本外评估的标签列,绝不可作为下单依据。",
                        detail={"line": node.lineno},
                    )
                )
        self.generic_visit(node)


REQUIRED_ENTRIES = ("init", "handle_bar")


def check_ast(code: str) -> list[Finding]:
    """对策略源码做全部 AST 规则检查。"""
    findings: list[Finding] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [
            Finding(
                RuleId.AST_SYNTAX,
                Severity.ERROR,
                f"策略代码语法错误,无法执行:{e.msg}(第 {e.lineno} 行)",
                detail={"line": e.lineno},
            )
        ]

    top_names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [f for f in REQUIRED_ENTRIES if f not in top_names]
    if missing:
        findings.append(
            Finding(
                RuleId.AST_SYNTAX,
                Severity.ERROR,
                f"缺少 RQAlpha 策略必需入口函数:{', '.join(missing)}。",
                detail={"missing": missing},
            )
        )

    detector = _ShiftDetector()
    detector.visit(tree)
    findings.extend(detector.findings)

    visitor = _StrategyVisitor()
    visitor.visit(tree)
    findings.extend(visitor.findings)
    return findings
