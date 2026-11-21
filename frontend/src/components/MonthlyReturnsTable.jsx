// 月度收益热力表(聚宽/米筐回测报告页标配):由净值序列计算相邻月末环比,
// 行 = 年份,列 = 1~12 月 + 年收益;红涨绿跌(A 股语义),灰 = 无数据。
// 折叠展示(details),放在收益图下方;数据来自 portfolio_series,纯前端计算。
import { useMemo, useState } from 'react';
import { monthlyReturns, normalizeSeries } from '../chartUtils.js';

function pct(v) {
  if (v == null) return '—';
  const p = v * 100;
  return `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`;
}

// 月份表头(1~12 月,两位数紧凑)
const MONTH_COLS = Array.from({ length: 12 }, (_, i) => `${i + 1}月`);
const MONTH_SHORT = Array.from({ length: 12 }, (_, i) => `${i + 1}`);

export default function MonthlyReturnsTable({ series }) {
  const points = useMemo(() => normalizeSeries(series), [series]);
  const rows = useMemo(() => monthlyReturns(points), [points]);
  const [open, setOpen] = useState(false);

  if (rows.length === 0) return null;

  const cellClass = (v) => {
    if (v == null) return 'mrt-cell mrt-empty';
    if (v > 0) return 'mrt-cell mrt-up';
    if (v < 0) return 'mrt-cell mrt-down';
    return 'mrt-cell mrt-flat';
  };

  return (
    <details className="mrt-wrap" open={open} onToggle={(e) => setOpen(e.target.open)}>
      <summary className="mrt-summary">
        月度收益热力表
        <span className="mrt-summary-hint mono">{points[0]?.time ?? ''} ~ {points[points.length - 1]?.time ?? ''}</span>
      </summary>
      {open && (
        <div className="mrt-scroll">
          <table className="mrt-table mono">
            <thead>
              <tr>
                <th className="mrt-year-h">年份</th>
                {MONTH_COLS.map((m) => <th key={m}>{m}</th>)}
                <th>年收益</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.year}>
                  <td className="mrt-year">{row.year}</td>
                  {row.months.map((v, i) => (
                    <td key={`${row.year}-${i}`} className={cellClass(v)} title={`${MONTH_SHORT[i]}月:${pct(v)}`}>
                      {v == null ? '·' : (v * 100).toFixed(1)}
                    </td>
                  ))}
                  <td className={cellClass(row.yearReturn)}>{pct(row.yearReturn)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!open && <div className="mrt-collapsed-hint">点击展开查看逐月收益(%)</div>}
    </details>
  );
}
