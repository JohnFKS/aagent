# 真人仿真测试协议(GUI 黑盒)

测试工具:agent-browser(Chrome/CDP,独立会话,accessibility-tree 快照驱动)。
被测对象:完整平台(FastAPI 托管前端 dist + 真实数据 bundle `~/.aquant/bundle-real`)。
测试立场:黑盒——只通过 UI 操作,不绕过界面直接调 API;模拟真实用户的目标、耐心与误操作。

## 用户画像与流程

| # | 画像 | 流程 | 预期 | 证据 |
|---|------|------|------|------|
| F1 | 首次访问者 | 打开首页,观察平台状态 | 顶栏状态灯绿色(health ok),两栏布局完整渲染 | 截图 01-home |
| F2 | 策略新手 | 点"填入需求示例"→ 点"生成并回测"→ 等待 | 步骤条 edit→validate→backtest→review→report 逐个点亮;指标卡出现(收益为负显示绿色/负值);findings 含 8 条 ℹ 注入说明 | 截图 02-chat-backtest |
| F3 | 谨慎用户 | 点"填入坏策略示例"→ 点"仅校验" | passed=false 徽章;ast.shift_negative 的 ✗ ERROR 置顶;中文解释可读 | 截图 03-validate-reject |
| F4 | 手动输入用户 | 自己键入需求文本(非按钮)→ 回测 | 同 F2,证明输入路径真实可用 | 截图 04-manual-input |
| F5 | 边界测试 | 空输入点按钮 / 断开场景 | 前端友好错误提示,无白屏无裸异常 | 截图 05-edge |
| F6 | 报告阅读者 | 展开"查看完整报告" | 分层验证/校验结论/指标表全部可见 | 截图 06-report |

## 通过标准
- 每个流程核心断言成立(见预期列);
- 无 console 级致命错误导致的白屏;
- 截图存 quant-platform/screenshots/,命名与上表一致。

## 已知非目标
- 性能压测、多用户并发、鉴权(本机单用户模式)、移动端像素级适配。
