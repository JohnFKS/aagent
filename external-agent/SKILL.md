---
name: aquant-quant
description: 当用户要求编写/回测/优化/验证 A 股量化策略,或提到 AQ-Platform/AQ-Platform MCP 量化工具时使用。通过 MCP 工具完成策略编辑→生成→回测→优化→验证全流程。
---

# AQ-Platform A 股量化策略方法论

通过 AQ-Platform 的 MCP 工具完成「策略编辑→生成→回测→优化→验证」全流程。分工原则:**策略文件用你自己的文件工具读写;校验/回测/优化/验证等重活一律交给 MCP 工具**,不要在本地复现回测逻辑。所有工具依赖平台 API 已运行(地址由 AQUANT_API_BASE 控制,默认 http://127.0.0.1:8000)。

## 0. 前置检查(每次会话开始,顺序执行)

1. `platform_workflow_guide()` — 读取平台官方七步工作流与工具契约;若与本文件有出入,以工具返回为准。
2. `platform_health()` — 确认后端可用(返回 status/version)。失败时按返回文本处置:连接失败 → 请用户启动 API;401 → 请用户检查 AQUANT_MCP_TOKEN。前置检查未通过不得进入后续步骤。

## 1. 七步工作流

### 第 1 步:初始化项目目录

外部平台选中的文件夹即项目目录,约定:

```
~/aquant-project/
├── strategies/   # 策略 .py,命名 <策略名>_<标的代码>.py,如 ma_cross_000001.py
└── reports/      # 报告 JSON/MD,文件名带日期,如 2026-11-15_ma_cross_000001_backtest.md
```

目录不存在则用文件工具创建(若项目带 README 说明,先读并遵循);每完成一个阶段,把结果落盘 reports/。

### 第 2 步:编写策略代码(RQAlpha 6.5.0 日线)

约束清单(违反即被校验 422 拦截):

- 骨架:`init(context)` + `handle_bar(context, bar_dict)`;标的用 order_book_id(深交所 `.XSHE`、上交所 `.XSHG`,如 `000001.XSHE`);
- `history_bars(...)` 返回 **ndarray**:直接切片/`.mean()` 运算,**不要当 DataFrame 用**(实测高频误用点);
- 持仓查询必须带方向:`context.portfolio.get_position(code, POSITION_DIRECTION.LONG)`,需 `from rqalpha.const import POSITION_DIRECTION`;
- 信号只用截至当前 bar 的数据:禁未来函数 `shift(-n)`、禁墙钟 `datetime.now()`、禁 requests/akshare 等策略内拉外部数据、禁 `before_trading` 引用当日行情、禁全局 `fit()` 过拟合模式;
- 不得配置激进参数(零滑点/关闭 T+1/费率<1)——平台强制保守默认(0.2% 滑点、万八佣金+千一印花税、T+1、成交量占比 ≤20%),只能给更保守的覆盖。

合规骨架(可直接作为起点,存为 `strategies/ma_cross_<代码>.py`):

```python
from rqalpha.const import POSITION_DIRECTION


def init(context):
    context.s1 = "000001.XSHE"
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

### 第 3 步:校验到 0 error

`platform_validate_code(code=<策略源码>, start=?, end=?)` → `{passed, error_count, warning_count, findings, summary}`(start/end 可选,提供后做含回测配置的完整校验)。

- `error_count` 必须为 0 才能回测;HTTP 422 或 `passed=false` 时,逐条按 `findings[].rule` 修改策略文件后重新调用(常见:shift(-1) 未来函数、T+1 违规、零滑点配置);
- warning 不拦截,但须向用户转述。

### 第 4 步:回测并读指标诊断

`platform_run_backtest(code, start, end, cash=?)` → `{passed, verdict, metrics, trades_count}`,start/end 必填(YYYY-MM-DD)。

metrics 关键字段:`total_returns` / `annualized_returns` / `benchmark_total_returns` / `sharpe` / `max_drawdown` / `volatility` / `alpha` / `beta` / `turnover` / `win_rate` / `total_commission` / `total_tax` / `trading_days` / `unsafe`。

- 422 = 回测前硬门禁再拦:回到第 3 步修 findings;
- 400 = bundle 缺失或参数问题:按返回的构建指引处理;
- 回测前后可用 `platform_get_kline(oid, start, end)` 观察量价形态、确认标的在数据 bundle 内(start/end 必填;oid 不在 bundle → 400)。

逐项诊断(五类症状 → 处理方向):

| 症状 | 判断 | 处理方向 |
|---|---|---|
| `trades_count = 0` | 信号从未触发 | 放宽阈值/换标的或区间(用 platform_get_kline 核实行情)/换逻辑 |
| `turnover` 过高 | 频繁换仓,成本侵蚀 | 加趋势过滤、降低调仓频率 |
| `total_commission + total_tax` 占收益比例大 | 成本主导 | 降频、提高单次调仓幅度 |
| `max_drawdown` 偏大 | 缺风控 | 加止损、降低仓位上限 |
| `win_rate` 低 | 信号质量差 | 信号过滤、调整出场逻辑 |

诊断结论落盘 reports/,再与用户确认是否优化。

### 第 5 步:AI 优化(推荐)

`platform_optimize(code, start, end, rounds=2, cash=?, overrides=?)` → `{improved, best: {code, summary}, baseline, rounds, elapsed_seconds}`;rounds 取值 1~5,默认 2,连续 2 轮无改进自动早停。

- **耗时数分钟**(每轮 = 1 次 LLM 生成 + 2 次回测),调用前告知用户,期间勿中断重试(工具读超时已放宽);
- 依赖平台侧已配置 `AQUANT_LLM_*`;未配置时为模板兜底,优化无实际意义;
- `improved=true`:**用文件工具把 `best.code` 回写策略文件**,再回第 4 步复测确认;
- `improved=false`:无改进,保留原代码并如实告知用户。

### 第 6 步:分层验证(防过拟合,必做)

`platform_verify_layered(code, start, end, is_ratio=0.7, overrides=?)` → `{status: pass|warning|fail, comparison, is_metrics, oos_metrics, checks, report, paper_checklist, ...}`。

- **回测好看 ≠ 策略可用**:IS→OOS 对比 + 随机基准对照是唯一防过拟合闸门,交付前必做(优化产出的 best.code 也要再验一次);
- start/end 跨度须 ≥ 40 个交易日(建议取比第 4 步更长的区间);is_ratio 限 0.5~0.9;
- `status=fail`(OOS 收益为负/回撤放大/夏普腰斩)→ 回第 4/5 步继续优化,通过后如实报告,不得粉饰;
- `paper_checklist`(模拟盘引导清单)必须原样转告用户:回测通过 ≠ 可以实盘。

### 第 7 步:竞技与归档

多策略对比:`platform_arena(candidates=[{"name": "MA金叉", "code": "..."}, ...](1~6 个), start, end)` → 按 (年化, 夏普) 降序排名;校验不过的候选自动 rejected、执行失败只淘汰自己;候选名必须唯一。提交前先用 `platform_validate_code` 预检各候选可提高入围率;冠军代码建议再做第 6 步防过拟合。

归档:最终采用的代码确保在 strategies/;把校验/回测/优化/验证/竞技场结果整理落盘 reports/(文件名带日期与阶段),并向用户汇报最终结论。

## 2. 错误处理

| 返回 | 含义 | 动作 |
|---|---|---|
| HTTP 422 + findings/verdict | Layer 1/3 校验拦截 | 逐条修 findings 后重试;**不得绕过** |
| HTTP 400 + bundle 构建指引 | 数据 bundle 缺失 | 按指引构建 bundle 或设 AQUANT_BUNDLE_PATH;需用户操作时明确告知 |
| HTTP 400 + 其他中文提示 | 参数问题(区间<40 交易日、oid 不在 bundle、候选名重复等) | 按提示改参数重试 |
| HTTP 401 | token 无效/缺失 | 请用户检查 AQUANT_MCP_TOKEN |
| HTTP 403 | scope 不足(readonly token 调了写接口) | 请用户改用 readwrite token |
| HTTP 429 | 触发限流 | 等待后重试,并降低调用频率 |
| HTTP 502 | 平台侧 LLM 服务不可用 | 稍后重试;请用户检查平台 AQUANT_LLM_* 配置 |
| HTTP 500 | 服务端回测执行失败 | 重试一次;仍失败请用户查服务端日志 |
| 「无法连接量化平台 API」文本 | 后端未启动或地址不对 | 请用户启动 API(如 `uvicorn aquant.api.main:app --port 8000`)或核对 AQUANT_API_BASE |

工具返回错误均为可读中文文本,不抛堆栈——按文本自纠后重试,不要向用户原样转发堆栈。

## 3. 纪律

1. **不绕过校验**:校验是平台正确性的核心;不尝试禁用校验、不使用 `AQUANT_ALLOW_UNSAFE` 豁免。
2. **不承诺收益**:回测仅供学习研究、不构成投资建议;对 verify=fail、零成交等坏结果如实呈现。
3. **报告落盘 reports/**:文件名带日期与阶段(如 `2026-11-15_ma_cross_000001_verify.md`);verify 报告必须包含 paper_checklist 与免责声明。
4. **文件与回测一致**:所有代码改动先写入 strategies/ 下文件,再以文件内容调用工具;optimize 采纳的 best.code 必须回写文件。

> 注:工具名与参数已与 MCP server(src/aquant/mcp_server/server.py)的注册一致;若客户端实际暴露的工具签名与本文件不符,以实际为准。
