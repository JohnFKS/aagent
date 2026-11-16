# 2026-11-16 ETF 数据源接入 + 五福策略 MCP 项目 Agent 优化归档

## 1. 任务来源

用户要求:
1. 在 MCP 模式下直接接入项目 agent 根据回测结果优化策略再发送给项目,
   或打开子 agent 作为项目 agent、由助手扮演用户提出修改意见让子 agent 改策略;
2. 给项目接入 ETF 数据源并扩充标的池;
3. 选择最全、最强、最稳定的免费数据源作为项目默认数据源。

## 2. ETF 数据源接入(默认数据源)

### 2.1 选型结论

| 数据源 | 接口 | 优势 | 短板 |
|---|---|---|---|
| 东方财富 | `fund_etf_hist_em` / `fund_etf_spot_em` | 覆盖全、支持复权 | push2 行情易限流(`Push2RateLimitError`) |
| 新浪 | `fund_etf_hist_sina` / `fund_etf_category_sina` | 实测 927 只 ETF、历史回溯至 2012、稳定不限流 | 分类接口个别类目(场内货币/QDII)偶发 KeyError |

选用 **AKShare 免费聚合**作为基础,实现 **东财优先 → 新浪降级 → 东财熔断**:
首批探测确认东财限流后,本批次剩余标的全部走新浪,避免大池子逐只空等。
`fund_etf_category_sina` 主类目 `ETF基金` 返回 927 只,足够构建全市场池。

### 2.2 工程改动

- 新增 `src/aquant/data/etf_source.py`:`EtfSource.discover_etf_codes(top_n)`、代码规范化、东财→新浪降级、熔断;
  合约元数据 `type="ETF"` + `fund_type="ETF"`,`round_lot=100`。
- `src/aquant/data/bundle.py`:按合约类型分流,`ETF/LOF/REITs` 写 `funds.h5`,个股写 `stocks.h5`(删除"funds.h5 恒空"旧假设)。
- `src/aquant/data/build_bundle.py`:`--source` 默认改为 `etf`,新增 `--top`(默认 300),未指定 `--codes` 时自动发现全市场 ETF。
- `scripts/start_platform.sh`:默认 bundle 改为 `~/.aquant/bundle-etf`。
- `src/aquant/data/provider.py`:BarSource 协议补充 ETF 合约类型/`fund_type`/`funds.h5` 约定。
- `tests/data/test_etf_source.py`:10 项测试(代码规范化、ETF 元数据、东财限流降级、东财正常不降级、ETF 写入 funds.h5)——全部通过。

### 2.3 构建产物(默认 bundle)

```
/root/.aquant/bundle-etf
  instruments.pk : 61 条(60 ETF + 1 INDX 基准 000001.XSHG)
  funds.h5       : 5.3 MB(60 ETF 日线,真实价 + ex_cum_factor 复权路径)
  indexes.h5     : 000001.XSHG(基准)
  trading_dates  : 1420 个交易日,20210104 ~ 20261113
  rqalpha self-check 通过
```

启动验证:`GET /api/health` 返回 200,rqalpha 6.5.0 OK,日志显示默认加载 `bundle-etf`。

## 3. 五福策略 ETF 移植

将聚宽《五福改进版 v5》移植到 RQAlpha 并换用真实 ETF 轮动池:

- `strategies/wufu_v5_etf_rotation.py`(基线,最终交付版本):
  - 池:13 只真实 ETF(宽基 510300/510500/159915/588000/510050/512100、
    主题 512480/512800/512880/515880、跨境 513050/513180、黄金 518880);
  - 防御标的:511880.XSHG(银华日利货币 ETF);基准:000001.XSHG;
  - 核心:12 组 `lookback × r2_min × ma_lb` 动量共识 + 加权频率投票;
  - 状态机:走弱/牛市进攻/强制防御;多层风控:硬止损 8%、成本保护、移动止损、
    MA20 破位、ATR 吊灯、RSI 超买止盈;10 日回撤 -10.5% 触发防御。

