"""AQ-Platform:基于 RQAlpha 的 A 股回测正确性平台。

四层校验体系:
- Layer 1 代码自动校验层(validation):Agent 生成代码后、回测执行前,静态检查
  T+1、涨跌停、手续费完整性、未来函数风险、可复现性。
- Layer 2 统一数据口径层(data):AKShare/合成数据 + 本地缓存,统一复权方式
  (真实价 + ex_cum_factor),板块涨跌停价规则,RQAlpha bundle 构建。
- Layer 3 保守默认参数层(config):强制注入 0.2% 滑点、成交量占比限制,
  禁止零滑点/零费率/关闭 T+1。
- Layer 4 分层验证流程(verify):样本内回测 → 样本外回测 → 模拟盘引导清单。
"""

__version__ = "0.1.0"
