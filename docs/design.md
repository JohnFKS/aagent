# AQ-Platform:A 股回测正确性平台 设计文档

> 基于 RQAlpha 6.5.0 的 A 股策略回测系统,核心目标:**回测结果 100% 可信,回测与实盘偏差最小化**。
> 双模式产品:**策略工坊**(用户手写代码、自选回测与分析方式)+ **Agent 对话**(自然语言生成,
> 校验-自愈-评审管线)。两种模式殊途同归:所有回测(无论代码来自谁)都必须过同一套四层校验。

## 1. 设计依据(三仓库调研 + 博主文本)

### 1.1 microsoft/qlib — 采纳的工程模式

| 借鉴点 | 来源 | 落地方式 |
|---|---|---|
| factor 与复权价同落库,回测按 factor 还原股数 | `scripts/dump_bin.py`、`backtest/exchange.py` L221 | bundle 存**真实价格**(不复权),`ex_cum_factor.h5` 存累计复权因子,RQAlpha 查询时按需复权(价格×factor、量÷factor) |
| signal shift=1 强制取昨日信号 | `contrib/strategy/signal_strategy.py` L141 | 静态检查器识别"用当前 bar 收盘价在当日开盘前决策"的未来函数模式 |
| 决策/执行/撮合三层解耦 | `backtest/decision.py`/`executor.py`/`exchange.py` | 平台层只产出"校验过的策略+配置",撮合细节交给 RQAlpha exchange(涨跌停/停牌/成交量限制在 exchange 收口) |
| train/valid/test 三段式验证 | `examples/benchmarks/*/*.yaml` | Layer 4 分层验证流程(IS→OOS→模拟盘) |
| 时变股票池(带上市/退市区间) | `storage/file_storage.py` | instruments 元数据含 listed_date/de_listed_date |

### 1.2 PA_Agent — 采纳的 Agent 工程 / 明确拒绝的部分

| 采纳 | 来源 | 落地方式 |
|---|---|---|
| 硬约束 prompt 模板(裸 JSON/思考正文分离/尾置必做清单) | `ai/prompt_assembler.py` | `agents/prompts/strategy_editor.md` 同款结构 |
| 校验-重试-防作弊三件套 + 错误分级重试 | `orchestrator/validation_retry.py`、`ai/retry_policy.py` | StrategyEditorAgent:生成→校验→带 findings 重试(max 3),禁止 LLM 在重试中篡改校验通过的不可变字段 |
| 经验库(成功/失败案例检索注入) | `records/experience_reader.py` | 预留 `agents/experience/` 目录与检索接口,本期不做 |
| NULL 基线实证验证方法论 | `research/PA_提示词工程实证验证报告.md` | Layer 4 报告中输出"买入持有基准/随机基准"对照 |

| 拒绝 | 原因 |
|---|---|
| 无回测、无成本建模(只记决策) | 与"回测正确性"目标根本冲突 |
| "禁仓位管理"的人设约束 | 风控是回测可信的必要条件 |
| 蹭 IDE 免费额度的连接器、伪装 UA 爬虫链 | 合规风险 |
| `two_stage.py` 2168 行巨型文件 | 单文件超限,模块必须拆分 |

### 1.3 tick-stock-panel — 采纳的架构模式

| 借鉴点 | 来源 | 落地方式 |
|---|---|---|
| 涨跌停按板块规则(主板±10%/创业·科创±20%/北交所±30%/ST±5%/上市首日无限制) | `app/price_limits.py` | `data/limits.py`,四舍五入到分(Decimal ROUND_HALF_UP,模拟交易所规则) |
| Provider Protocol 抽象数据源 | `data_providers/base.py` | `data/provider.py`:Synthetic/AKShare 可插拔 |
| 回测独立 worker 进程 + 内存监控 | `app/backtest/worker.py` | runner 在子进程执行回测,主进程隔离崩溃(本期:子进程;内存监控后续) |
| 对话 Agent 工具注册表 + 高危操作人工审批 | `custom/assistant/actions.py` | orchestrator 的校验-拦截环节:校验不通过绝不进入回测 |
| MCP 薄桥 + scope 权限 + 每token限流 | `mcp-server/server.py`、`services/api_gateway.py` | 后续 MCP 接入的设计参考(本期 API 先行) |