移植差异(RQAlpha 适配):`initialize→init`、`run_daily 多时点→单一 handle_bar`、
`get_price DataFrame→history_bars ndarray`、`pos.avg_cost→pos.avg_price`(RQAlpha 6.5.0)。

## 4. MCP 项目 Agent 四轮优化(失败结论)

### 4.1 工作方式

用户指定的 MCP 模式:项目自带 LLM 端点 `/api/sessions/{sid}/turns` 调用失败
(`dots3-note-prev` 服务 ReadTimeout 502)。按用户备选路径,打开子 agent 作为"项目 agent",
由助手扮演用户依据回测结果提出修改意见 → 子 agent 输出完整改码 → 助手落盘 → MCP 校验/回测 → 落版本。
会话 `s_9af83f77cc` 保存基线 v1 与 3 个 agent 实验版本(v2~v4,第2轮修正版仅回测未落该会话版本),每轮对话/指标均已归档。

### 4.2 四轮结果(2021-01-01~2026-11-15,本金 ¥20000)

| 版本(策略文件) | 会话版本 | 改动要点 | 年化 | 最大回撤 | 换手 | 手续费 | 结论 |
|---|---|---|---|---|---|---|---|
| **基线 `wufu_v5_etf_rotation.py`** | v1 | ETF 动量共识原版 | **-0.85%** | **26.18%** | **124.1** | 2692 | **最优** |
| 第1轮 `wufu_v5_etf_optimized.py` | v2 | 最小持有10日+严格粘性+高门槛+早防御 | -12.01% | 62.74% | 225.1 | 2994 | 劣化 |
| 第2轮 `wufu_v5_etf_v2.py` | (仅回测) | 门槛回退+防御冷却5日+防御守持有期 | -8.04% | 48.47% | 196.0 | 3806 | 劣化 |
| 第3轮 `wufu_v5_etf_v3.py` | v3 | 止损切511880冷却5日+粘性回温和+持有5日 | -4.60% | 62.44% | 235.0 | 3526 | 劣化 |
| 第4轮 `wufu_v5_etf_v4.py` | v4 | 基线仅加5日最小持有+持仓扫描含511880 | -5.46% | 64.08% | 236.0 | 3200 | 劣化 |

### 4.3 根因分析

- 多参数动量共识在 A 股 ETF 上是**弱信号**,基线年化已为负;
- 所有"复杂避险逻辑"(止损冷却、货币 ETF 往返、最小持有期)的**往返损耗 > 节省的回撤**;
- 最小持有期让持仓**锁在转弱标的**上,错过止损与反弹,换手不降反升(124→236);
- 6.5% 硬止损 + -8% 早防御在震荡市频繁触发,叠加防御往返放大损耗。

### 4.4 决策

不做激进改动,会话 rollback 回 v1 基线 ETF 策略作为最终方案。
项目 agent 的四轮修改均如实归档(含指标),未采纳。

## 5. 有效性与风险声明

- 基线策略样本内年化 **-0.85%**、最大回撤 **26.18%**,年化为负;
- 分层验证 `/api/verify` 在**纯 ETF bundle 上无法执行**(随机基准需股票合约,返回 400 指引),
  需在含个股的 bundle 上另验;即便如此,样本内负收益已说明策略**不具备可交易的正期望**;
- **结论:不得进入实盘讨论**,仅作为 ETF 数据源与 MCP 工作流的演示基线。

## 6. 关联改动

- 修复 `cash` 未从 API 透传 runner 的 bug(此前 `cash=20000` 实际按默认 100 万回测,
  手续费 14 万),本次所有 ETF 回测均按 ¥20000 真实本金;回归测试见
  `tests/agents/test_cash_passthrough.py`。
- README 更新:ETF 默认数据源、双源降级、ETF bundle 构建命令、纯 ETF bundle 分层验证限制。
