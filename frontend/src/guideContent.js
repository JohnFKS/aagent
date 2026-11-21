// 策略编写指南内容(与 GuidePanel.jsx 配套):
// - PROMPT_FULL:通用提示词模板 —— 用户复制到任意大模型网页对话(ChatGPT/Claude/
//   Gemini/DeepSeek/Kimi…),替换【策略需求】占位后即可得到能通过平台四层校验、
//   直接回测成功的 RQAlpha 日线策略代码。契约与 docs/strategy-code-conventions.md、
//   aquant.validation.ast_rules 的规则逐条对齐(未来函数/外部数据/墙钟时间禁令、
//   history_bars 单字段一维数组语义、下单 API 仅限 handle_bar 等)。
// - PROMPT_MINI:极简提示词(一段话版,适合移动端或快捷对话)。
// - SKELETON_CODE:参考骨架(与 strategies/demo_ma_cross.py 同构,可送入策略工坊)。
// 标的池必须与平台默认数据 bundle(~/.aquant/bundle-baostock,scripts/add_benchmark_index.sh
// 注入真实上证指数/沪深300)保持一致 —— 池外标的无法回测。

// 平台默认标的池(10 只,覆盖主板/创业板,波动特征互补)
export const STOCK_POOL = [
  { oid: '000001.XSHE', name: '平安银行', note: '主板' },
  { oid: '600519.XSHG', name: '贵州茅台', note: '主板·高价股' },
  { oid: '600036.XSHG', name: '招商银行', note: '主板' },
  { oid: '000333.XSHE', name: '美的集团', note: '主板' },
  { oid: '601318.XSHG', name: '中国平安', note: '主板' },
  { oid: '000858.XSHE', name: '五粮液', note: '主板' },
  { oid: '002415.XSHE', name: '海康威视', note: '主板' },
  { oid: '600276.XSHG', name: '恒瑞医药', note: '主板' },
  { oid: '300750.XSHE', name: '宁德时代', note: '创业板(±20%)' },
  { oid: '601012.XSHG', name: '隆基绿能', note: '主板' },
];

export const SKELETON_CODE = `from rqalpha.const import POSITION_DIRECTION

def init(context):
    context.s1 = "600519.XSHG"      # 标的:从标的池中选择
    context.fast = 10               # 快线周期(改成你的参数)
    context.slow = 30               # 慢线周期(改成你的参数)

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
`;

export const PROMPT_FULL = `你是一名精通 RQAlpha 6.5.0 的 A 股量化策略工程师。请为我编写一份可以在「RQAlpha 日线回测平台」直接运行、无需任何修改的策略代码。

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
5. 标的只能从下面的标的池中选(平台数据 bundle 只包含这些标的,选其它标的无法回测):
   000001.XSHE(平安银行)、600519.XSHG(贵州茅台)、600036.XSHG(招商银行)、000333.XSHE(美的集团)、601318.XSHG(中国平安)、000858.XSHE(五粮液)、002415.XSHE(海康威视)、600276.XSHG(恒瑞医药)、300750.XSHE(宁德时代,创业板涨跌停±20%)、601012.XSHG(隆基绿能)
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
`;

export const PROMPT_MINI = `用 RQAlpha 6.5.0 日线策略格式(只含 init(context) 与 handle_bar(context, bar_dict) 两个顶层函数)写一个策略:在这里描述你的策略想法。标的只用 600519.XSHG。行情只用 history_bars(标的, 数量, "1d", "close")获取,单字段返回一维 numpy 数组,直接切片运算,使用前先判断 if prices is None or len(prices) < 周期: return。只在 handle_bar 里下单,用 order_target_percent,仓位不超过 0.95,下单前用 context.portfolio.get_position("600519.XSHG", POSITION_DIRECTION.LONG).quantity 判断持仓。禁止:shift(-n)、datetime.now() 等墙钟时间、requests/open() 等外部数据、模块顶层 fit()。只 import numpy as np、pandas as pd、from rqalpha.const import POSITION_DIRECTION。只输出一个 python 代码块,不要任何解释。`;

// 契约速查表:允许 / 禁止(与平台 Layer 1 校验规则一一对应)
export const ALLOWED_ITEMS = [
  { title: '入口函数', detail: 'init(context)、handle_bar(context, bar_dict),可加辅助函数' },
  { title: '行情 API', detail: 'history_bars(oid, count, "1d", fields)、get_price(...)' },
  { title: '下单 API', detail: 'order_target_percent / order_shares / order_value(仅 handle_bar 内)' },
  { title: '持仓查询', detail: 'context.portfolio.get_position(oid, POSITION_DIRECTION.LONG)' },
  { title: '当前时间', detail: 'context.now(引擎驱动,非墙钟)' },
  { title: '导入', detail: 'numpy / pandas / rqalpha.const' },
];

export const FORBIDDEN_ITEMS = [
  { title: '未来函数', detail: 'shift(-n)、任何用到未来 K 线的计算、[::-1][0](警告)' },
  { title: '墙钟时间', detail: 'datetime.now()、date.today()、time.time()、pd.Timestamp.now()' },
  { title: '外部数据', detail: 'requests / httpx / urllib / socket / open() / pd.read_* 等一切 IO' },
  { title: '顶层训练', detail: '模块顶层调用 .fit()(过拟合模式)' },
  { title: '配置篡改', detail: '滑点 / 手续费 / T+1 / 涨跌停由平台强制注入,更激进值会被拒绝' },
  { title: '生命周期滥用', detail: 'before_trading 中引用当日行情;init 中下单' },
];

// 常见回测失败与修复
export const TROUBLE_ITEMS = [
  {
    symptom: '422 校验拦截:shift(-1) / 墙钟时间 / 外部数据',
    fix: '回到模型对话,把平台报错贴给它,要求按硬性规则重写;或删除对应代码。',
  },
  {
    symptom: '执行崩溃:一维 ndarray 按字段名取列(TypeError / KeyError)',
    fix: '单字段 history_bars 返回一维数组,直接 prices[-n:].mean();多字段请求才可 prices["close"]。',
  },
  {
    symptom: '标的无法交易 / 无行情',
    fix: '标的必须在标的池内(代码形如 600519.XSHG);确认拼写与交易所后缀。',
  },
  {
    symptom: '总收益 0、无成交',
    fix: '区间内信号未触发(真实结果):放宽阈值、缩短均线周期,或改用竞技场多候选对比。',
  },
  {
    symptom: '激进参数被拒(422):零滑点 / 关 T+1',
    fix: '平台强制保守参数;想更保守可在工坊高级区自选,更激进的值不被接受。',
  },
  {
    symptom: '回测区间越界(超出 2022-01-04 ~ 2024-12-31)',
    fix: '平台默认 bundle 数据区间为 2022-01-04 ~ 2024-12-31,请把区间收进该范围。',
  },
];

// 提交前自查清单
export const CHECKLIST_ITEMS = [
  '代码只有 init 与 handle_bar 两个入口(允许辅助函数)',
  'history_bars 使用前判空判长度,单字段返回值按一维数组运算',
  '下单只出现在 handle_bar,目标仓位 ≤ 0.95,先查持仓再下单',
  '没有 shift(-n) / datetime.now() / requests / open() / 顶层 fit()',
  '标的在标的池内,日期区间在数据范围内(默认 2023-01-03 ~ 2024-06-28)',
  '先过校验台(Layer 1),再回测;失败信息可直接贴回模型对话修复',
];
