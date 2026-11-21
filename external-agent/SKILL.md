---
name: aquant-quant
description: 当用户要求编写/回测/优化/验证 A 股量化策略,或提到 AQ-Platform/AQ-Platform MCP 量化工具时使用。通过 MCP 工具完成「策略编写→校验→回测→诊断迭代→验证→汇报」全流程;平台无需配置 LLM(策略思考由你——agent 框架内的大模型——完成)。
---

# AQ-Platform A 股量化策略方法论(agent 自主模式)

分工原则:**策略代码的"思考与编写"由你完成;校验/回测/诊断/排名/数据等确定性
重活一律交给 MCP 工具**。平台侧无需配置 LLM(AQUANT_LLM_*),direct 模式(默认)
也无需启动 REST 服务 —— 工具进程内直调,与 Web 端同一套四层校验与保守参数。

## 0. 前置检查(每次会话开始)

1. `platform_workflow_guide()` — 读官方工作流;若与本文件有出入,以工具返回为准。
2. `platform_health()` — 确认服务可用(direct 模式恒可用;proxy 模式失败时按返回
   文本处置:连接失败 → 请用户启动 API 或改用 direct 模式;401 → 检查 token)。

## 1. 工作流

### 第 1 步:初始化项目目录

外部平台选中的文件夹即项目目录,约定:

```
~/aquant-project/
├── strategies/   # 策略 .py,命名 <策略名>_<标的代码>.py,如 ma_cross_600519.py
└── reports/      # 报告 MD,文件名带日期,如 2026-11-21_ma_cross_600519_backtest.md
```

### 第 2 步:读契约与标的池(写码前必做)

- `platform_strategy_contract()` — 代码契约(入口函数 / history_bars 一维数组语义 /
  下单规范 / 禁令清单)+ 参考骨架 + 当前标的池;
- `platform_list_instruments()` — 标的池(oid/名称)、可用基准、数据区间;
  标的与回测日期都从这里选;区间超出数据范围时平台会自动拉取最新行情(首次约 30~90s)。

### 第 3 步:编写策略代码

按契约把用户需求写成完整策略,存入 strategies/(文件内容与后续工具调用保持一致)。
要点(详见契约返回):只含 `init` + `handle_bar`;`history_bars` 单字段返回一维数组
直接切片运算;只在 handle_bar 下单、仓位 ≤0.95、先查持仓;禁未来函数/墙钟时间/
外部数据/顶层 fit();只 import numpy/pandas/POSITION_DIRECTION。

合规骨架(可直接作为起点,存为 `strategies/ma_cross_<代码>.py`):

```python
from rqalpha.const import POSITION_DIRECTION


def init(context):
    context.s1 = "600519.XSHG"
    context.fast = 5
    context.slow = 20


def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, context.slow + 2, "1d", "close")
    if prices is None or len(prices) < context.slow:
        return
    ma_fast = prices[-context.fast:].mean()
    ma_slow = prices[-context.slow:].mean()
    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if ma_fast > ma_slow and pos.quantity == 0:
        order_target_percent(context.s1, 0.95)
    elif ma_fast < ma_slow and pos.quantity > 0:
        order_target_percent(context.s1, 0.0)
```

### 第 4 步:校验到 0 error

`platform_validate_code(code, start=?, end=?)` → `{passed, findings, summary}`。

- `error_count` 必须为 0 才能回测;`passed=false` 时逐条按 `findings[].rule` 修改
  文件后重试(高频问题:shift(-1) 未来函数、一维 ndarray 按字段名取列、T+1 违规);
- warning 不拦截,但须向用户转述。

### 第 5 步:回测并汇报

`platform_run_backtest(code, start, end, cash=?, benchmark=?)` → JSON + **人读
Markdown 报告**(可直接引用汇报)。benchmark:000001.XSHG 上证指数 / 000300.XSHG
沪深300 / bundle 内任意标的 / 'none' 关闭,缺省上证指数。

- 返回含 metrics、portfolio_series(净值摘要)、benchmark_series、trades、
  benchmark_label;报告含基准对比、最大回撤区间、成本与提示;
- 执行崩溃(execution_error):把错误原文带回上下文修码重试(典型:一维 ndarray
  按字段名取列、标的当日停牌);
- metrics 关键字段:`total_returns` / `annualized_returns` / `benchmark_total_returns` /
  `sharpe` / `max_drawdown` / `turnover` / `win_rate` / `total_commission` / `total_tax`。

