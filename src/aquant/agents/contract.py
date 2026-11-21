"""Agent 策略编写契约与工作流指南(供 MCP 工具返回给外部 agent 的大模型)。

设计动机:外部 agent 框架(Claude Code / ZCode / Codex…)自带大模型,平台侧
**不配置 LLM** 也能完成「策略编辑 → 校验 → 回测 → 优化迭代 → 汇报」全流程 ——
平台只做确定性的校验/回测/诊断,策略代码的"思考"由 agent 自身模型完成。

本模块是 agent 模型的"脑粮":把平台的代码契约(docs/strategy-code-conventions.md
与 Layer 1 校验规则 ast_rules 的约束)压缩为可直接注入对话的文本。与前端
「编写指南」Tab(frontend/src/guideContent.js)保持同源,修改需双侧同步。

:py:data:`CONTRACT_MARKDOWN` —— 代码契约 + 标的池说明 + 参考骨架;
:py:data:`WORKFLOW_MARKDOWN` —— 七步自主工作流(校验-回测-诊断-迭代-汇报);
:py:func:`stock_pool_text` —— 当前 bundle 的标的池(动态读取,含真实指数)。
"""

from __future__ import annotations

CONTRACT_MARKDOWN = """\
# AQ-Platform 策略代码契约(RQAlpha 6.5.0 日线)

## 硬性规则(违反任意一条都会被 Layer 1 校验 422 拒绝)
1. 只定义两个顶层入口函数:`init(context)` 与 `handle_bar(context, bar_dict)`;
   可加辅助函数与常量;不要定义 before_trading / open_auction 等其它生命周期函数;
   init 里不要下单。
2. 行情只允许 `history_bars(标的, 数量, "1d", fields)`:
   - 单字段(如 "close")返回**一维 numpy 数组**,直接切片运算
     (如 `prices[-20:].mean()`),不要按字段名取列;
   - 多字段(`fields="close+volume"`)才返回结构化数组,此时才可用 `prices["close"]`;
   - 使用前必须判空判长:`if prices is None or len(prices) < 所需周期: return`。
3. 只在 `handle_bar` 内下单;推荐 `order_target_percent(标的, 仓位)`;仓位 ≤ 0.95;
   买入 0.95 / 清仓 0.0;下单前用
   `context.portfolio.get_position(标的, POSITION_DIRECTION.LONG).quantity` 判断持仓。
4. 一律禁止(静态扫描,出现即拒):
   - 未来函数:`shift(-n)`、任何依赖未来 K 线的计算、`[::-1][0]`;
   - 墙钟时间:`datetime.now()` / `date.today()` / `time.time()` / `pd.Timestamp.now()`
     (需要当前时间只用 `context.now`);
   - 外部数据:requests / httpx / urllib / socket / open() / pd.read_* 等一切 IO;
   - 模块顶层调用机器学习训练(如 `.fit()`);
   - 出现或修改回测配置(滑点/手续费/T+1/涨跌停由平台强制注入,更激进值会被拒绝)。
5. 标的只能用当前 bundle 内的合约(用 `platform_list_instruments` 查询,结果里
   会给出标的池与当前数据区间);选其它标的无法回测。
6. 只允许这些 import:`import numpy as np`、`import pandas as pd`、
   `from rqalpha.const import POSITION_DIRECTION`。
7. 输出:完整可运行的单文件策略代码,不要解释性文字、测试代码或调用示例。

## 参考骨架(在此结构上填充策略逻辑)
```python
from rqalpha.const import POSITION_DIRECTION

def init(context):
    context.s1 = "600519.XSHG"      # 标的:用 platform_list_instruments 查询
    context.fast = 10               # 快线周期(按需求修改)
    context.slow = 30               # 慢线周期(按需求修改)

def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, context.slow + 2, "1d", "close")
    if prices is None or len(prices) < context.slow:
        return
    ma_fast = prices[-context.fast:].mean()
    ma_slow = prices[-context.slow:].mean()
    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if ma_fast > ma_slow and pos.quantity == 0:
        order_target_percent(context.s1, 0.95)   # 买入(仓位 ≤ 0.95)
    elif ma_fast < ma_slow and pos.quantity > 0:
        order_target_percent(context.s1, 0.0)    # 清仓
```

## 提示
- 均线预热不占回测区间:`history_bars` 可取到回测起始日之前的 bundle 数据;
- 回测默认强制 0.2% 滑点、万八佣金 + 千一印花税、T+1、涨跌停与成交量限制,
  指标是贴近实盘的保守口径;不要在代码里试图绕过;
- 422 返回的 findings 会逐条说明违规规则与行号,按条修正即可。
"""

