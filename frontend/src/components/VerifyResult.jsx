// 分层验证(Layer 4,/api/verify)结果渲染:
// - 状态徽章:pass(绿)/ warning(黄)/ fail(红);
// - IS/OOS 指标对照表(样本内 vs 样本外,格式化口径与 MetricsCards 一致);
// - comparison.checks 规则检查列表(✓/✗ + 阈值 + 说明);
// - 随机基准(random_baseline_returns)与模拟盘清单(paper_checklist);
// - 验证报告全文以可折叠 <pre> 展示。
// 后端字段都可能缺失,全部按防御式渲染(缺什么显示什么,不崩)。
import { useState } from 'react';
import { formatValue } from './MetricsCards.jsx';

// 状态 → 徽章样式与文案
const STATUS_META = {
  pass: { cls: 'badge-pass', icon: '✓', label: '验证通过' },
  warning: { cls: 'badge-warn', icon: '⚠', label: '有警告' },
  fail: { cls: 'badge-fail', icon: '✗', label: '未通过' },
};

// IS/OOS 对照表优先行(与 MetricsCards 的指标口径一致);其余键按出现顺序补在后面
const TABLE_ROWS = [
  { key: 'total_returns', label: '总收益', kind: 'percent', signed: true },
  { key: 'annualized_returns', label: '年化收益', kind: 'percent', signed: true },
  { key: 'sharpe', label: '夏普比率', kind: 'num2' },
  { key: 'max_drawdown', label: '最大回撤', kind: 'percent' },
  { key: 'volatility', label: '波动率', kind: 'percent' },
  { key: 'alpha', label: 'Alpha', kind: 'percent', signed: true },
  { key: 'beta', label: 'Beta', kind: 'num2' },
  { key: 'win_rate', label: '胜率', kind: 'percent' },
  { key: 'turnover', label: '换手率', kind: 'percent' },
  { key: 'benchmark_total_returns', label: '基准收益', kind: 'percent', signed: true },
  { key: 'total_commission', label: '总佣金', kind: 'cny' },
  { key: 'total_tax', label: '印花税', kind: 'cny' },
  { key: 'trading_days', label: '交易日', kind: 'int' },
];

// 布尔/标量 → 可读文本(对照摘要与 checks 通用)
function scalarToText(v) {
  if (v === undefined || v === null || v === '') return '—';
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(6)));
  if (typeof v === 'string') return v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

// is_split 形态未知,防御式格式化:对象(is_start/is_end/oos_start/oos_end)/
// 字符串 / 数组[isStart, isEnd, oosStart, oosEnd] 都能给出可读文本
function fmtSplit(split) {
  if (!split) return '';
  if (typeof split === 'string') return split;
  if (Array.isArray(split)) return split.join(' ~ ');
  if (typeof split === 'object') {
    if (split.is_start || split.oos_start) {
      return `样本内 ${split.is_start ?? '?'}~${split.is_end ?? '?'} · 样本外 ${split.oos_start ?? '?'}~${split.oos_end ?? '?'}`;
    }
    try {
      return JSON.stringify(split);
    } catch {
      return String(split);
    }
  }
  return String(split);
}

// 模拟盘清单条目:字符串或对象(item/text/message/title/description 等常见键)
function checklistItemToText(item, i) {
  if (item === undefined || item === null || item === '') return `第 ${i + 1} 项`;
  if (typeof item === 'string') return item;
  const keys = ['item', 'text', 'message', 'title', 'description', 'content', 'note'];
  for (const k of keys) {
    if (typeof item[k] === 'string' && item[k]) return item[k];
  }
  try {
    return JSON.stringify(item);
  } catch {
    return String(item);
  }
}

// IS/OOS 单元格:signed 且数值可解析时按正红负绿着色(A 股习惯,与指标卡一致)
function tableCell(row, metrics) {
  const v = metrics?.[row.key];
  let cls = '';
  if (row.signed && v !== undefined && v !== null && v !== '') {
    const n = Number(v);
    if (!Number.isNaN(n) && n !== 0) cls = n > 0 ? 'm-up' : 'm-down';
  }
  return (
    <td key={row.key} className={`mono ${cls}`.trim()}>
      {formatValue(row.kind, v)}
    </td>
  );
}

function buildTableRows(isMetrics, oosMetrics) {
  const has = (k) =>
    (isMetrics && isMetrics[k] !== undefined && isMetrics[k] !== null) ||
    (oosMetrics && oosMetrics[k] !== undefined && oosMetrics[k] !== null);
  const rows = TABLE_ROWS.filter((r) => has(r.key));
  // 两个指标 dict 里 TABLE_ROWS 之外的数值/字符串键,按字母序补充(未知字段不丢)
  const known = new Set(TABLE_ROWS.map((r) => r.key));
  const extraKeys = Array.from(
    new Set([...Object.keys(isMetrics || {}), ...Object.keys(oosMetrics || {})]),
  )
    .filter((k) => !known.has(k))
    .sort();
  for (const k of extraKeys) {
    rows.push({ key: k, label: k, kind: 'raw' });
  }
  return rows;
}

