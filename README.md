# AQ-Platform · A股回测正确性平台

> 基于 **RQAlpha 6.5.0** 的 A 股策略回测系统,核心目标:**回测结果可信、贴近实盘**。
> 对话式生成策略 → 四层校验把关 → 真实数据回测 → 分层验证 → 多策略竞技场排名。
> 附带 React Web 前端、REST API、CLI 与 MCP server 四种使用方式。

**当前状态**:仅供学习与研究。回测结果不构成任何投资建议;实盘对接不在范围内。

---

## 目录

- [核心特性](#核心特性)
- [架构总览](#架构总览)
- [快速开始](#快速开始)
- [方式一:Web 界面](#方式一web-界面推荐)
- [方式二:CLI 命令行](#方式二cli-命令行)
- [方式三:REST API](#方式三rest-api)
- [方式四:MCP Server](#方式四mcp-server)
- [真实数据构建](#真实数据构建)
- [LLM 配置(接入大模型)](#llm-配置接入大模型)
- [Token 闸门(scope 与限流)](#token-闸门scope-与限流)
- [环境变量参考](#环境变量参考)
- [测试与质量](#测试与质量)
- [项目结构](#项目结构)
- [已知限制(必读)](#已知限制必读)
- [FAQ](#faq)
- [许可](#许可)

---

## 核心特性

### 四层校验体系(本项目的灵魂)

| 层 | 解决的问题 | 落地位置 |
|---|---|---|
| **Layer 1 代码自动校验** | Agent 生成代码后、回测执行前,静态拦截:未来函数(`shift(-n)`)、墙钟时间(`datetime.now()`)、策略内直接拉外部数据(绕过统一口径)、全局 `fit()` 过拟合模式、`before_trading` 引用当日行情、T+0 回转;配置 lint:T+1 被关、涨跌停撮合被关、零滑点、费率 <1、信号模式 | `aquant.validation` |
| **Layer 2 统一数据口径** | bundle 只存**真实价(不复权)**,复权信息走 `ex_cum_factor` 单一路径(消除除权除息跳空与双重复权);板块涨跌停价规则(主板±10%/创业科创±20%/北交所±30%/ST±5%,Decimal 四舍五入到分);AKShare 双源降级 + Parquet 缓存 | `aquant.data` |
| **Layer 3 保守默认参数** | 强制注入 0.2% 比例滑点、单 K 线成交量占比 ≤20%、万八佣金 + 千一印花税、T+1 开启;用户给更保守值则尊重,更激进值直接 ERROR 拒绝 | `aquant.config` |
| **Layer 4 分层验证** | 样本内 → 样本外自动切分对比(OOS 收益为负/回撤放大/夏普腰斩 → FAIL)+ **随机交易基准**对照 + 模拟盘引导清单(回测通过 ≠ 可以实盘) | `aquant.verify` |

### Agent 管线(对话式生成的安全网)

```
用户需求 → StrategyEditorAgent(LLM 生成,校验-重试-防作弊 ≤3 次)
        → ValidationAgent(硬门禁)
        → BacktestAgent(校验不过绝不回测)── 回测崩溃?→ 错误回馈 LLM 自愈(≤2 次)
        → ReviewAgent(规则 + LLM 复核)
        → 报告 + 净值曲线 + K 线 + 经验库自动入库
```

**运行时自愈闭环**:静态校验发现不了 API 误用(如实测中 LLM 把 `history_bars` 返回的
ndarray 当 DataFrame 用,崩在撮合阶段)——运行时崩溃的错误会回馈给 LLM 修正,
实测连续自愈 2 次后成功跑通。失败案例自动进经验库(`~/.aquant/experience/`),
后续生成时按需求标签检索相似案例注入 prompt(复盘-反思-优化闭环)。

### 多策略竞技场

1~6 个策略候选并行回测(校验不过即拒、执行崩溃隔离),按年化+夏普降序排名——
"养蛊式大逃杀"的工程化落地。

---

## 架构总览

```
                ┌────────────────────────────────────────┐
                │      Web 前端(React + Vite,两个 Tab)    │
                │  策略对话 / 校验台 / 净值图 / K线 / 竞技场 │
                └──────────────┬─────────────────────────┘
                               │ FastAPI(6 端点,token 闸门)
        ┌──────────────────────▼────────────────────────┐
        │            agents/ 编排器 Orchestrator          │
        │  Editor(自愈)→ Validator → Backtest → Review  │
        └───────┬──────────────────────────┬─────────────┘
                ▼                          ▼
  ┌── Layer 1+3 校验与门禁 ──┐   ┌── runner(子进程隔离)──┐
  │ AST 未来函数检测          │   │ rqalpha run_file      │
  │ 配置 lint / 保守参数注入  │   │ sys_analyser 结果解析 │
  └──────────────────────────┘   └──────────┬────────────┘
                                            ▼
  ┌── Layer 2 数据口径 ──────┐   ┌── Layer 4 分层验证 ──────┐
  │ AKShare→新浪降级 + 缓存   │   │ IS→OOS 对比 + 随机基准    │
  │ 板块涨跌停 + 复权 bundle  │   │ + 模拟盘引导清单          │
  └──────────────────────────┘   └──────────────────────────┘
```

---

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/JohnFKS/aagent.git
cd aagent

# 2. Python 环境(3.11+,rqalpha 锁 numpy 1.26 / pandas 2.x)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # 真实数据另装:pip install -e ".[akshare]"

# 3. 构建数据 bundle —— 两种方式任选

#   3a. 合成数据(秒级,无网络依赖,先跑通管线)
python -m aquant.data.build_bundle --source synthetic --out ~/.aquant/bundle

#   3b. 真实 A 股数据(需网络;东财限流时自动降级新浪)
pip install -e ".[akshare]"
python -m aquant.data.build_bundle --source akshare \
    --codes 000001,600519 --start 2022-01-01 --end 2024-12-31 \
    --out ~/.aquant/bundle-real

# 4. 一键启动完整平台(前端 + API)
scripts/start_platform.sh 8000 ~/.aquant/bundle-real
# 浏览器打开 http://127.0.0.1:8000

# 5. 跑测试(283 个)
python -m pytest tests/
```

> 前端开发模式:`cd frontend && pnpm install && pnpm dev`(dev 代理 /api → :8000);
> 生产构建 `pnpm build` 产物 dist/ 由 FastAPI 自动托管(dist 不存在时 API 独立运行)。

---

## 方式一:Web 界面(推荐)

启动平台后浏览器打开 `http://127.0.0.1:8000`,顶部两个 Tab:

**Tab 1 · 策略对话**
- 左栏:自然语言描述策略需求(选起止日期)→"生成并回测"→ 五步进度条
  (生成代码 → Layer 1 校验 → 回测执行 → 评审复核 → 报告生成)→
  指标卡(总收益/年化/夏普/回撤/佣金/印花税,A股习惯涨红跌绿)+ 净值曲线 + K 线(标的行情)+
  校验结论(✗/⚠/ℹ 分级)+ 生成代码 + 完整报告弹层
- 右栏"校验台":粘贴任意策略代码 → 仅校验 / 校验并回测;附"填入坏策略示例"
  (含 `.shift(-1)` 未来函数)体验 Layer 1 拦截

**Tab 2 · 多策略竞技场**
- "载入示例"→"开赛"→ 并行回测 → 排名表(年化+夏普降序,榜首高亮)+
  每候选状态徽章(✓ 已入榜 / ✗ 校验拦截 / ⚠ 执行失败,可展开详情)

**Tab 3 · 策略工坊(手动模式)**
- 平台不是全 Agent 的——**你可以自己写策略代码、自己选回测与分析方式**:
  - 代码编辑器(行号、Tab 缩进、Ctrl/Cmd+Enter 快捷执行),模板:空白骨架 /
    双均线择时 / RSI 超卖(规范 ndarray 写法);
  - 本地策略库(localStorage):保存/载入/删除/导出 .py,草稿自动恢复;
  - **回测方式自选**:日期区间、初始资金,高级选项可自选更保守的滑点与
    成交量占比(平台默认即保守值;激进值由 Layer 1 拒绝);
  - **分析方式自选**(勾选执行):基础回测(必选)/ 分层验证(Layer 4:
    IS→OOS 对照 + 随机基准 + 模拟盘清单)/ K 线行情(自动从代码提取标的);
  - 手动模式与 Agent 模式殊途同归:**所有代码都过同一套四层校验**;
  - **对话生成的代码可"送入策略工坊编辑"再加工**(编辑器 ↔ Agent 交替迭代)。

## AI 自动优化闭环

对**任何策略**(手写或 Agent 生成)发起自动优化:每轮 = 基线回测 → **五类规则自动诊断**
(零成交/换手过高/成本占比/回撤偏大/胜率偏低)→ LLM 调整(超参数单次幅度 ≤50%、
信号过滤、出场逻辑、代码/逻辑重构须说明理由)→ Layer 1 校验(不过则带反馈重试)→
回测(运行时崩溃错误回馈自愈)→ 与当前最优按 (夏普, 年化, -回撤) 元组对比保留;
连续 2 轮无改进早停。演进表全程可见(每轮采用/未超越/失败 + LLM 分析摘要),
"采用最优版本"一键回写编辑器。

实测战果(真实数据 + 真实 LLM,2 轮):夏普 -1.03 → +0.004、年化 -13.55% → +1.19%、
最大回撤 30.4% → 14.6%、佣金 -36%;LLM 改动:均线 5/20→7/30、新增 60 日趋势过滤、
新增 5% 止损、仓位 95%→90%。

```bash
curl -s -X POST localhost:8000/api/optimize -H 'content-type: application/json' -d '{
  "code": "...", "start": "2023-01-03", "end": "2024-06-28", "rounds": 2
}'
# → {"improved", "best": {"code","summary"}, "baseline", "rounds": [...], "elapsed_seconds"}
# 耗时:每轮 = 1 次 LLM 生成(30~120s)+ 2 次回测;2 轮常见 3~8 分钟
```

## 分层验证 API

```bash
curl -s -X POST localhost:8000/api/verify -H 'content-type: application/json' -d '{
  "code": "...", "start": "2022-01-04", "end": "2024-12-31",
  "is_ratio": 0.7, "overrides": {"sys_simulation": {"slippage": 0.005}}
}'
# → status(pass/warning/fail) + IS/OOS 指标 + checks 判定 + 随机基准 + 模拟盘清单 + report 全文
```

`/api/backtest` 与 `/api/verify` 均接受可选 `overrides`(更保守值生效,
激进值 422 拒绝——保守参数防线对手动模式同样生效)。

## 方式二:CLI 命令行

```bash
# Layer 1 校验(AST 未来函数 + 配置 lint)
python -m aquant validate strategies/demo_ma_cross.py

# 单次回测(校验是硬门禁,不过即拒)
python -m aquant backtest strategies/demo_ma_cross.py \
    --start 2023-01-03 --end 2024-06-28 --bundle ~/.aquant/bundle-real

# 分层验证(Layer 4:IS→OOS 对比 + 随机基准 + 模拟盘清单)
python -m aquant verify strategies/demo_ma_cross.py \
    --start 2022-01-04 --end 2024-12-31 --bundle ~/.aquant/bundle-real

# 拦截演示(全部会被 ERROR 拒绝)
python -m aquant validate strategies/bad_examples/lookahead_shift.py   # .shift(-1)
python -m aquant validate strategies/bad_examples/external_data.py     # requests+墙钟
python -m aquant validate strategies/demo_ma_cross.py \
    --overrides strategies/bad_examples/zero_cost_overrides.json       # 零滑点+关T+1
```

## 方式三:REST API

| 端点 | 方法 | scope | 说明 |
|---|---|---|---|
| `/api/health` | GET | - | 健康与版本 |
| `/api/validate` | POST | readonly | Layer 1 校验:`{"code", "start"?, "end"?}` |
| `/api/chat` | POST | readwrite | 对话式全管线:`{"text", "start", "end"}` → steps/verdict/metrics/portfolio_series/report |
| `/api/backtest` | POST | readwrite | 直接回测已就绪代码:`{"code", "start", "end", "cash"?}` |
| `/api/arena` | POST | readwrite | 多策略竞技场:`{"candidates": [{"name","code"}...1~6], "start", "end"}` |
| `/api/kline` | GET | readonly | K 线真实价:`?oid=000001.XSHE&start=...&end=...` |

```bash
# 校验(坏策略 → passed=false + findings)
curl -s -X POST localhost:8000/api/validate -H 'content-type: application/json' \
    -d "{\"code\": \"$(cat strategies/bad_examples/lookahead_shift.py | base64 -w0)\"}"

# 竞技场(校验不过的候选自动 rejected,不占回测)
curl -s -X POST localhost:8000/api/arena -H 'content-type: application/json' -d '{
  "candidates": [{"name": "MA金叉", "code": "..."}, {"name": "MACD", "code": "..."}],
  "start": "2023-01-03", "end": "2024-06-28"
}'
```

错误语义:校验拦截 422(附 verdict)、bundle 缺失 400(附构建指引)、
LLM 故障 502、限流 429、token 无效 401/权限不足 403——全部中文可读 body。

## 方式四:MCP Server

```bash
python -m aquant.mcp_server.server     # stdio 模式,接 Claude Desktop 等客户端
```

客户端配置示例(Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "aquant": {
      "command": "/path/to/aagent/.venv/bin/python",
      "args": ["-m", "aquant.mcp_server.server"],
      "env": {
        "AQUANT_API_BASE": "http://127.0.0.1:8000",
        "AQUANT_MCP_TOKEN": "tkn_xxx"
      }
    }
  }
}
```

3 个工具:`platform_health` / `platform_validate_code` / `platform_run_backtest`。
非法参数与非 2xx 一律返回**可读中文错误文本**(不抛异常),让 LLM 自纠。

---

## 真实数据构建

```bash
# 平台已内置双源降级:东方财富(主)→ 新浪(备,东财 IP 限流时自动切换)
python -m aquant.data.build_bundle --source akshare \
    --codes 000001,600519 --start 2022-01-01 --end 2024-12-31 \
    --out ~/.aquant/bundle-real

# 自检输出:instruments 加载、stocks.h5 首行(真实价 + 涨跌停价)
```

口径说明(重要):
- bundle 存**真实价**,复权因子在 `ex_cum_factor.h5`——策略信号计算可用复权价,
  撮合用真实价与真实涨跌停价,**不存在双重复权**;
- 现金分红进 `dividends.h5`(账户模块处理);AKShare 分红接口失败时价格复权仍正确
  (因子已含),仅缺现金到账,构建日志会警告;
- 区间首日无前收盘价,涨跌停字段为超宽占位值(回测从第 2 日起完全真实)。

---

## LLM 配置(接入大模型)

无配置时使用 `DeterministicLLM` 兜底模板(演示管线,不解析自然语言)。
配置后接入任意 **OpenAI 兼容** API,对话生成才真正理解需求:

```bash
# 写入项目根 .env(start_platform.sh 自动加载;切勿提交到仓库!)
AQUANT_LLM_BASE_URL=https://api.example.com/v1
AQUANT_LLM_API_KEY=sk-xxxx
AQUANT_LLM_MODEL=your-model
AQUANT_LLM_AUTH_SCHEME=bearer        # OpenAI 标准用 bearer;Dots 等平台用 api-key
AQUANT_LLM_TIMEOUT=120               # 秒,带深度思考的模型建议 ≥120
```

实测可用:Dots API(`https://note3-prev-api.askdiandian.com`,`api-key` 头,
模型 `dots3-note-prev`)。管线已实测:RSI 需求被正确解析、运行时错误两轮自愈后
回测跑通、生成策略与需求中的均线参数/仓位一致。

> ⚠️ 密钥安全:Key 不进源码、前端、日志、公开仓库;泄露立即在平台侧吊销。

## Token 闸门(scope 与限流)

默认(未配置 token)为**本机开放模式**。配置后生效:

```bash
export AQUANT_MCP_TOKENS='{"tkn_a": {"scope": "readonly", "name": "只读", "rpm": 30},
                           "tkn_b": {"scope": "readwrite", "name": "MCP"}}'
# 或单 token 简写(= readwrite):
export AQUANT_MCP_TOKEN='tkn_xxx'
```

- readonly:仅 `/api/validate`(行情读取类端点同样登记 readonly);
- readwrite:全部写操作端点;
- 401 无效/缺失、403 scope 不足、429 超 RPM(滑窗,默认 60/分钟);
- **fail-closed**:新端点忘记登记 scope 会被默认拒绝。

## 环境变量参考

| 变量 | 默认 | 说明 |
|---|---|---|
| `AQUANT_BUNDLE_PATH` | `~/.aquant/bundle` | RQAlpha 数据 bundle 目录 |
| `AQUANT_LLM_BASE_URL` / `AQUANT_LLM_API_KEY` / `AQUANT_LLM_MODEL` | - | OpenAI 兼容 LLM 三件套(全设才启用) |
| `AQUANT_LLM_AUTH_SCHEME` | `bearer` | `bearer` 或 `api-key` |
| `AQUANT_LLM_TIMEOUT` | `120` | LLM 请求超时(秒) |
| `AQUANT_MCP_TOKEN` / `AQUANT_MCP_TOKENS` | - | token 闸门(未设=开放) |
| `AQUANT_ALLOW_UNSAFE` | - | `=1` 时豁免校验 error(结果打 unsafe 标,仅供研究) |

## 测试与质量

```bash
python -m pytest tests/        # 283 个测试(单元/E2E/契约/GUI 辅助)
```

- **端到端**:合成 bundle 真实跑通 rqalpha 回测,断言成本生效、成交量限制生效;
- **真实数据链路**:东财→新浪降级、复权因子单调性闸门、bundle 逐字段与 rqalpha
  源码常量比对;
- **GUI 系统测试**:web-gui-tester 方法论,10 个用户流全部通过
  (报告:[docs/system-test-report.md](docs/system-test-report.md),截图 [screenshots/](screenshots/));
- **code-review 双轴评审**已执行并修复(Standards 14 项 / Spec 11 项)。

## 项目结构

```
├── docs/               # design.md(架构+ADR)、system-test-report.md、gui-test-protocol.md
├── frontend/           # React 18 + Vite(对话/校验台/竞技场/K线/净值图)
├── scripts/            # start_platform.sh 一键启动
├── screenshots/        # 系统测试截图证据
├── src/aquant/
│   ├── validation/     # Layer 1:AST 规则 + 配置 lint + findings
│   ├── config/         # Layer 3:defaults.yaml(唯一事实来源)+ schema
│   ├── data/           # Layer 2:provider/synthetic/akshare/cache/limits/adjust/bundle
│   ├── runner/         # RQAlpha 子进程封装 + key_metrics
│   ├── verify/         # Layer 4:pipeline/metrics/arena
│   ├── agents/         # 4 子 agent + orchestrator + experience + prompts
│   ├── api/            # FastAPI + token_gate + kline + adapter
│   └── mcp_server/     # MCP 薄桥(3 工具)
├── strategies/         # 合规示例 + bad_examples/(校验拦截演示)
└── tests/              # 283 个测试
```

## 已知限制(必读)

1. **AST 检测是启发式**:拦截已知危险模式,不能穷尽所有未来函数变体;
   运行时自愈兜底 API 误用,但仍需人工复核(Layer 4 报告页已声明)。
2. **日线回测**:分钟线与撮合升级在路线图中。
3. **数据边界**:bundle 首日涨跌停为占位值;分红接口 best-effort(价格复权不受影响);
   ST/北交所规则完整度依赖 AKShare 数据可得性。
4. **LLM 生成质量**依赖上游模型;平台以校验-自愈-竞技场-分层验证四重机制控制风险,
   不承诺策略盈利。趋势策略在单边震荡窗口可能零成交——平台如实显示。
5. **实盘与模拟盘对接不在范围**;Layer 4 的模拟盘清单是进入实盘前的必经门槛。

## FAQ

**Q: 东财数据接口报错/限流?**
平台自动降级新浪接口;若两者都失败,等待限流窗口(东财首犯约 5 分钟,反复触发更久)。

**Q: 对话生成用了固定模板,没解析我的需求?**
未配置 LLM 时走 DeterministicLLM 兜底(演示管线用)。配置 `AQUANT_LLM_*` 三件套即可。

**Q: 回测结果总收益为 0 / 无成交?**
先看校验结论与报告:可能区间内信号未触发(如 RSI 未达阈值),属真实结果;
可用竞技场多候选对比,或放宽/更换策略逻辑后用 Layer 4 验证。

**Q: rqalpha 版本兼容?**
平台针对 **6.5.0** 源码核实并适配(bundle 格式、`get_position` 签名、summary 键、
trades 为 DataFrame 等);其他版本不保证。

**Q: 为什么禁止零滑点回测?**
实盘不存在零冲击成交,零滑点是收益虚高的第一大来源;同理佣金/印花税/T+1/涨跌停
撮合限制都强制开启。确需研究对比可设 `AQUANT_ALLOW_UNSAFE=1`(结果打 unsafe 标)。

## 许可

本仓库代码按仓库根目录 [LICENSE](LICENSE)(**Apache License 2.0**)发布。
依赖的 [RQAlpha](https://github.com/ricequant/rqalpha) 同为 Apache 2.0(含商业用途
附加条款),商用请自行确认米筐科技授权;数据来自 AKShare 公开接口,仅供研究。

---
*由 ZCode 多 agent 协作构建(需求分析 → 三仓库调研 → 四层校验 TDD → 多 worker 并行
交付 → code-review 双轴评审 → 真实数据系统测试),全套过程证据见 docs/ 与 screenshots/。*
