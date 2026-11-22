// 成交明细表(trades):按日期区间筛选,支持从近到远 / 从远到近。
// A 股语义:买入红 / 卖出绿;可折叠,默认展开最近 10 笔(倒序)。
import { useMemo, useState } from 'react';
import DateRangeFilter from './DateRangeFilter.jsx';

const SIDE_BUY = 'BUY';

export default function TradesTable({ trades, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [order, setOrder] = useState('desc');

  const allRows = useMemo(
    () => (Array.isArray(trades) ? trades.filter((t) => t && t.date) : []),
    [trades],
  );

  const rows = useMemo(() => {
    const direction = order === 'asc' ? 1 : -1;
    return allRows
      .filter((t) => {
        const day = String(t.date).slice(0, 10);
        if (start && day < start) return false;
        if (end && day > end) return false;
        return true;
      })
      .sort((a, b) => direction * String(a.date).localeCompare(String(b.date)));
  }, [allRows, start, end, order]);

  const reset = () => { setStart(''); setEnd(''); setOrder('desc'); };

  if (!allRows.length) return null;

  return (
    <details className="td-wrap" open={open} onToggle={(e) => setOpen(e.target.open)}>
      <summary className="td-summary">
        成交明细
        <span className="td-meta mono">
          {rows.length === allRows.length
            ? `${rows.length} 笔(${order === 'asc' ? '从远到近' : '从近到远'})`
            : `筛选 ${rows.length}/${allRows.length} 笔`}
        </span>
      </summary>
      {open && (
        <>
          <DateRangeFilter
            start={start} end={end} order={order}
            onStartChange={setStart} onEndChange={setEnd} onOrderChange={setOrder}
            onReset={reset} total={allRows.length} shown={rows.length} unit="笔"
          />
          <div className="td-scroll">
            <table className="td-table mono">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>标的</th>
                  <th>方向</th>
                  <th className="num">价格</th>
                  <th className="num">数量</th>
                  <th className="num">金额</th>
                  <th className="num">费用</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((t, i) => {
                  const buy = String(t.side).toUpperCase() === SIDE_BUY;
                  return (
                    <tr key={`${t.date}-${i}`} className={buy ? 'td-buy' : 'td-sell'}>
                      <td className="td-time">{String(t.date).slice(0, 16)}</td>
                      <td className="td-oid">
                        {t.order_book_id}
                        {t.symbol ? <span className="td-name">({t.symbol})</span> : null}
                      </td>
                      <td className="td-side">{buy ? '买入' : '卖出'}</td>
                      <td className="num">{Number(t.price).toLocaleString('zh-CN', { maximumFractionDigits: 3 })}</td>
                      <td className="num">{Number(t.quantity).toLocaleString('zh-CN')}</td>
                      <td className="num">{Number(t.amount ?? t.price * t.quantity).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}</td>
                      <td className="num">{Number((t.commission ?? 0) + (t.tax ?? 0)).toFixed(2)}</td>
                    </tr>
                  );
                })}
                {!rows.length && (
                  <tr><td colSpan="7" className="range-filter-empty">该日期区间内没有成交</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </details>
  );
}
