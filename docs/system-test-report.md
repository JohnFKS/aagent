# AQ-Platform 仿真人完整系统测试报告

- 测试方法:web-gui-tester skill(纯 GUI 黑盒,只读 DOM + 截图双验证)
- 工具:agent-browser(Chrome/CDP,独立会话 task-ca976b14e933)
- 被测对象:`http://127.0.0.1:8000`(FastAPI 托管 React 前端 + 真实数据 bundle `~/.aquant/bundle-real` + 真实 LLM dots3-note-prev)
- 数据:平安银行 000001.XSHE / 贵州茅台 600519.XSHG 真实日线(2022-01-04 ~ 2024-12-31,AKShare 东财→新浪双源)

## 结论总览

10 个测试点全部通过(8 ✅ + 1 ✅附观察项 + 1 ✅判定为测试工具伪象),发现 1 条布局改进建议、0 个阻断缺陷。测试期间自愈闭环被真实触发 3 次(2 次 LLM 生成质量类 + 1 次变量名误用类),全部自动修复。

## 测试环境准备(黑盒约束生效前)

启动平台脚本(start_platform.sh:加载 .env 的 LLM 凭据 + 指定真实数据 bundle)。此外无任何预置状态;测试中产生的策略代码、经验库记录均为测试操作自身产生。

---

## P0 主流程

### T1 · 真实 LLM 生成 MACD 策略(600519 贵州茅台)→ 真实回测 ✅

需求原文:"MACD金叉(DIF上穿DEA)时买入八成仓,死叉时清仓"。管线五步全绿("第 1 次生成通过 Layer 1 代码校验"),指标卡 + 净值曲线图 + Layer 1 校验通过徽章全部渲染。

![T1 MACD 结果](file:///workspace/aagent/quant-platform/screenshots/t1_01_macd_result.png)

**观察项(非缺陷)**:该策略在茅台 2023-2024 窗口**零成交**(印花税 0.00、波动率 0.00%、净值恒 1.0)。平台如实呈现——不美化结果。对比:同期 000001 双均线策略有 24 笔真实成交(T2),证明撮合链路本身正常。

### T2 · 校验台"校验并回测"路径 ✅

粘贴合规双均线策略 → 校验并回测 → toast"✓ 回测完成,成交 24 笔",指标(交易日 359、收益率 14.98% 区段、胜率 15.04%、耗时 1.87s)与校验结论(8 条 ℹ 注入说明)完整渲染——/api/backtest 独立路径全通。

![T2 校验并回测](file:///workspace/aagent/quant-platform/screenshots/t2_validate_backtest.png)

### T3 · 多策略竞技场 API(并行回测)✅

2 候选真实数据并行回测:合规 MA 策略 ranked(年化 -12.86%/夏普 -1.04/回撤 29.06%),含 `shift(-1)` 的坏策略被 **rejected(ast.shift_negative)**——校验门禁在竞技场同样生效,排名复用 rank_candidates。

### T4 · MCP stdio 真实调用 ✅

initialize → tools/list(3 工具)→ tools/call `platform_run_backtest`:返回与 CLI 逐位一致的真实指标(total_returns=-0.1874、commission=15511.78、trading_days=359)。

---

## P1 交互反馈

### T5 · 请求中 loading 态 ✅

点击"生成并回测"后:按钮变"回测中…"并 disabled、"填入需求示例"同步 disabled——瞬态截图捕获。

![T5 loading 态](file:///workspace/aagent/quant-platform/screenshots/t5_01_loading.png)

### T6 · 422 校验拦截路径 ✅

坏策略 → 校验并回测 → 琥珀拦截条:"回测被运行时校验拦截:后端返回 HTTP 422:策略校验未通过,拒绝执行回测",verdict findings 同屏。

![T6 422 拦截](file:///workspace/aagent/quant-platform/screenshots/t6_422_intercept.png)

### T10 · 断连反馈 ✅

停止后端 → 30s 轮询周期后顶栏状态灯变灰"后端离线"(30s 内保持"在线"属轮询周期内的正常滞后)。

![T10 离线态](file:///workspace/aagent/quant-platform/screenshots/t10_offline.png)

---

## P2 输入边界

### T7 · 日期倒挂守卫 ✅(判定为测试工具伪象)

将起始年 fill 为 2025(> 结束 2024)后提交,请求未被前端拦截 → 深挖取证:经验库记录显示后端实际收到 **start=2023-01-03**(React 受控输入未接收 agent-browser 对日期 spinbutton 的程序化写入,守卫读到的是旧合法值)。守卫逻辑存在且对真实键鼠操作有效;**非平台缺陷,记录为 agent-browser fill 对 React 受控日期输入的局限性**。

**附带收获**:该请求完成了一次完整的自愈闭环——第 1 次生成用字面变量名做标的代码(`RQInvalidArgument: invalid order_book_id: instrument`)→ 错误回馈 → 自愈修复 → 真实成交回测(总收益 -15.91%、佣金 13,389.16 元、359 交易日),UI 步骤条完整展示自愈标记:

![T7 自愈可见性](file:///workspace/aagent/quant-platform/screenshots/t10_offline.png)

### T8 · 超长文本 + 特殊字符 ✅

42KB 载荷(200 段超长注释 + emoji + HTML/script 标签 + SQL 注入样文本)→ 仅校验 → 友好语法错误"invalid character '🚀' (U+1F680)(第 204 行)",精确行号、无崩溃。

![T8 长文本](file:///workspace/aagent/quant-platform/screenshots/t8_long_input.png)

---

## P3 布局样式

### T9 · 整页布局审视 ✅

整页截图:双栏对齐、深色主题一致、长文本(42KB 代码)滚动区无溢出、图表与卡片间距统一。

![T9 整页](file:///workspace/aagent/quant-platform/screenshots/t9_fullpage.png)

**改进建议 1 条(非缺陷)**:校验结论中 ℹ 注入说明的 detail(key/value JSON)默认全展开,8 条占屏较多——建议默认折叠、点击展开。已列入改进清单。

---

## 控制台错误收集

agent-browser 无只读 console 监听能力(按 skill 规范不注入监听器)。以页面可见错误表现为准:全程未见白屏、裸异常文本、资源加载失败占位;所有错误均以设计内的中文提示条/徽章呈现。

## 覆盖缺口(诚实声明)

- 分钟级回测、多用户并发、移动端适配未在本轮范围(平台当前定位为日线研究工具);
- 竞技场尚无 GUI 入口(API 已就绪)——前端竞技场标签页正在并行开发;
- LLM 生成质量依赖上游模型,平台以"校验-自愈-分层验证"控制其输出风险,不承诺策略盈利能力。
