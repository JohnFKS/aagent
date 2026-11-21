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
- [外部 Agent 模式](#外部-agent-模式external-agent)
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
pip install -e ".[dev,baostock,akshare]"

# 3. 构建数据 bundle —— 默认 Baostock 主力 + AKShare 交叉校验

#   3a. 主力源:Baostock 真实价 + 后复权因子;AKShare 自动做跨源比价
#       (平台默认 bundle 即按此构建:10 只流动性个股,2022-01-04 ~ 2024-12-31)
python -m aquant.data.build_bundle --source baostock \
    --codes 000001,600519,600036,000333,601318,000858,002415,600276,300750,601012 \
    --start 2022-01-01 --end 2024-12-31 \
    --out ~/.aquant/bundle-baostock
#   构建后注入真实基准指数(上证指数 + 沪深300,Baostock 指数接口):
#   scripts/add_benchmark_index.sh ~/.aquant/bundle-baostock 000001.XSHG,000300.XSHG
#   raw/hfq close 超差时按覆盖度评分自动切换到数据最全的源整段回测(不混源),
#   选择原因与差异明细写入 bundle/cross_check.json
#   K线默认缓存到 ~/.aquant/cache/duckdb,用 --cache-max-mb / --cache-retention-days
#   控制磁盘占用;超出上限时淘汰最久未用标的

#   3b. 合成数据(秒级,无网络依赖,先跑通管线)
python -m aquant.data.build_bundle --source synthetic --out ~/.aquant/bundle-synth

#   3c. 仅用 AKShare(仍会尝试 Baostock 做交叉校验)
python -m aquant.data.build_bundle --source akshare \
    --codes 000001,600519 --start 2022-01-01 --end 2024-12-31 \
    --out ~/.aquant/bundle-stock

# 4. 一键启动完整平台(前端 + API),默认加载 ~/.aquant/bundle-baostock
#    (默认 bundle:10 只流动性个股 + 真实上证指数/沪深300 基准,见下方「A股基准与收益图」)
scripts/start_platform.sh 8000
# 指定其它 bundle: scripts/start_platform.sh 8000 ~/.aquant/bundle-stock
# 浏览器打开 http://127.0.0.1:8000

# 5. 跑测试
python -m pytest tests/
```

> 前端开发模式:`cd frontend && pnpm install && pnpm dev`(dev 代理 /api → :8000);
> 生产构建 `pnpm build` 产物 dist/ 由 FastAPI 自动托管(dist 不存在时 API 独立运行)。

---

## 方式一:Web 界面(推荐)

启动平台后浏览器打开 `http://127.0.0.1:8000`,顶部五个 Tab:

**Tab 0 · 编写指南(外部模型工作流)**
- 面向"用任意大模型网页对话(ChatGPT / Claude / Gemini / DeepSeek / Kimi…)生成策略代码"的用户:
  - **一键复制的提示词模板**(完整版 + 极简版):模板内容与平台四层校验规则逐条对齐
    (禁未来函数/外部数据/墙钟时间、`history_bars` 一维数组语义、下单仅限 `handle_bar`、
    标的池白名单),照模板生成的代码**默认配置下可直接校验通过并回测成功**;
  - 三步工作流:复制提示词 → 模型网页生成(只要一个 python 代码块)→ 回平台校验台
    过 Layer 1 → 策略工坊回测;
  - 代码契约速查(允许/禁止对照表)、标的池(平台 bundle 内标的)、常见回测失败与修复
    对照表、收益图与指标导读、提交前自查清单;
  - 参考骨架可一键"送入策略工坊"。

**Tab 1 · 策略对话**
- 左栏:自然语言描述策略需求(选起止日期)→"生成并回测"→ 五步进度条
  (生成代码 → Layer 1 校验 → 回测执行 → 评审复核 → 报告生成)→
  指标卡(总收益/年化/夏普/回撤/基准收益/佣金/印花税,A股习惯涨红跌绿)+
  **收益图(策略净值曲线 + A股基准对照线,默认上证指数)** + K 线(标的行情)+
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
  - **回测方式自选**:日期区间、初始资金、**A股基准**(上证指数 / 沪深300 / 不设基准),
    高级选项可自选更保守的滑点与成交量占比(平台默认即保守值;激进值由 Layer 1 拒绝);
  - **分析方式自选**(勾选执行):基础回测(必选,含**收益图**:策略净值曲线 +
    A股基准对照线)/ 分层验证(Layer 4:IS→OOS 对照 + 随机基准 + 模拟盘清单)/
    K 线行情(自动从代码提取标的);
  - 手动模式与 Agent 模式殊途同归:**所有代码都过同一套四层校验**;
  - **对话生成的代码可"送入策略工坊编辑"再加工**(编辑器 ↔ Agent 交替迭代)。

**Tab 4 · 多轮对话工作台(持续迭代同一策略)**
- 左栏多轮对话:每轮用自然语言要求修改当前策略,Agent 按「改码 → Layer 1 校验 → 回测 → 落版本」执行;
- 中栏策略全程可见可编辑:编辑器内容即当前版本,手动保存生成 `source=user` 新版本,版本时间线支持查看/回退/改说明/永久删除单个版本;
- 左栏对话消息支持编辑、删除、手工追加笔记;会话提供软删除(归档)与**永久删除**(不可恢复)两种入口;
- 右栏展示回测指标与任意两版本的行级 diff(增删行高亮 + 统一 diff 文本);
- 会话与版本落盘 `~/.aquant/sessions/<sid>.json`,刷新页面/重启服务不丢;回测区间可随时调整。

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
| `/api/chat` | POST | readwrite | 对话式全管线:`{"text", "start", "end", "benchmark"?}` → steps/verdict/metrics/portfolio_series/benchmark_series/report |
| `/api/backtest` | POST | readwrite | 直接回测已就绪代码:`{"code", "start", "end", "cash"?, "benchmark"?, "overrides"?}` → metrics/portfolio_series/benchmark_series(默认基准上证指数,`"none"` 关闭) |
| `/api/arena` | POST | readwrite | 多策略竞技场:`{"candidates": [{"name","code"}...1~6], "start", "end"}` |
| `/api/kline` | GET | readonly | K 线真实价:`?oid=000001.XSHE&start=...&end=...` |

**多轮对话会话端点**(策略全程可见/可编辑/可回退/可对比,均需 readwrite):

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/sessions` | GET/POST | 列出会话摘要 / 新建会话(`{start,end,code?,title?,cash?}`) |
| `/api/sessions/{sid}` | GET/PATCH/DELETE | 会话全量(对话+版本) / 更新元信息 / 删除 |
| `/api/sessions/{sid}/turns` | POST | 一轮对话:`{text,backtest?}` → Agent 改码+校验+回测+落版本 |
| `/api/sessions/{sid}/code` | PUT | 手动编辑代码:`{code,message?,backtest?}` → 新版本 |
| `/api/sessions/{sid}/backtest` | POST | 回测当前版本(原地更新指标,不新增版本) |
| `/api/sessions/{sid}/rollback` | POST | 回退:`{version_id}`,历史版本永不删除 |
| `/api/sessions/{sid}/versions` | GET | 版本摘要列表;`/{vid}` 取完整快照 |
| `/api/sessions/{sid}/diff` | GET | `?from_version=v1&to_version=v2` → 行级 diff + unified diff |

**历史对话与版本 CRUD / 永久删除**(均需 readwrite):

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/sessions/{sid}` | DELETE | 默认软删除(移入 `.trash/` 可归档);`?permanent=true` **永久抹除不可恢复** |
| `/api/sessions/{sid}/turns` | GET | 对话消息列表 |
| `/api/sessions/{sid}/turns/message` | POST | 手工追加消息(`{role:user/agent/note,text,version_id?}`),不触发 Agent |
| `/api/sessions/{sid}/turns/{tid}` | GET/PATCH/DELETE | 单条对话的查/改文本角色关联/永久删除 |
| `/api/sessions/{sid}/versions/{vid}` | PATCH/DELETE | 改版本说明(代码不可原地改)/永久删除版本(当前版本回落父版本) |

对话与版本 ID 采用会话级单调计数器,删除后新增不会复用 ID,避免历史引用错乱。

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

## 方式四:MCP Server(agent 自主模式,推荐)

在 Claude Code / ZCode / Codex 等 agent 框架里接入后,**无需平台配置 LLM、无需启动
REST 服务**,agent 框架内的大模型即可通过对话完成「策略编写 → 校验 → 回测 → 诊断
迭代 → 结果汇报」全流程 —— 策略"思考"由 agent 自身模型完成,平台只提供确定性的
校验 / 回测 / 诊断 / 数据服务(与 Web 端同一套四层校验与保守参数)。

```bash
python -m aquant.mcp_server.server     # stdio 模式;direct 直调,默认无需 REST 服务
```

客户端配置示例(Claude Desktop / Claude Code / ZCode 的 mcpServers):

```json
{
  "mcpServers": {
    "aquant": {
      "command": "/path/to/aagent/.venv/bin/python",
      "args": ["-m", "aquant.mcp_server.server"],
      "env": { "AQUANT_MCP_MODE": "direct", "AQUANT_BUNDLE_PATH": "~/.aquant/bundle-baostock" }
    }
  }
}
```

**两种模式**(`AQUANT_MCP_MODE`,默认 `direct`):
- `direct`(默认):进程内直调领域服务 —— 不依赖 uvicorn、不依赖平台 LLM;token 闸门
  (AQUANT_MCP_TOKEN(S))语义与 REST 一致(fail-closed);
- `proxy`:经典薄桥,HTTP 转发到 `AQUANT_API_BASE`(默认 http://127.0.0.1:8000,需先启动
  服务);重编排场景(chat 会话轮次 / optimize)在 proxy 模式下可用平台 LLM。

**28 个工具**,agent 自主闭环核心:

- `platform_strategy_contract` — 策略代码契约(入口函数 / history_bars 一维数组语义 /
  下单规范 / 禁令清单 + 参考骨架),写码前必读;
- `platform_list_instruments` — 标的池 / 可用基准 / 数据区间(动态读 bundle);
- `platform_run_backtest` — 真实回测(可传 benchmark 对照;区间超出数据范围自动拉取
  最新行情),返回指标 + 净值曲线摘要 + 成交明细 + **人读 Markdown 报告**(可直接向用户汇报);
- `platform_diagnose_strategy` — 回测 + 规则诊断(无成交 / 换手过高 / 成本侵蚀 /
  回撤偏大 / 胜率偏低),改码方向由 agent 自行决定;
- `platform_rank_strategies` — 多候选按(夏普,年化,-回撤)排名,挑最优;
- `platform_validate_code` / `platform_verify_layered` / `platform_arena` / `platform_get_kline` /
  `platform_workflow_guide` — 校验 / 防过拟合 / 竞技场 / 行情 / 工作流指南;
- 会话工具(16 个):版本化保存、手动回测、回退、diff、增删改查(direct 下全部可用,
  且不依赖平台 LLM);`platform_session_turn`(agent 改码)与 `platform_optimize`
  需要平台 LLM,未配置时返回 503 并指引 agent 走自主闭环。

**典型对话流**(agent 在自己框架里完成):

```
用户:帮我写一个贵州茅台的双均线策略并回测
agent:platform_strategy_contract → platform_list_instruments(选 600519.XSHG)
      → 写码 → platform_validate_code(按 findings 修正)→ platform_run_backtest
      → 向用户汇报:总收益/相对沪深300 超额/最大回撤/成交笔数 + 报告
用户:胜率太低了,优化一下
agent:platform_diagnose_strategy → 依据线索改码 → 复测 → 对比前后指标 → 汇报
```

## 外部 Agent 模式(external-agent/)

在 ZCode / Claude Code / Codex 等**全能力 agent 平台**里完成全流程:外部平台选中任意文件夹作为项目目录(`strategies/` 存策略、`reports/` 存报告),agent 通过 MCP 工具完成「策略编辑→生成→回测→优化→验证」,全程在它们自己的对话里进行——本平台的 Web/CLI 此时只是后台引擎。

快速接入三步:

```bash
# 1. 启动平台 API(安装与 bundle 构建见「快速开始」)
uvicorn aquant.api.main:app --port 8000

# 2. 注册 MCP server(Claude Code 示例;python 需能 import aquant,建议用 .venv 绝对路径)
claude mcp add aquant -- python -m aquant.mcp_server.server

# 3. 安装方法论 SKILL.md(让 agent 自动按七步工作流执行)
mkdir -p ~/.claude/skills/aquant-quant && cp external-agent/SKILL.md ~/.claude/skills/aquant-quant/SKILL.md
```

MCP 侧环境变量:`AQUANT_API_BASE`(默认 http://127.0.0.1:8000)、`AQUANT_MCP_TOKEN`(平台启用 token 闸门时必填,回测/优化/验证需 readwrite 档)。

- `external-agent/SKILL.md` — agent 方法论:七步工作流(目录约定→写码→校验→回测→诊断→优化→分层验证/竞技场)、策略约束清单、错误处理表;
- `external-agent/README.md` — Claude Code / ZCode / Codex 各平台接入指南与通用 mcpServers 配置模板;
- `external-agent/project-template/` — 可复制的项目目录骨架(strategies/、reports/、.gitignore)。

---

## 真实数据构建

平台**默认数据源 = ETF**(全市场 ETF 轮动/配置策略开箱即用):

```bash
# 默认:全市场 ETF,东财(主)→ 新浪(备)双源降级,东财限流自动熔断切新浪
python -m aquant.data.build_bundle --source etf --out ~/.aquant/bundle-etf \
    --top 300 --start 2021-01-01 --end 2026-11-15
# 不传 --codes 时自动发现全市场 ETF 并按成交额取 Top-N(--top,默认 300)
# 自检输出:instruments 条数、ETF 合约(type=ETF/fund_type=ETF)、funds.h5、trading_dates
```

**ETF 数据源选型(为何是默认)**:基于 AKShare 聚合的免费源——
- 东方财富 `fund_etf_hist_em`/`fund_etf_spot_em`:覆盖全、支持复权,但 push2 行情易限流;
- 新浪 `fund_etf_hist_sina`/`fund_etf_category_sina`:实测 900+ 只 ETF、历史可回溯、稳定不限流;
- 实现**东财优先→新浪降级→东财熔断**:首批探测确认东财限流后本批次全走新浪,避免大池子逐只空等。
- ETF/LOF/REITs 合约写入 `funds.h5`(`type="ETF"` 且带 `fund_type`),个股写 `stocks.h5`。

个股数据(需时可选):

```bash
# 平台内置双源降级:东方财富(主)→ 新浪(备,东财 IP 限流时自动切换)
python -m aquant.data.build_bundle --source akshare \
    --codes 000001,600519 --start 2022-01-01 --end 2024-12-31 \
    --out ~/.aquant/bundle-stock
# 自检输出:instruments 加载、stocks.h5 首行(真实价 + 涨跌停价)
```

口径说明(重要):
- bundle 存**真实价**,复权因子在 `ex_cum_factor.h5`——策略信号计算可用复权价,
  撮合用真实价与真实涨跌停价,**不存在双重复权**;
- 现金分红进 `dividends.h5`(账户模块处理);AKShare 分红接口失败时价格复权仍正确
  (因子已含),仅缺现金到账,构建日志会警告;
- 区间首日无前收盘价,涨跌停字段为超宽占位值(回测从第 2 日起完全真实);
- 分层验证(`/api/verify`)的随机基准需从股票合约构造,**纯 ETF bundle 不支持**(返回 400 指引),
  如需分层验证请另建含个股的 bundle。

### 如何验证回测准确性(闭式对照,精确到分)

平台内置**解析可对照验证**:构造结果可手算的确定性策略(首日买入 100 股、末日卖出),
用 bundle 原始行情(h5 直读,不经引擎)+ 撮合/成本规则**闭式计算**期望值,与平台输出
逐项比对到分:

```bash
scripts/verify_backtest_accuracy.sh --bundle ~/.aquant/bundle-baostock --shares 100
# 或 MCP 工具:platform_verify_accuracy(无参数,agent 可随时自检引擎可信度)
```

验证规则(全部源自 rqalpha 6.5.0 源码核实,手算依据):
- 撮合价:`current_bar_close` = 下单当日 bar 收盘价;滑点 PriceRatioSlippage 全额双边:
  买入价 = close×1.002、卖出价 = close×0.998;
- 佣金 = max(成交金额×0.0008, 5 元/笔);
- **PIT 印花税率**(贴近真实历史):卖出日 < 2023-08-28 → 千一(0.001);
  ≥ 2023-08-28 → 万五(0.0005,财政部减半征收);
- 检查项:零交易资金恒等、买卖价格滑点公式、佣金/税率/期末总资产/总收益闭式对照、
  净值恒等式(末净值 = 1 + 总收益)、费用合计 = 逐笔合计等 18 项。

实测(600519.XSHG,2022-01-04 买入 → 2026-11-12 卖出):**18/18 全部通过**,
期末总资产闭式值 934,864.304973 与平台输出**差异为 0**。退出码可作 CI 门禁。

### 数据自动扩展与保留策略(标的与日期双自适应)

**标的不限白名单**:任意正常上市交易的 A 股股票/ETF 均可回测 —— 策略代码/benchmark
中出现数据池外(或已退市清理)的标的时,平台**自动在线获取行情**(Baostock 日线+复权
因子+停牌/ST,约 30~90 秒)并加入数据池;已退市/无行情的标的返回可读 422;
**长期未使用的标的自动清理**(instrument_usage.json 台账,默认 90 天未用且不在本次
请求中;基准指数永不清理;DuckDB 缓存按访问时间同步淘汰)。

回测区间超出 bundle 数据范围时(典型:数据截止后想回测最近月份),同样**自动拉取
最新行情**并重建数据,无需手动重建:

```bash
# 默认开启;显式关闭:AQUANT_BUNDLE_AUTO_EXTEND=0
export AQUANT_BUNDLE_AUTO_EXTEND=1
```

- 触发:请求区间超出 bundle 日历 → 自动按 [请求区间 ∪ 现有区间] 重建
  (复用 build_bundle 整套管线:真实价 + 板块涨跌停 + 复权因子;只走 Baostock 主力源
  以保证同步等待时长),随后**重注入真实基准指数**,原子换目录;
- 因子宽容:扩展场景对复权因子缺失日按上一有效因子前向填充(数据源因子表按分红
  事件发布,近期事件可能尚未落库);主构建保持"缺失即拒绝"的质量闸门;
- 旧数据删除:换新成功后旧 bundle 目录直接删除;DuckDB K 线缓存按最近访问时间
  清理(默认 90 天未访问的 oid 连同行情一并删除,LRU 容量淘汰不变);
- 安全阀:仅对真实构建的 bundle 生效(合成测试 bundle 不触发);合约数 >60 或
  跨度 >10 年跳过并提示手动构建;单文件锁防并发重建(残留锁 10 分钟自动接管);
- 前端会在回测完成后提示"数据 bundle 已自动更新:旧截止 → 新截止"。

### A股基准与收益图

回测界面(Chat / 工坊 / 多轮工作台)的收益图叠加**真实 A 股基准净值曲线**,与策略净值
同为"首日归一 = 1.0"口径,指标卡附带基准收益与超额(alpha/beta):

```bash
# 给既有 bundle 注入真实指数日线(Baostock,免 token):上证指数 + 沪深300
scripts/add_benchmark_index.sh ~/.aquant/bundle-baostock 000001.XSHG,000300.XSHG
# 幂等可重复执行;来源与行数写入 bundle/benchmark_meta.json(响应如实标注数据出处)
```

- **基准可自指定**:工坊基准框自由输入任意 bundle 内合约(上证指数 / 沪深300 /
  池内个股 / ETF,`none` 关闭;会话级基准随会话保存);未知 oid 直接给可读 400;
- 未注入真实指数的 bundle 中 000001.XSHG 为成分股等权合成基准(仅形态对照);
- API 侧:`/api/backtest` 与 `/api/chat` 返回 `portfolio_series` + `benchmark_series` +
  `trades` 明细(`[{date, side, price, quantity}]`);
- **收益图增强**(对标聚宽/米筐/TradingView):最大回撤区间阴影带(峰值→谷底,含
  −x.x% 谷底标记)、交易 B/S 箭头标记(买红下/卖绿上,同日合并 ×N)、固定 legend
  信息条(十字线跟随更新策略/基准/回撤/相对强弱)、**回撤水下副图**(与主图时间轴
  双向联动)、**底部拖柄自由调高**(180~720px,localStorage 记忆,双击复位)、
  **月度收益热力表**(相邻月末环比,红涨绿跌)、竞技场**多候选净值曲线叠加**
  (图例点选隐藏,基准灰虚线对照);
- **时间轴交互**(收益图/竞技场/K 线通用):鼠标左右**拖拽平移日期段**、滚轮/捏合
  **缩放**、双击时间轴复位,区间快捷按钮(1月/3月/6月/1年/全部)一键切窗;
  水下副图保持只读并与主图窗口联动。
- **每日持仓&收益面板**(聚宽「持仓&收益」同构):按交易日分组,逐持仓行
  (品种/多空/数量/**可用数量**(T+1 昨仓口径)/收盘价/市值/盈亏逐笔浮盈)+ Cash 行 +
  总共行(总资产 + 当日盈亏),红涨绿跌;倒序展示 + 按需加载更早日期;
  数据由平台日志 mod 在每日收盘结算时结构化采集,随响应/会话/MCP 全链路透出;
- **表头悬浮修复**:成交明细与每日持仓表格改为容器内滚动(420px 限高)+
  `border-collapse: separate` + sticky 表头 —— 数据再多、滚到任何位置列名始终可见;
- **聚宽式运行日志**(平台注入,不改策略契约):【初始化完成】【开盘前】持仓检查、
  【成交】(标的/名称/方向/股数/价格/金额/费用)、【拒单/撤单】WARNING、
  【收盘结算】逐日总资产/日收益/累计收益 + 逐持仓浮盈,全部带回测交易时间戳;
  策略代码里的 `print` / logger 输出按交易时间戳同步捕获透出;
  前端「运行日志」终端式面板(可折叠/复制/已截断标记)+「成交明细」表(买卖红绿)。
  回测页日志上限 2000 行,对话/会话/MCP 场景取末 300 行。

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
| `AQUANT_BUNDLE_PATH` | `~/.aquant/bundle-etf` | RQAlpha 数据 bundle 目录(启动脚本默认 ETF) |
| `AQUANT_LLM_BASE_URL` / `AQUANT_LLM_API_KEY` / `AQUANT_LLM_MODEL` | - | OpenAI 兼容 LLM 三件套(全设才启用) |
| `AQUANT_LLM_AUTH_SCHEME` | `bearer` | `bearer` 或 `api-key` |
| `AQUANT_LLM_TIMEOUT` | `120` | LLM 请求超时(秒) |
| `AQUANT_MCP_TOKEN` / `AQUANT_MCP_TOKENS` | - | token 闸门(未设=开放) |
| `AQUANT_ALLOW_UNSAFE` | - | `=1` 时豁免校验 error(结果打 unsafe 标,仅供研究) |
| `AQUANT_RQALPHA_ALLOW` | - | `=1` 时临时放行未登记的 RQAlpha 版本(仅供隔离验证;正式升级见下方脚本) |

## 测试与质量

```bash
python -m pytest tests/        # 283 个测试(单元/E2E/契约/GUI 辅助)
```

**升级 RQAlpha(无缝/可回滚)**:版本适配事实已收口到 `src/aquant/compat.py`,
`/api/health` 的 `rqalpha` 字段显示已装版本与兼容清单。新版本先在隔离环境完成
契约门禁 + 全量回归,通过后才写 `requirements.lock.txt`:

```bash
scripts/upgrade_rqalpha.sh                 # 自动取 PyPI 最新版验证
scripts/upgrade_rqalpha.sh 6.6.0           # 指定版本验证
scripts/upgrade_rqalpha.sh 6.6.0 --apply   # 验证通过后应用到当前 .venv(旧锁自动备份)
```

版本变更后必须重建 bundle(新旧目录并存),用 `AQUANT_BUNDLE_PATH` 切换并对比关键指标;
回滚执行脚本输出中提示的 `requirements.lock.txt.bak` 恢复命令即可。

- **端到端**:合成 bundle 真实跑通 rqalpha 回测,断言成本生效、成交量限制生效;
- **真实数据链路**:东财→新浪降级、复权因子单调性闸门、bundle 逐字段与 rqalpha
  源码常量比对;
- **GUI 系统测试**:web-gui-tester 方法论,10 个用户流全部通过
  (报告:[docs/system-test-report.md](docs/system-test-report.md),截图 [screenshots/](screenshots/));
- **code-review 双轴评审**已执行并修复(Standards 14 项 / Spec 11 项)。

### 规范文档

- [策略代码编写规范](docs/strategy-code-conventions.md):RQAlpha 日线策略结构、未来函数/外部数据/墙钟时间禁令、下单与配置门禁、提交前自查。
- [数据处理规范](docs/data-processing-conventions.md):Baostock 主力 + AKShare 补充、真实价与复权因子口径、跨源比价与超差回退、DuckDB 缓存与容量控制、ST/停牌/退市处理。
- [LLM 提示词模板](docs/llm-prompt-template.md):用任意大模型网页对话生成"百分百可回测"策略代码的提示词模板与工作流(与前端「编写指南」Tab 同源)。

## 项目结构

```
├── docs/               # design.md(架构+ADR)、system-test-report.md、gui-test-protocol.md
├── frontend/           # React 18 + Vite(编写指南/对话/校验台/竞技场/K线/收益图+基准)
├── scripts/            # start_platform.sh 一键启动;add_benchmark_index.sh 注入真实基准指数
├── screenshots/        # 系统测试截图证据
├── src/aquant/
│   ├── validation/     # Layer 1:AST 规则 + 配置 lint + findings
│   ├── config/         # Layer 3:defaults.yaml(唯一事实来源)+ schema
│   ├── data/           # Layer 2:provider/synthetic/akshare/cache/limits/adjust/bundle/benchmark
│   ├── runner/         # RQAlpha 子进程封装 + key_metrics
│   ├── verify/         # Layer 4:pipeline/metrics/arena
│   ├── agents/         # 4 子 agent + orchestrator + experience + prompts
│   ├── api/            # FastAPI + token_gate + kline + adapter + 会话端点
│   ├── session/        # 多轮会话/策略版本快照持久化 + 行级 diff
│   └── mcp_server/     # MCP 薄桥(24 工具)
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
默认支持 **6.5.0**(bundle 格式、`get_position` 签名、summary 键、trades 为 DataFrame
等已逐条核实);适配事实集中在 `aquant.compat`,新版本按上文"升级 RQAlpha"流程
先验证再放行,未登记版本会在回测入口 fail-fast。

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
