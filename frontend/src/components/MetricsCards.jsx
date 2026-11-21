// 回测指标卡片:总收益/年化/夏普/最大回撤/佣金/印花税/交易日 等核心指标。
// - 百分比两位小数;收益类正值红色、负值绿色(A股习惯:涨=红、跌=绿);
// - 后端 metrics 任一键都可能缺失,缺失显示 "—";
// - metrics 本身为 null(被拒/未执行回测)时显示空态说明。

// 按百分比展示的键(后端约定:0.1234 → 12.34%)
const PERCENT_KEYS = new Set([
  'total_returns',
  'annualized_returns',
  'benchmark_total_returns',
  'max_drawdown',
  'volatility',
  'alpha',
  'beta',
  'turnover',
  'win_rate',
]);

// 核心指标卡(缺失也占位显示 "—";基准收益进核心卡,避免有/无基准时网格整体跳动)
const CORE_CARDS = [
  { key: 'total_returns', label: '总收益', kind: 'percent', signed: true },
  { key: 'annualized_returns', label: '年化收益', kind: 'percent', signed: true },
  // 实测后端 max_drawdown 为正数幅度(如 0.3795 → 37.95%),非收益,不着正红
  { key: 'max_drawdown', label: '最大回撤', kind: 'percent', signed: false },
  { key: 'benchmark_total_returns', label: '基准收益', kind: 'percent', signed: true },
  { key: 'sharpe', label: '夏普比率', kind: 'num2' },
  { key: 'total_commission', label: '总佣金', kind: 'cny' },
  { key: 'total_tax', label: '印花税', kind: 'cny' },
  { key: 'trading_days', label: '交易日', kind: 'int' },
];

// 扩展指标卡:仅在后端返回了对应键时才展示
const EXTRA_CARDS = [
  { key: 'volatility', label: '波动率', kind: 'percent' },
  { key: 'alpha', label: 'Alpha', kind: 'percent', signed: true },
  { key: 'beta', label: 'Beta', kind: 'num2' },
  { key: 'turnover', label: '换手率', kind: 'percent' },
  { key: 'win_rate', label: '胜率', kind: 'percent' },
  { key: 'elapsed_seconds', label: '耗时', kind: 'sec' },
];

// 数值格式化:缺失/非法 → '—'(导出供 VerifyResult 的 IS/OOS 对照表复用,口径一致)
export function formatValue(kind, v) {
  if (v === undefined || v === null || v === '') return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  switch (kind) {
    case 'percent':
      return `${(n * 100).toFixed(2)}%`;
    case 'num2':
      return n.toFixed(2);
    case 'cny':
      return `${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 元`;
    case 'int':
      return String(Math.round(n));
    case 'sec':
      return `${n.toFixed(2)} s`;
    default:
      return String(v);
  }
}

// 收益类正红负绿(A股习惯):signed 卡片按符号着色
function valueCls(card, v) {
  if (!card.signed || v === undefined || v === null || v === '') return '';
  const n = Number(v);
  if (Number.isNaN(n) || n === 0) return '';
  return n > 0 ? 'm-up' : 'm-down';
}

function MetricCard({ card, value }) {
  return (
    <div className="metric-card">
      <div className="metric-label">{card.label}</div>
      <div className={`metric-value mono ${valueCls(card, value)}`}>{formatValue(card.kind, value)}</div>
    </div>
  );
}

export default function MetricsCards({ metrics }) {
  // 空态:被校验拦截 / 未执行回测 / 后端未返回
  if (!metrics || typeof metrics !== 'object' || Object.keys(metrics).length === 0) {
    return (
      <div className="metrics-empty">
        本次未产生回测指标(可能被校验拦截、未执行回测或后端未返回)。
      </div>
    );
  }

  const extras = EXTRA_CARDS.filter(
    (c) => metrics[c.key] !== undefined && metrics[c.key] !== null,
  );

  return (
    <section className="metrics">
      <div className="metrics-head">
        <span className="metrics-title">回测指标</span>
        {(metrics.start || metrics.end) && (
          <span className="metrics-range mono">
            {metrics.start || '—'} ~ {metrics.end || '—'}
          </span>
        )}
        {metrics.unsafe ? (
          <span className="unsafe-chip" title={JSON.stringify(metrics.unsafe)}>
            ⚠ 存在不可复现项(unsafe)
          </span>
        ) : null}
      </div>
      <div className="metrics-grid">
        {CORE_CARDS.map((c) => (
          <MetricCard key={c.key} card={c} value={metrics[c.key]} />
        ))}
        {extras.map((c) => (
          <MetricCard key={c.key} card={c} value={metrics[c.key]} />
        ))}
      </div>
    </section>
  );
}
