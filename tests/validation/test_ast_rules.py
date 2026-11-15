"""Layer 1 AST 规则测试:未来函数 / 不可复现 / 时序误用的拦截能力。"""

import pytest

from aquant.validation.ast_rules import check_ast
from aquant.validation.findings import RuleId, Severity

GOOD_STRATEGY = '''
def init(context):
    context.s1 = "000001.XSHE"

def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, 20, "1d", "close")
    ma = prices[-5:].mean()
    if prices[-1] > ma:
        order_target_percent(context.s1, 0.9)
'''


def _rules(findings):
    return {f.rule for f in findings}


class TestGoodStrategy:
    def test_clean_strategy_passes(self):
        findings = check_ast(GOOD_STRATEGY)
        errors = [f for f in findings if f.severity is Severity.ERROR]
        assert errors == []


class TestShiftNegative:
    @pytest.mark.parametrize("expr", [
        "s.shift(-1).iloc[-1]",
        "df['close'].shift(-5)",
        "s.shift(-n)",
    ])
    def test_shift_negative_is_error(self, expr):
        code = f'''
def init(context):
    context.s1 = "000001.XSHE"

def handle_bar(context, bar_dict):
    s = history_bars(context.s1, 20, "1d", "close")
    {expr}
    order_shares(context.s1, 100)
'''
        findings = check_ast(code)
        assert RuleId.AST_SHIFT_NEGATIVE in _rules(findings)
        f = next(f for f in findings if f.rule == RuleId.AST_SHIFT_NEGATIVE)
        assert f.severity is Severity.ERROR

    def test_shift_positive_is_allowed(self):
        code = GOOD_STRATEGY + "\n    x = s.shift(1)\n"
        assert RuleId.AST_SHIFT_NEGATIVE not in _rules(check_ast(code))


class TestWallClock:
    @pytest.mark.parametrize("call", [
        "datetime.datetime.now()",
        "datetime.date.today()",
        "pd.Timestamp.now()",
        "time.time()",
    ])
    def test_wall_clock_is_error(self, call):
        code = f'''
def init(context):
    context.s1 = "000001.XSHE"

def handle_bar(context, bar_dict):
    t = {call}
    order_shares(context.s1, 100)
'''
        findings = check_ast(code)
        assert RuleId.AST_WALL_CLOCK in _rules(findings)


class TestExternalData:
    @pytest.mark.parametrize("call", [
        "requests.get('http://x')",
        "pd.read_csv('data.csv')",
        "open('secret.csv')",
        "urllib.request.urlopen('http://x')",
        "read_csv('a.csv')",  # from pandas import read_csv 风格
    ])
    def test_external_data_is_error(self, call):
        code = f'''
def init(context):
    context.s1 = "000001.XSHE"

def handle_bar(context, bar_dict):
    d = {call}
    order_shares(context.s1, 100)
'''
        findings = check_ast(code)
        assert RuleId.AST_EXTERNAL_DATA in _rules(findings)
        f = next(f for f in findings if f.rule == RuleId.AST_EXTERNAL_DATA)
        assert f.severity is Severity.ERROR

    @pytest.mark.parametrize("call", [
        "config.get('key')",
        "positions.get(context.s1)",
        "params.get('fast', 5)",
        "cache.open('x')",        # 属性调用 open 不等于内建 open
        "series.get(0)",
    ])
    def test_common_methods_not_flagged(self, call):
        """防误报:普通 .get()/.open() 方法调用不得命中外部数据规则(code-review 修复回归)。"""
        code = f'''
def init(context):
    context.params = {{"fast": 5}}
    context.s1 = "000001.XSHE"

def handle_bar(context, bar_dict):
    d = {call}
    prices = history_bars(context.s1, 20, "1d", "close")
    if prices is not None:
        order_shares(context.s1, 100)
'''
        findings = check_ast(code)
        assert RuleId.AST_EXTERNAL_DATA not in _rules(findings)

    def test_history_bars_in_before_trading_is_error(self):
        code = '''
def init(context):
    pass

def before_trading(context):
    prices = history_bars(context.s1, 5, "1d", "close")

def handle_bar(context, bar_dict):
    pass
'''
        findings = check_ast(code)
        assert RuleId.AST_BEFORE_TRADING_PRICE in _rules(findings)


class TestBeforeTrading:
    def test_bar_dict_in_before_trading_is_error(self):
        code = '''
def init(context):
    pass

def before_trading(context):
    x = bar_dict[context.s1].close
    if x > 0:
        order_shares(context.s1, 100)

def handle_bar(context, bar_dict):
    pass
'''
        findings = check_ast(code)
        assert RuleId.AST_BEFORE_TRADING_PRICE in _rules(findings)

    def test_snapshot_in_open_auction_is_error(self):
        code = '''
def init(context):
    pass

def open_auction(context):
    snap = current_snapshot(context.s1)
    order_shares(context.s1, 100)

def handle_bar(context, bar_dict):
    pass
'''
        findings = check_ast(code)
        assert RuleId.AST_BEFORE_TRADING_PRICE in _rules(findings)


class TestGlobalFit:
    def test_fit_is_warning(self):
        code = '''
def init(context):
    model.fit(X, y)

def handle_bar(context, bar_dict):
    pass
'''
        findings = check_ast(code)
        assert RuleId.AST_GLOBAL_FIT in _rules(findings)
        f = next(f for f in findings if f.rule == RuleId.AST_GLOBAL_FIT)
        assert f.severity is Severity.WARNING


class TestStructure:
    def test_missing_handle_bar_is_error(self):
        findings = check_ast("def init(context):\n    pass\n")
        assert RuleId.AST_SYNTAX in _rules(findings)

    def test_syntax_error_is_error(self):
        findings = check_ast("def init(context)\n  pass")
        f = next(f for f in findings if f.rule == RuleId.AST_SYNTAX)
        assert f.severity is Severity.ERROR

    def test_order_in_init_is_warning(self):
        code = '''
def init(context):
    order_shares("000001.XSHE", 100)

def handle_bar(context, bar_dict):
    pass
'''
        findings = check_ast(code)
        assert RuleId.AST_ORDER_API_MISUSE in _rules(findings)
        f = next(f for f in findings if f.rule == RuleId.AST_ORDER_API_MISUSE)
        assert f.severity is Severity.WARNING
