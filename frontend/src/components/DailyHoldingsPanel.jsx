// 每日持仓&收益面板:按日期区间筛选,支持从近到远 / 从远到近。
// 数据来自 daily_holdings([{date, positions, cash, total_value, market_value,
// daily_pnl, daily_returns}]),表格内滚动 + 表头悬浮。
import { useMemo, useState } from 'react';
import DateRangeFilter from './DateRangeFilter.jsx';

const NUM = (v, digits = 2) =>
  Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });

function pnlCls(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return '';
  return n > 0 ? 'pnl-up' : 'pnl-down';
}

function DayGroup({ day }) {
  const positions = Array.isArray(day.positions) ? day.positions : [];
  const totalPnl = Number(day.daily_pnl ?? 0);
  return (
    <div className="dh-day">
      <div className="dh-date mono">
        {day.date}
        <span className={`dh-daypnl ${pnlCls(totalPnl)}`}>
          当日盈亏 {totalPnl >= 0 ? '+' : ''}{NUM(totalPnl)}
        </span>
        <span className={`dh-dayret ${pnlCls(day.daily_returns)}`}>
          {Number(day.daily_returns ?? 0) >= 0 ? '+' : ''}
          {(Number(day.daily_returns ?? 0) * 100).toFixed(2)}%
        </span>
      </div>
      <div className="dh-scroll">
        <table className="dh-table mono">
          <thead>
            <tr>
              <th>品种</th>
              <th>多空</th>
              <th className="num">数量</th>
              <th className="num">可用数量</th>
              <th className="num">收盘价</th>
              <th className="num">市值/价值</th>
              <th className="num">盈亏/逐笔浮盈</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p, i) => (
              <tr key={p.oid ?? i}>
                <td className="dh-name">{p.name}({p.oid})</td>
                <td>{p.direction ?? '多'}</td>
                <td className="num">{NUM(p.quantity, 0)}</td>
                <td className="num">{NUM(p.available, 0)}</td>
                <td className="num">{NUM(p.close, 3)}</td>
                <td className="num">{NUM(p.market_value)}</td>
                <td className={`num ${pnlCls(p.pnl)}`}>{p.pnl >= 0 ? '+' : ''}{NUM(p.pnl)}</td>
              </tr>
            ))}
            <tr className="dh-cash">
              <td className="dh-name">Cash</td><td>—</td><td className="num">—</td><td className="num">—</td>
              <td className="num">—</td><td className="num">{NUM(day.cash)}</td><td className="num">0.00</td>
            </tr>
            <tr className="dh-total">
              <td className="dh-name">总共</td><td>—</td><td className="num">—</td><td className="num">—</td>
              <td className="num">—</td><td className="num">{NUM(day.total_value)}</td>
              <td className={`num ${pnlCls(totalPnl)}`}>{totalPnl >= 0 ? '+' : ''}{NUM(totalPnl)}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function DailyHoldingsPanel({ dailyHoldings, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [order, setOrder] = useState('desc');
  const [limit, setLimit] = useState(10);

  const allDays = useMemo(
    () => (Array.isArray(dailyHoldings) ? dailyHoldings.filter((d) => d && d.date) : []),
    [dailyHoldings],
  );
  const days = useMemo(() => {
    const direction = order === 'asc' ? 1 : -1;
    return allDays
      .filter((d) => {
        const day = String(d.date).slice(0, 10);
        if (start && day < start) return false;
        if (end && day > end) return false;
        return true;
      })
      .sort((a, b) => direction * String(a.date).localeCompare(String(b.date)));
  }, [allDays, start, end, order]);

  const changeStart = (v) => { setStart(v); setLimit(10); };
  const changeEnd = (v) => { setEnd(v); setLimit(10); };
  const changeOrder = (v) => { setOrder(v); setLimit(10); };
  const reset = () => { setStart(''); setEnd(''); setOrder('desc'); setLimit(10); };

  if (!allDays.length) return null;

  return (
    <details className="dh-wrap" open={open} onToggle={(e) => setOpen(e.target.open)}>
      <summary className="dh-summary">
        持仓&amp;收益(每日)
        <span className="dh-meta mono">
          {days.length === allDays.length
            ? `${days.length} 个交易日(${order === 'asc' ? '从远到近' : '从近到远'})`
            : `筛选 ${days.length}/${allDays.length} 天`}
        </span>
      </summary>
      {open && (
        <>
          <DateRangeFilter
            start={start} end={end} order={order}
            onStartChange={changeStart} onEndChange={changeEnd} onOrderChange={changeOrder}
            onReset={reset} total={allDays.length} shown={days.length} unit="天"
          />
          {days.slice(0, limit).map((day) => <DayGroup key={day.date} day={day} />)}
          {!days.length && <div className="range-filter-empty">该日期区间内没有持仓记录</div>}
          {days.length > limit && (
            <button type="button" className="dh-more" onClick={() => setLimit((l) => l + 20)}>
              加载{order === 'asc' ? '更晚' : '更早'} {Math.min(20, days.length - limit)} 天(剩余 {days.length - limit})
            </button>
          )}
        </>
      )}
      {!open && <div className="dh-collapsed-hint">点击展开每日持仓明细(可筛选日期与排序)</div>}
    </details>
  );
}