### 第 6 步:诊断迭代(你当优化大脑)

`platform_diagnose_strategy(code, start, end, benchmark=?)` — 回测 + 规则诊断
(无成交 / 换手过高 / 成本侵蚀 / 回撤偏大 / 胜率偏低)。

| 线索 | 处理方向 |
|---|---|
| 无成交 | 放宽阈值 / 换标的或区间(先 platform_get_kline 核实行情)/ 换逻辑 |
| 换手过高 | 加趋势过滤、降低调仓频率 |
| 成本占比大 | 降频、提高单次调仓幅度 |
| 回撤偏大 | 加止损、降低仓位上限 |
| 胜率偏低 | 信号过滤、调整出场逻辑 |

迭代循环:**你改码 → 第 4 步校验 → 第 5 步复测 → 把每轮指标变化告诉用户**。
(可选)平台侧 AI 优化 `platform_optimize` 需要配置平台 LLM;未配置时返回 503 指引,
走本步自主循环即可。

### 第 7 步:对比排名与防过拟合(交付前必做)

- 多候选:`platform_rank_strategies(candidates=[{"name", "metrics"}…])` 按
  (夏普, 年化, -回撤)排名;竞技场 `platform_arena` 可让多份完整代码同场回测;
- 防过拟合:`platform_verify_layered(code, start, end, is_ratio=0.7)` — IS→OOS 对照 +
  随机基准;**回测好看 ≠ 策略可用**,交付前必做;`status=fail` 时回第 6 步继续,
  通过后如实报告,不得粉饰;`paper_checklist`(模拟盘清单)必须原样转告用户;
- 会话工具(可选):`platform_session_create` + `platform_session_edit_code` +
  `platform_session_backtest` + `platform_session_rollback` / `platform_session_diff`
  做版本化管理(direct 下可用,不依赖平台 LLM)。

### 第 8 步:汇报与归档

把 report_md 与关键指标用你自己的口吻整理给用户:策略思路、总收益与相对基准超额、
最大回撤区间、成本、诊断与迭代过程、验证结论、下一步建议。结果落盘 reports/
(文件名带日期与阶段);最终代码确保在 strategies/。

## 2. 错误处理

所有工具失败都返回**可读中文文本**(不抛堆栈),按文本自纠后重试:

| 返回 | 含义 | 动作 |
|---|---|---|
| 422 + findings/verdict | Layer 1/3 校验拦截 | 逐条修 findings 后重试;**不得绕过** |
| 422 + execution_error | 静态校验通过但撮合崩溃 | 按错误原文修码(高频:一维 ndarray 取列) |
| 422 + "回测区间 … 无行情数据" | 区间越界(正常会被自动扩展兜底) | 若出现,说明自动扩展被关闭/超限,调整日期或手动重建 bundle |
| 400 + bundle 构建指引 | 数据 bundle 缺失 | 按指引构建或设 AQUANT_BUNDLE_PATH |
| 400 + 基准不在 bundle | benchmark oid 非法 | 改用 000001.XSHG/000300.XSHG 或池内标的 |
| 401/403/429 | token 无效 / scope 不足 / 限流 | 请用户检查 token 配置;降频重试 |
| 503 + 自主闭环指引 | 平台未配置 LLM 而调了 chat/turn/optimize | 改用本文件的自主闭环工具链 |
| 「无法连接量化平台 API」 | proxy 模式且后端未启动 | 启动服务或切回 direct 模式 |

## 3. 纪律

1. **不绕过校验**:不尝试禁用校验、不使用 `AQUANT_ALLOW_UNSAFE` 豁免。
2. **不承诺收益**:回测仅供学习研究、不构成投资建议;verify=fail、零成交等坏结果如实呈现。
3. **报告落盘 reports/**:文件名带日期与阶段;verify 报告必须包含 paper_checklist 与免责声明。
4. **文件与回测一致**:所有代码改动先写入 strategies/ 文件,再以文件内容调用工具。
5. **诚实汇报**:把每轮回测结果(含变差)告诉用户;采纳与否由用户决定。

> 注:工具名与参数已与 MCP server(src/aquant/mcp_server/server.py)注册一致;
> direct 模式的端点语义在 src/aquant/mcp_server/service.py;若实际签名不符,以工具返回为准。