export default function VerifyResult({ data }) {
  const [reportOpen, setReportOpen] = useState(false);
  const status = String(data?.status || '').toLowerCase();
  const meta = STATUS_META[status] || STATUS_META.warning;
  const comparison = data?.comparison && typeof data.comparison === 'object' ? data.comparison : {};
  const checks = Array.isArray(comparison.checks) ? comparison.checks : [];
  const isMetrics = data?.is_metrics && typeof data.is_metrics === 'object' ? data.is_metrics : null;
  const oosMetrics =
    data?.oos_metrics && typeof data.oos_metrics === 'object' ? data.oos_metrics : null;
  // 随机基准:后端可能返回单值(随机信号平均收益)或数组(多组收益),两种都兼容
  const rawBaseline = data?.random_baseline_returns;
  const baselines = Array.isArray(rawBaseline)
    ? rawBaseline.filter((v) => Number.isFinite(Number(v))).map(Number)
    : Number.isFinite(Number(rawBaseline)) && rawBaseline !== null && rawBaseline !== ''
      ? [Number(rawBaseline)]
      : [];
  const baselineIsScalar = !Array.isArray(rawBaseline);
  const checklist = Array.isArray(data?.paper_checklist) ? data.paper_checklist : [];
  const report = typeof data?.report === 'string' ? data.report : '';

  // comparison 中除 checks/status 外的标量字段 → 对照摘要(未知字段兜底展示;
  // status 与顶部徽章重复,不重复渲染)
  const comparisonExtras = Object.entries(comparison)
    .filter(([k, v]) => k !== 'checks' && k !== 'status' && ['string', 'number', 'boolean'].includes(typeof v))
    .slice(0, 12);

  const baselineMean = baselines.length
    ? baselines.reduce((s, v) => s + Number(v), 0) / baselines.length
    : null;

  const rows = isMetrics || oosMetrics ? buildTableRows(isMetrics, oosMetrics) : [];

  return (
    <section className="studio-verify" aria-label="分层验证结果">
      <div className="verdict-head">
        <span className={`badge ${meta.cls}`}>
          {meta.icon} 分层验证{meta.label}
        </span>
        <span className="verdict-counts mono">
          {data?.start || '—'} ~ {data?.end || '—'}
          {fmtSplit(data?.is_split) ? ` · 切分:${fmtSplit(data.is_split)}` : ''}
        </span>
        {checks.length > 0 && (
          <span className="verdict-counts mono">
            检查 {checks.filter((c) => c?.passed).length}/{checks.length} 通过
          </span>
        )}
      </div>

      {/* IS / OOS 指标对照表 */}
      {rows.length > 0 ? (
        <div className="arena-table-wrap">
          <table className="arena-table studio-table">
            <thead>
              <tr>
                <th>指标</th>
                <th>样本内(IS)</th>
                <th>样本外(OOS)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key}>
                  <td className="studio-table-label">{r.label}</td>
                  {tableCell(r, isMetrics || {})}
                  {tableCell(r, oosMetrics || {})}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="findings-empty">后端未返回样本内/样本外指标(可能两次回测均未执行成功)。</div>
      )}

      {/* comparison 附加标量摘要(如衰减比等未知字段兜底) */}
      {comparisonExtras.length > 0 && (
        <div className="studio-extras mono">
          {comparisonExtras.map(([k, v]) => (
            <span key={k} className="studio-extra-chip" title={`comparison.${k}`}>
              {k} = {scalarToText(v)}
            </span>
          ))}
        </div>
      )}

      {/* checks 规则检查列表 */}
      {checks.length > 0 && (
        <>
          <div className="sub-head">规则检查({checks.length})</div>
          <ul className="studio-checks">
            {checks.map((c, i) => (
              <li key={i} className={`studio-check ${c?.passed ? 'studio-check-pass' : 'studio-check-fail'}`}>
                <span className="studio-check-icon" aria-hidden="true">
                  {c?.passed ? '✓' : '✗'}
                </span>
                <div className="studio-check-body">
                  <div className="studio-check-line">
                    <code className="mono">{String(c?.rule || 'unknown.rule')}</code>
                    <span className="mono studio-check-value">
                      值 {scalarToText(c?.value)} · 阈值 {scalarToText(c?.threshold)}
                    </span>
                  </div>
                  {c?.note ? <div className="studio-check-note">{String(c.note)}</div> : null}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* 随机基准 */}
      <div className="sub-head">
        随机基准{baselineIsScalar ? '(随机信号平均收益)' : `(${baselines.length} 组随机信号收益)`}
      </div>
      {baselines.length > 0 ? (
        <div className="studio-baseline">
          <span className="studio-baseline-mean mono">
            {baselineIsScalar ? '均值' : `${baselines.length} 组均值`} {formatValue('percent', baselineMean)}
          </span>
          <div className="studio-baseline-chips">
            {baselines.map((v, i) => (
              <span key={i} className="studio-baseline-chip mono">
                {formatValue('percent', v)}
              </span>
            ))}
          </div>
        </div>
      ) : (
        <div className="findings-empty">未返回随机基准数据。</div>
      )}

      {/* 模拟盘清单(实盘前人工确认项) */}
      <div className="sub-head">模拟盘清单({checklist.length} 项,实盘前逐条人工确认)</div>
      {checklist.length > 0 ? (
        <ul className="studio-paper">
          {checklist.map((item, i) => (
            <li key={i}>
              <span className="studio-paper-box" aria-hidden="true">
                ☐
              </span>
              {checklistItemToText(item, i)}
            </li>
          ))}
        </ul>
      ) : (
        <div className="findings-empty">未返回模拟盘清单。</div>
      )}

      {/* 验证报告全文:可折叠 <pre> */}
      {report ? (
        <div className="studio-report">
          <button
            type="button"
            className="code-toggle"
            onClick={() => setReportOpen((o) => !o)}
            aria-expanded={reportOpen}
          >
            <i className={`caret${reportOpen ? ' caret-open' : ''}`} aria-hidden="true">
              ▸
            </i>
            <span>验证报告全文</span>
          </button>
          {reportOpen && <pre className="studio-report-pre mono">{report}</pre>}
        </div>
      ) : null}
    </section>
  );
}
