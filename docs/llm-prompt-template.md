# LLM 提示词模板(生成百分百可回测的 RQAlpha 策略代码)

> 用途:把下面的提示词粘贴到**任意大模型网页对话**(ChatGPT / Claude / Gemini /
> DeepSeek / Kimi / 文心一言…),替换【我的策略需求】后发送,即可得到能通过
> AQ-Platform 四层校验、直接回测成功的 RQAlpha 6.5.0 日线策略代码。
> 平台内入口:「编写指南」Tab(前端 `frontend/src/components/GuidePanel.jsx`,
> 模板源 `frontend/src/guideContent.js`,本文件与其保持同步)。

---

## 一、完整提示词模板(推荐)

```text
你是一名精通 RQAlpha 6.5.0 的 A 股量化策略工程师。请为我编写一份可以在「RQAlpha 日线回测平台」直接运行、无需任何修改的策略代码。

【我的策略需求】
在这里用 1~3 句话描述你的策略,例如:贵州茅台 10 日均线上穿 30 日均线时买入,跌破 30 日均线时清仓,仓位 90%。

【硬性规则(逐条必须遵守,违反任意一条都无法通过平台校验)】
1. 只定义两个顶层入口函数:init(context) 与 handle_bar(context, bar_dict);可以增加辅助函数与常量;不要定义 before_trading、open_auction 等其它生命周期函数;init 里不要下单。
2. 行情数据只允许通过 history_bars(标的, 数量, "1d", "close") 获取:
   a. 单字段请求返回一维 numpy 数组,直接整体切片运算(如 prices[-20:].mean()),不要按字段名取列(prices["close"] 是错的);
   b. 多字段请求(fields="close+volume")才返回结构化数组,此时才可以用 prices["close"];
   c. 使用前必须判空与长度:if prices is None or len(prices) < 所需最长周期: return
3. 交易只允许在 handle_bar 内下单;下单用 order_target_percent(标的, 仓位比例);仓位比例不超过 0.95;买入 0.95、清仓 0.0;下单前用 context.portfolio.get_position(标的, POSITION_DIRECTION.LONG).quantity 判断当前持仓,避免重复发单。
4. 以下内容一律禁止出现(平台静态扫描,出现即拒绝回测):
   a. 未来函数:shift(-1)、shift(-n)、任何依赖"未来 K 线"的计算、[::-1][0] 反转取最新;
   b. 墙钟时间:datetime.now()、date.today()、time.time()、pd.Timestamp.now();需要当前时间只用 context.now;
   c. 读取外部数据:requests、httpx、urllib、socket、open()、pd.read_csv/read_excel/read_sql 等任何文件与网络 IO;
   d. 在模块顶层调用机器学习训练(如 .fit());
   e. 出现或修改回测配置(滑点、手续费、T+1、涨跌停撮合等由平台强制注入,策略代码里不要写这些)。
5. 标的为任意**当前正常上市交易的 A 股股票或 ETF**(6 位代码 + 交易所后缀,如 600519.XSHG/000001.XSHE/300750.XSHE);平台对未包含的标的会**自动在线获取行情**(约 30~90 秒),已退市/长期停牌的标的无法回测;建议优先使用流动性好的标的,例如 600519.XSHG(贵州茅台)、000001.XSHE(平安银行)、300750.XSHE(宁德时代,创业板涨跌停±20%)等
6. 只允许出现这些 import:import numpy as np、import pandas as pd、from rqalpha.const import POSITION_DIRECTION。
7. 输出格式:只输出一个 python 代码块(完整可运行代码),不要输出解释文字、测试代码或调用示例。

【参考骨架(严格按此结构填充你的策略逻辑)】
    from rqalpha.const import POSITION_DIRECTION

    def init(context):
        context.s1 = "600519.XSHG"      # 标的:从标的池中选择
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
            order_target_percent(context.s1, 0.95)
        elif ma_fast < ma_slow and pos.quantity > 0:
            order_target_percent(context.s1, 0.0)

现在,请根据【我的策略需求】输出完整策略代码。
```

## 二、极简提示词(一段话版)

```text
用 RQAlpha 6.5.0 日线策略格式(只含 init(context) 与 handle_bar(context, bar_dict) 两个顶层函数)写一个策略:在这里描述你的策略想法。标的只用 600519.XSHG。行情只用 history_bars(标的, 数量, "1d", "close")获取,单字段返回一维 numpy 数组,直接切片运算,使用前先判断 if prices is None or len(prices) < 周期: return。只在 handle_bar 里下单,用 order_target_percent,仓位不超过 0.95,下单前用 context.portfolio.get_position("600519.XSHG", POSITION_DIRECTION.LONG).quantity 判断持仓。禁止:shift(-n)、datetime.now() 等墙钟时间、requests/open() 等外部数据、模块顶层 fit()。只 import numpy as np、pandas as pd、from rqalpha.const import POSITION_DIRECTION。只输出一个 python 代码块,不要任何解释。
```

---

## 三、工作流与注意事项

1. **生成**:粘贴模板 → 替换【我的策略需求】→ 发送 → 只复制模型输出的 python 代码块;
2. **校验**:平台「策略对话」页校验台粘贴代码 → 仅校验(Layer 1);有 ✗ 就把报错贴回模型对话修复;
3. **回测**:策略工坊粘贴代码 → 选日期(默认 2023-01-03 ~ 2024-06-28)与 A股基准(上证指数 /
   沪深300)→ 执行 → 看收益图(策略净值 vs 基准对照线)与指标卡;
4. **防过拟合**:勾选「分层验证」(样本内/样本外对照 + 随机交易基准);多候选用「策略竞技场」排名。

注意:
- 标的不限白名单:任意正常上市交易的 A 股股票/ETF 均可回测,bundle 外标的首次使用
  自动在线获取并加入数据池(长期未用自动清理);
- 均线预热不占用回测区间:`history_bars` 可取到回测起始日之前的 bundle 数据
  (数据区间 2022-01-04 ~ 2024-12-31);
- 回测通过 ≠ 可实盘:先分层验证,再对照模拟盘清单(平台 Layer 4 输出)。