### 1.4 博主资源文本 — 架构思路采纳 / 风险拒绝

| 采纳的思路 | 落地方式 |
|---|---|
| 多 Agent 养蛊式大逃杀(蒸馏 20 个 Agent 同场竞技、按表现动态排名) | 多策略候选排名:`verify/metrics.py` `rank_candidates`/`render_ranking`(按年化+夏普降序);分层验证含**随机交易基准**对照(固定种子可复现) |
| Agent 自我复盘/反思/改正优化循环 | StrategyEditorAgent 的"校验→带反馈重试"循环 + ReviewAgent |
| 盘前预案/复盘工作流 | 预留报告模板(Layer 4 输出 IS/OOS 对照报告,后续接入每日盘前简报) |
| 数据广度(分钟线/龙虎榜/板块成分/ETF/可转债) | 数据层 Provider Protocol 按品类扩展;本期只做日线,AKShare 公开数据自建 |

| 明确拒绝 | 原因 |
|---|---|
| "SVIP 财经 API 密钥分享" | **泄露的第三方凭证,禁止使用**(法律与安全) |
| 付费数据包(138GB 分钟数据等)转载分发 | 盗版分发风险;统一用 AKShare 公开数据 + 本地缓存自建 |
| 打板自动交易/自动盯盘下单 | 本期范围是回测正确性;实盘与自动下单不在范围 |
| 抓取付费社区(淘股吧等)数据 | ToS/合规风险;情绪类数据走 AKShare 公开接口 |

## 2. RQAlpha 6.5.0 事实基础(已从源码核实)

影响正确性的全部"可关闭开关"(均位于 rqalpha 安装包内):

| 配置键 | 默认 | 危险值 | 平台策略 |
|---|---|---|---|
| `mod.sys_accounts.stock_t1` | `True` | `False`(可 T+0) | **强制 True**,出现 False 即 ERROR |
| `mod.sys_simulation.price_limit` | `True` | `False` | **强制 True** |
| `mod.sys_simulation.volume_limit` | `True` | `False` | **强制 True** |
| `mod.sys_simulation.volume_percent` | `0.25` | `>0.25` | 上限 0.25,默认 0.2 |
| `mod.sys_simulation.slippage` | `0`(!) | `0` | **强制 ≥0.002**(比例滑点),默认 0.002 |
| `mod.sys_simulation.slippage_model` | `PriceRatioSlippage` | 白名单外模型 | 白名单:PriceRatioSlippage / TickSizeSlippage,白名单外 WARNING |
| `mod.sys_simulation.matching_type` | `current_bar` | 其他 | 日线白名单:current_bar / vwap |
| `mod.sys_transaction_cost.stock_commission_multiplier` | `1` | `<1` | **强制 ≥1**(默认佣金万八+印花千一单边) |
| `mod.sys_transaction_cost.tax_multiplier` | `1` | `<1` | **强制 ≥1** |
| `mod.sys_simulation.signal` | `False` | `True`(跳过撮合) | **强制 False** |
| `base.accounts` | - | 期货等 | 本期仅 `STOCK` 现货账户 |

Bundle 数据格式(6.5.0,已核实 `data/base_data_source/`):
- `stocks.h5`:每合约结构化数组 `datetime(int64,YYYYMMDD000000) / open / close / high / low / volume / total_turnover / limit_up / limit_down`(float64),**价格一律真实价(不复权)**
- `ex_cum_factor.h5`:`start_date`+`ex_cum_factor`(复权因子,查询时应用)
- `dividends.h5`:`book_closure_date / announcement_date / dividend_cash_before_tax / ex_dividend_date / payable_date / round_lot`(现金分红 → 持仓账户自动处理,消除除息跳空误差)
- `split_factor.h5`:`ex_date`+`split_factor`;`suspended_days.h5`/`st_stock_days.h5`:DateSet(停牌/ST 日集合)
- `instruments.pk`(注意后缀)、`trading_dates.npy`、`yield_curve.h5`(含 `data` 数据集)、`future_info.json`(≥1 条目,初始化强制)、`indexes.h5`(基准)

