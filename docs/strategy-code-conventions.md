# 策略代码编写规范（AQ-Platform / RQAlpha 6.5.0）

适用范围：通过网页「策略工坊」「策略对话」或 MCP 工具提交到本平台的全部策略源码。
规范目标：保证样本内/样本外结果可复现、无未来信息、成本假设保守，避免“回测很美、实盘亏钱”。

## 1. 框架与结构

- 回测引擎：RQAlpha 6.5.0；频率固定 `1d`；账户仅 `STOCK`。
- 顶层必须定义两个入口，且代码必须可被 Python AST 解析：
  - `init(context)`：只做一次性初始化，如设置标的、均线窗口。
  - `handle_bar(context, bar_dict)`：每个交易日收盘后执行，读数据、判断、下单。
- 允许从 `rqalpha.const` 导入常量，例如 `POSITION_DIRECTION.LONG`。
- LLM 生成策略时只输出一个 ` ```python ` 代码块，块外不要解释文字。

## 2. 数据来源（硬性要求）

策略内只能通过平台数据接口取数：

- 允许：`history_bars(oid, count, frequency, fields)`、`get_price(...)` 等 RQAlpha 接口。
- 禁止：
  - 网络请求：`requests`、`httpx`、`urllib`、`socket`。
  - 本地文件：`open()`、`pd.read_csv()`、`read_excel()`、`read_sql()`、`read_parquet()`、`read_hdf()`。
  - 任何绕过 bundle 的“自己读数据”行为。

命中规则 `ast.external_data` 直接 **ERROR**，拒绝回测。

## 3. 时间口径（硬性要求）

- 禁止墙钟时间：`datetime.now()` / `today()` / `utcnow()`、`date.today()`、`time.time()`、`time.localtime()`、`pd.Timestamp.now()` 等。
- 回测时钟只能用 `context.now`。
- 禁止未来函数：`.shift(-n)` 或等价的向未来取数，命中 `ast.shift_negative` 为 **ERROR**。
- 反转切片 `[::-1]` 后用 `[0]` 当“最新值”属于脆弱模式，命中 `ast.reverse_slice` 为 **WARNING**，需人工确认。

## 4. 生命周期与下单

- 下单只发生在 `handle_bar()`。
- `init()` 中禁止 `order_*` / `buy` / `sell`，命中 `ast.order_api_misuse` 为 **WARNING**。
- `before_trading()` / `open_auction()` 运行在当日行情产生之前，禁止引用：
  - `bar_dict`
  - `current_snapshot` / `get_snapshot()` / `snapshot`
  - `history_bars()` / `get_price()`

  命中 `ast.before_trading_price` 为 **ERROR**。
- 不得假设 T+0 回转；平台强制 `stock_t1=True`。
- 目标仓位建议不超过 95%，保留现金缓冲。
- 模型训练若使用 `.fit()` / `.fit_transform()`，命中 `ast.global_fit` 为 **WARNING**；必须确认只用历史窗口滚动训练，否则就是严重未来函数。

## 5. 平台注入的回测配置

策略代码不要覆盖以下参数；平台会在 `defaults.yaml` 中强制校验：

| 项目 | 要求 |
| --- | --- |
| `stock_t1` | `True` |
| `price_limit` | `True` |
| `volume_limit` | `True`，`volume_percent <= 0.25` |
| 滑点 | 非零，默认比例滑点 0.2% |
| 佣金倍率 | `>= 1`（万八基准） |
| 印花税倍率 | `>= 1`（千一单边） |
| `signal` | `False`，禁止跳过撮合 |
| 撮合方式 | 仅 `current_bar` / `vwap` |
| 账户类型 | 仅 `STOCK` |

在策略或覆盖参数中设置 `slippage=0`、佣金倍率 `<1`、`stock_t1=False`、`volume_limit=False`、`signal=True` 等均会被 ERROR 拦截。

## 6. 合规示例

```python
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

    from rqalpha.const import POSITION_DIRECTION
    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if ma_fast > ma_slow and pos.quantity == 0:
        order_target_percent(context.s1, 0.9)
    elif ma_fast < ma_slow and pos.quantity > 0:
        order_target_percent(context.s1, 0.0)
```

## 7. 提交前自查清单

1. 是否只有 `init` / `handle_bar` 及必要的辅助函数？
2. 是否没有 `shift(-n)`、`now()`、`open()`、`requests`、全样本 `.fit()`？
3. 是否所有下单都在 `handle_bar` 且目标仓位 ≤95%？
4. 是否使用 `history_bars` / `get_price` 而不是自己读文件？
5. 是否已通过 `platform_validate_code` 或网页校验台？
6. 是否用 `platform_verify_layered` 做过样本外 + 随机基准验证？

> 注意：AST 检查是启发式的。命中规则一定拦截；未命中不等于绝对安全，仍需结合分层验证和交易逻辑人工复核。详细规则实现见 `src/aquant/validation/ast_rules.py`。
