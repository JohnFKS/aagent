# AQ-Platform MCP 功能端到端测试报告（2026-11-16）

## 测试环境

- Python 3.11.1；rqalpha 6.5.0（平台唯一放行版本）；aquant 0.1.0（工作区源码可直接 import）
- 本次安装 `mcp==2.3.0` SDK（项目未把 mcp 写入 pyproject.toml，server 用 `MCPServer` 兼容分支）
- 后端：`scripts/start_platform.sh 8000 /root/.aquant/bundle-real`，健康状态 ok
- 数据：`/root/.aquant/bundle-real`（726 个交易日，2022-01-04 ~ 2024-12-31，真实价）
- LLM：`.env` 中配置 `dots3-note-prev`（`https://note3-prev-api.askdiandian.com/v1`）

## 测试方式

用自制最小 MCP stdio 客户端 + 官方 `mcp` SDK 客户端（`stdio_client` + `ClientSession`）分别连接 `python -m aquant.mcp_server.server`，按 JSON-RPC 走完 initialize → tools/list → tools/call，全部工具调用均通过 MCP 协议，未直接调用后端 HTTP API（仅一次额外用 API 直接探测 LLM 连通性）。

## 结果矩阵

| 测试项 | 工具/命令 | 结果 |
|---|---|---|
| MCP 握手 | initialize | 通过：协议版本 2025-11-25（官方 SDK）/ 2025-06-18（自制客户端），serverInfo=aquant-platform |
| 工具注册 | tools/list | 通过：8 个工具齐全（health/validate/backtest/optimize/verify/kline/arena/workflow_guide），inputSchema 参数完整 |
| 工作流指南 | platform_workflow_guide | 通过：返回七步指南纯文本，无网络依赖 |
| 健康检查 | platform_health | 通过：status=ok，rqalpha 6.5.0 |
| 合规策略校验 | platform_validate_code | 通过：error_count=0，warning_count=0，8 条保守参数注入 info |
| 坏策略拦截 | platform_validate_code(lookahead_shift.py) | 通过：返回可读中文文本（1 error，`ast.shift_negative`，第 18 行），未抛堆栈 |
| 真实回测 | platform_run_backtest | 通过：359 交易日、16 笔成交，保守参数生效 |
| 行情查询 | platform_get_kline | 通过：返回 2024-06 的真实 OHLCV |
| AI 优化 | platform_optimize | 通过（功能正常，策略无改进）：2 轮、478s，第 1 轮 LLM 读超时被如实记录，第 2 轮候选按评分未超越基线，improved=false |
| 分层验证 | platform_verify_layered | 通过（策略 fail）：IS/OOS 切分 + 随机基准 + paper_checklist 齐全，status=fail 如实返回 |
| 多策略竞技场 | platform_arena | 通过：3 候选并行回测、按 (年化, 夏普) 排名、ranked_count=3 |
| MCP 单元测试 | `pytest tests/mcp/test_mcp_server.py` | 通过：49 passed |
| 参数错误语义 | 直接 POST /api/optimize rounds=0 | 422 + pydantic 校验信息（MCP 薄桥对非 2xx 统一转可读文本，单元测试已覆盖 422/400/401/连接失败） |

## 发现与备注

1. **mcp SDK 未声明为依赖**：`pyproject.toml` 未包含 `mcp`，新环境按 README 装完依赖后 MCP server 无法启动；需手动 `pip install mcp`。server 代码已兼容 mcp 1.x/2.x，2.3.0 实测正常。
2. **LLM 超时较常见**：优化第 1 轮 LLM 读超时（120s × 3 次尝试，约 6 分钟），平台按设计重试并如实记录，未伪装成功。短问题测试 LLM 秒级响应，推测与优化 prompt 较长、深度思考有关。
3. **serverInfo.version 为空**：initialize 返回的 `serverInfo.version` 是空字符串（只读展示问题，不影响工具调用）。
4. **优化耗时**：2 轮优化实测约 8 分钟，与 README 的 3~8 分钟区间相符；MCP 侧 900s 读超时足够。
5. **自制客户端曾卡在 optimize**：无 nohup 干扰的前台短调用正常；后改用官方 MCP SDK 客户端完成全部长耗时调用，未再复现。疑似自制客户端在后台重定向 stdout 时对长响应流的处理问题，非平台服务端缺陷（API 侧日志显示请求已进入 LLM 阶段并正常 200 返回）。
6. **策略结论**：本次编辑的策略在样本外年化 -4.03%，未通过 Layer 4；竞技场三名候选均为负收益。这是真实结果，已如实归档，不构成投资建议。

## 归档文件

- `reports/2026-11-16_ma_cross_trend_000001_validate.txt`
- `reports/2026-11-16_ma_cross_trend_000001_backtest.txt`
- `reports/2026-11-16_ma_cross_trend_000001_optimize.md`
- `reports/2026-11-16_ma_cross_trend_000001_verify.md`
- `reports/2026-11-16_arena.md`
- `strategies/ma_cross_trend_000001.py`（本次编辑策略）
- `strategies/ma_cross_slow_000001.py`（竞技场候选）