## 3. 架构

```
                        ┌──────────────────────────────────────────┐
                        │              Agent 网页平台(后续)          │
                        │   FastAPI: /api/chat /api/validate        │
                        │           /api/backtest /api/health       │
                        │   (后续:MCP 薄桥 + scope 权限)             │
                        └───────────────┬──────────────────────────┘
                                        │
        ┌───────────────────────────────▼───────────────────────────────┐
        │                    agents/ 编排器 Orchestrator                 │
        │  StrategyEditor ─► ValidationAgent ─► BacktestAgent ─► Review  │
        │        │(校验-重试-防作弊, max3)   │(校验不过绝不回测)            │
        └────────┼───────────────────────────┼───────────────────────────┘
                 ▼                           ▼
   ┌─── Layer 1 代码自动校验层 ───┐   ┌─── runner(RQAlpha 封装) ────┐
   │ AST 未来函数检测              │   │ 子进程执行,解析 sys_analyser │
   │ 配置 lint(T+1/涨跌停/费率)   │   │ 结果 → metrics/portfolio     │
   │ 生成环境检查(可复现性)       │   └──────────────┬──────────────┘
   └─── Layer 3 保守默认参数 ─────┘                  ▼
   │ slippage≥0.002 强制注入      │   ┌─── Layer 4 分层验证流程 ────┐
   │ volume_percent≤0.25          │   │ 样本内 IS → 样本外 OOS        │
   │ 禁止零滑点/零费率/信号模式    │   │ → 模拟盘引导清单               │
   └──────────────────────────────┘   │ + NULL 基线对照                │
                 ▲                    └───────────────────────────────┘
   ┌─── Layer 2 统一数据口径 ─────┐
   │ Provider Protocol:           │
   │  SyntheticSource(测试/演示)  │
   │  AkshareSource(真实数据)     │
   │ ParquetCache 本地缓存        │
   │ limits.py 板块涨跌停规则     │
   │ bundle.py → RQAlpha 格式     │
   │ (真实价 + ex_cum_factor)     │
   └──────────────────────────────┘
```

## 4. 目录结构

```
quant-platform/
  pyproject.toml
  docs/design.md              # 本文档
  src/aquant/
    config/defaults.yaml      # Layer 3 保守默认参数(唯一事实来源)
    config/schema.py          # 配置合并/强制注入/导出 RQAlpha config
    validation/findings.py    # Finding(severity: error/warn/info)
    validation/ast_rules.py   # 未来函数/不可复现 检测规则(AST)
    validation/config_rules.py# T+1/涨跌停/手续费/滑点/成交量 lint
    validation/validator.py   # validate_strategy() 编排入口
    data/provider.py          # BarSource Protocol(可插拔数据源)
    data/synthetic_source.py  # 确定性合成数据(测试/演示)
    data/akshare_source.py    # AKShare 真实数据源(可选依赖)
    data/cache.py             # Parquet 本地缓存
    data/limits.py            # 板块涨跌停价规则(四舍五入到分)
    data/adjust.py            # 统一复权:hfq/raw → ex_cum_factor
    data/bundle.py            # RQAlpha 6.5.0 bundle 构建器
    runner/backtest.py        # run_backtest():子进程 + sys_analyser 解析
    verify/pipeline.py        # IS→OOS→paper 清单 + NULL 基线对照
    verify/metrics.py         # 指标对比与判定
    agents/llm.py             # LLMClient 协议:Deterministic/OpenAI 兼容
    agents/*.py               # 4 个子 agent + orchestrator
    agents/prompts/*.md       # 硬约束 prompt 模板
    api/main.py               # FastAPI(chat/validate/backtest/health)
    cli.py                    # python -m aquant validate|backtest|verify|serve
  strategies/                 # 合规示例 + 坏示例(用于校验演示)
  tests/                      # pytest 全覆盖
```

## 5. 关键决策记录(ADR)