WORKFLOW_MARKDOWN = """\
# Agent 自主工作流(平台无需配置 LLM,由你(agent 框架内的大模型)担任策略大脑)

1. **调研**:`platform_list_instruments` 拿到标的池/数据区间/可用基准 →
   `platform_get_kline` 看标的行情形态(可选);
2. **写码**:按 `platform_strategy_contract` 的契约把用户需求写成完整策略
   (只输出契约允许的结构);
3. **校验**:`platform_validate_code` —— 若 findings 非空,按条修正后重试,
   直到 passed=true;
4. **回测**:`platform_run_backtest`(可选 benchmark 对照)—— 返回指标、
   净值曲线摘要、成交明细与人读报告(report_md);
   - 若返回 execution_error:把错误原文与修复指引带回你的上下文,修改代码后重试
     (典型:`history_bars` 单字段返回的一维 ndarray 被按字段名取列、标的当日停牌);
   - 若区间超出数据范围:平台会自动拉取最新行情扩展数据(首次约 30~90s),稍后重试即可;
5. **诊断迭代**:`platform_diagnose_strategy` 返回规则诊断线索
   (无成交/换手过高/成本侵蚀/回撤偏大/胜率偏低)+ 当前指标 ——
   **由你(而非平台 LLM)决定怎么改码**,改完重复 3~4;
6. **对比**:把多个候选的 metrics 喂给 `platform_rank_strategies`
   (按夏普/年化/回撤排名),挑出最优;防过拟合用 `platform_verify_layered`
   (样本内/样本外对照);
7. **汇报**:把 report_md 与关键指标用你自己的口吻整理给用户
   (结果解读、相对基准的超额、回撤与成本、下一步建议)。
   回测通过 ≠ 可实盘:分层验证通过 + 模拟盘清单走完之前,只作为研究结论陈述。

错误处理:所有工具失败都返回可读中文文本(不抛堆栈),按文本提示修正参数/代码
后重试即可;`platform_health` 可随时确认服务状态。
"""

_DEFAULT_POOL_TEXT = """\
标的池以 `platform_list_instruments` 实时返回为准(平台默认 bundle 内含
上证指数、沪深300 与多只流动性个股;数据区间见同一条返回)。"""


def stock_pool_text() -> str:
    """标的池文本(动态读 bundle;缺失时给通用说明)。"""
    try:
        from aquant.config.schema import default_bundle_path
        from aquant.data.extend import bundle_calendar
        from aquant.data.instruments import load_instruments

        bundle = default_bundle_path()
        insts = load_instruments(bundle)
        if not insts:
            return _DEFAULT_POOL_TEXT
        cal = bundle_calendar(bundle)
        lines = [f"当前数据区间:{cal[0]} ~ {cal[1]}(回测超出该区间会自动拉取最新行情)。"]
        stocks, indexes, others = [], [], []
        for oid, inst in insts.items():
            entry = f"{oid}({inst.get('symbol', '')})"
            t = inst.get("type")
            if t == "CS":
                stocks.append(entry)
            elif t == "INDX":
                indexes.append(entry)
            else:
                others.append(f"{entry}[{t}]")
        lines.append(f"标的池({len(stocks)} 只个股):{'、'.join(stocks)}")
        if indexes:
            lines.append(f"基准指数:{'、'.join(indexes)}")
        if others:
            lines.append(f"其它合约:{'、'.join(others)}")
        return "\n".join(lines)
    except Exception:
        return _DEFAULT_POOL_TEXT


def contract_text() -> str:
    """契约全文(附当前标的池)。"""
    return CONTRACT_MARKDOWN + "\n\n## 当前标的池与数据区间\n\n" + stock_pool_text()


def workflow_text() -> str:
    """工作流全文(附标的池提示)。"""
    return WORKFLOW_MARKDOWN + "\n\n---\n\n" + stock_pool_text()
