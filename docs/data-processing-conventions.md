# 数据处理规范（AQ-Platform）

适用范围：bundle 构建、日线/因子落盘、跨源校验、缓存与容量管理、退市/ST/停牌处理。
核心原则：**真实价单一口径、复权因子单一路径、跨源先比价再回测、缓存不能破坏契约**。

## 1. 数据源分工

| 数据 | 主力源 | 补充源 | 说明 |
| --- | --- | --- | --- |
| A 股日线 | Baostock | AKShare（对比） | Baostock 历史长、含退市/ST/停牌字段 |
| 资金流/板块 | AKShare | — | 仅作研究补充，不作为 K 线主口径 |
| 合成数据 | SyntheticSource | — | 仅用于管线自测/演示，禁止当作真实回测结论 |

- `--source baostock` 为默认主力；AKShare 自动作为对比源。
- AKShare 东财接口被限流时自动降级新浪；必须保留限速与重试，禁止循环暴力请求。

## 2. 价格与复权口径

- bundle 中 `daily_raw` 只存**真实价**（不复权）。
- 复权因子单独存储 `ex_cum_factor`，派生公式固定为：
  `hfq_price = raw_price * ex_cum_factor`
- **禁止混源**：
  - 不得用 A 源的原始价配 B 源的复权因子；
  - 不得在一段回测中途切换数据源。
- Baostock 侧因子由 `后复权收盘(adjustflag=1) / 真实收盘(adjustflag=3)` 计算。
- AKShare 侧因子同样由真实价与后复权价派生，不直接使用平台返回的复权价序列。

## 3. 跨源校验与超差回退

回测前必须执行 `python -m aquant.data.cross_check`（或构建 bundle 时自动执行）：

1. 取两源重叠日期，对比 `raw close` 与 `hfq close`。
2. 默认容差 `--cross-check-tolerance 0.005`（0.5%）。
3. 超差时按覆盖度评分选择源：
   - 标的覆盖 50%
   - 交易日覆盖 30%
   - 字段完整度 20%
4. 评分高者胜出；平分时保留主力源 Baostock。
5. 切换后整段回测只使用选中源，报告写入 `bundle/cross_check.json`。

> 实测中两源原始价常一致但复权价差异可达 10%~30%，原因多为分红/拆股处理不同。未经比价直接混用会导致回测收益不可信。

## 4. Bundle 与合约元数据

`instruments.pk` 必须包含 RQAlpha 6.5.0 所需字段，尤其：

- `order_book_id`、`symbol`、`type`、`listed_date`、`de_listed_date`、`round_lot`
- `exchange`、`board_type`、`market_tplus`、`status`、`special_type`
- ETF 额外 `fund_type`

经验教训：Baostock 构建若缺 `board_type`，下单时会触发
`AttributeError: 'Instrument' object has no attribute 'board_type'`，导致回测失败。
`board_type` 映射：`688*` → `KSH`，`300/301*` → `GEM`，`4/8/92*` → `BJS`，其余 → `MainBoard`。

涨跌停限价按板块、上市首日、ST 状态计算；bundle 自检需输出 `limit_up/limit_down` 抽样值核对。

## 5. ST / 停牌 / 退市

- Baostock `tradestatus=0` → 停牌日；`isST=1` → ST 日，直接落盘，不靠推断。
- 退市信息取 `query_stock_basic` 的 `outDate` 与 `status`，写入 `de_listed_date`，不得默认全部 “未退市”。
- 免费源处理参差，必须自行校验：
  - 退市日之后不得有 bar；
  - ST 区间强制 ±5% 涨跌停；
  - 停牌日无成交且撮合不成交。
- AKShare 若无法稳定提供 ST/退市字段，应在元数据中标注局限，不得伪造数据。

## 6. 缓存与容量控制

### 6.1 限速与缓存

- AKShare 每次请求前随机 sleep `0.5~1.0s`，失败重试不超过 3 次。
- 资金流/板块接口使用更保守的限速和独立缓存。
- 禁止对免费源做无节制的循环全市场请求。

### 6.2 DuckDB 缓存

- 默认缓存目录：`~/.aquant/cache/duckdb`。
- 按 `source_key`（源 + codes + 区间）隔离数据库文件，避免不同口径混缓存。
- 表：`daily_raw`、`adjust_factor`、`json_cache`、`access`。
- 命中缓存不再回源；未命中才抓取并写入。
- 容量控制：
  - `--cache-retention-days N`：仅保留最近 N 天；
  - `--cache-max-mb M`：超限按 `access.last_access` 淘汰最久未用标的，并执行 `CHECKPOINT`；
  - 构建新口径 bundle 前，如缓存 key 未变但元数据已改，必须清理对应 `.duckdb` 文件。

## 7. 数据质量门禁

构建 bundle 后必须通过自检：

- `instruments` 可加载，合约字段齐全（含 `board_type`）。
- 每个标的日线非空且日期递增，无重复日期。
- `volume >= 0`，`close > 0`，复权因子单调不递减且非空。
- `funds.h5`、`trading_dates`、`construment` 等文件生成完整。
- 抽样检查涨跌停价是否符合板块规则。
- 跨源报告 `cross_check.json` 存在且记录差异与选源原因。

## 8. 存储与安全

- 数据 bundle 与缓存放在用户目录，不提交 Git。
- LLM API 密钥保存在 `~/.aquant/llm_settings.json`，权限 `0600`，接口只返回脱敏值。
- 第三方镜像下载的源码需核对来源；公开仓库快照可用于运行，但生产环境应固定 commit。
- 不要将真实账户、密钥、个人隐私信息写入策略代码或回测报告。

## 9. 推荐的完整流程

```bash
# 1) 构建真实 bundle（含跨源校验和 DuckDB 缓存）
python -m aquant.data.build_bundle \
  --source baostock --codes 000001,600519 \
  --start 2021-01-01 --end 2026-11-15 \
  --out ~/.aquant/bundle-baostock \
  --cache duckdb --cache-max-mb 512 --cache-retention-days 3650

# 2) 策略代码校验
python -m aquant.cli validate strategies/demo_ma_cross.py

# 3) 回测 + 分层验证 + 竞技场（也可通过 MCP 工具执行）
```

相关实现：`src/aquant/data/bundle.py`、`limits.py`、`cross_check.py`、`duckdb_cache.py`、`src/aquant/validation/`。