1. **bundle 存真实价 + ex_cum_factor 复权因子**(借鉴 qlib):策略信号计算可用复权价(消除除权跳空),撮合用真实价与真实涨跌停价;现金分红进 dividends.h5,由账户模块处理 → **口径唯一,不存在双重复权**。
2. **保守参数强制注入而非仅告警**:用户显式提供更保守值则尊重;激进值(零滑点/零费率/关 T+1)直接 ERROR 拒绝执行;可通过 `AQUANT_ALLOW_UNSAFE=1` 显式豁免(仅供研究,输出打标 unsafe)。
3. **未来函数检测 = 静态 AST 启发式 + 运行时数据口径约束**:静态层拦截已知危险模式(shift(-n)、外部数据访问、墙钟时间、before_trading/open_auction 中引用行情、外部调用);全局 `fit()` 属 judgement call,实现为 WARNING 级(无法静态证明其训练窗口是否含未来),须报告页人工确认。运行时靠 bundle 真实价+因子机制保证"历史查询天然无未来"。启发式 ≠ 完备,报告页必须声明这一点。可复现性以运行配置指纹 + 策略代码指纹(sha256)审计。
4. **回测子进程隔离**:策略代码永远在子进程运行(平台进程不被用户代码拖垮,也为后续网页平台多租户做准备)。
5. **Agent 层与回测层解耦**:agents 用 Protocol 依赖 runner,LLM 可插拔(Deterministic 兜底 → OpenAI 兼容 API);校验是硬门禁,LLM 无法绕过。
6. **双模式产品**(策略工坊手动模式 + Agent 对话):用户手写代码与 LLM 生成代码殊途同归,都必须过同一套四层校验与保守参数防线;工坊内可对任何策略(手写或 Agent 生成)发起 AI 自动优化。
7. **自动优化闭环**(StrategyOptimizerAgent,`POST /api/optimize`):每轮 = 基线回测 → 五类规则自动诊断(零成交/换手过高/成本占比/回撤/胜率)→ LLM 改动(超参数单次幅度 ≤50%;代码/逻辑改动须说明理由;四层校验禁令沿用)→ Layer 1 校验(不过则带反馈轮内重试)→ 回测(运行时崩溃错误回馈自愈 ≤1 次)→ 与当前最优按 (sharpe, 年化, -回撤) 元组对比保留。早停:连续 2 轮无改进。优化不动摇校验防线——所有轮次的代码都过同一硬门禁;结果进经验库。

## 6. 已知限制与路线

- 本期:日线回测;ST/北交所规则完整度依赖 AKShare 数据可得性;AST 检测是启发式,不能保证捕获所有未来函数变体。
- 数据源双链:东方财富(stock_zh_a_hist)→ 新浪(stock_zh_a_daily)自动降级(东财有 IP 级限流);分红接口 best-effort,失败时复权信息仍完整(在 adjust_factor 中),仅现金分红现金流缺失。
- bundle 首日无前收盘价,涨跌停字段用超宽占位值(回测从区间第 2 日起完全真实);A股分红接口缺失时,除权除息价格口径仍正确(因子已含),仅缺现金到账。
- DeterministicLLM 兜底模板不解析自然语言(固定双均线形态);配置 AQUANT_LLM_* 接入 OpenAI 兼容 API 后,"对话生成"才真正理解需求语义。
- ~~MCP token 闸门是 scope 门禁的简化版~~ → 已完整化:多 token(AQUANT_MCP_TOKENS JSON)+ scope 规则表(readonly/readwrite,fail-closed)+ 每 token 滑窗限流(RPM 可配,429);api/token_gate.py。
- 后续:分钟线与撮合升级、模拟盘对接(实盘不在本期)、多 Agent 竞技场前端深化(逐轮进化赛制)、移动端适配。
- 外部 Agent 模式(external-agent/,纯文档、不新增端点):面向 ZCode/Claude Code/Codex 等外部 agent 平台的集成包——SKILL.md 方法论(七步工作流,与 workflow_guide 工具对齐)+ 各平台 MCP stdio 接入模板 + 可复制的项目目录骨架;外部平台选中任意文件夹作项目目录,经 MCP 薄桥驱动本系统完成"编辑→校验→回测→优化→验证"全流程,四层校验防线对外部 agent 同样生效。
