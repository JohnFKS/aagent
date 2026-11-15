# reports/ — 报告落盘目录

- 每个阶段(校验/回测/优化/验证/竞技场)结束后,由 agent 将结果整理落盘到本目录;
- 命名约定:`YYYY-MM-DD_<策略名>_<阶段>.md`(人读)或 `.json`(留档,原样保存工具返回),如 `2026-11-15_ma_cross_000001_backtest.md`;
- 阶段词固定:validate / backtest / optimize / verify / arena;
- verify 报告必须包含 paper_checklist(模拟盘引导清单)与"回测仅供研究、不构成投资建议"声明。
