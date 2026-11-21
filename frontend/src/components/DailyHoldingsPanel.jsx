// 每日持仓&收益面板(聚宽「持仓&收益」同构):
// 按交易日分组,每组展示 逐持仓行(品种/多空/数量/可用数量/收盘价/市值/盈亏)
// + Cash 行(现金)+ 总共行(总资产 + 当日盈亏);倒序(最近一天在前)。
// 数据来自回测响应的 daily_holdings([{date, positions, cash, total_value,
// market_value, daily_pnl, daily_returns}]),缺失/空时不渲染。
// 表格内滚动 + 表头悬浮(sticky),数据多时列名始终可见。
import { useMemo, useState } from 'react';

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
              <td className="dh-name">Cash</td>
              <td>—</td>
              <td className="num">—</td>
              <td className="num">—</td>
              <td className="num">—</td>
              <td className="num">{NUM(day.cash)}</td>
              <td className="num">0.00</td>
            </tr>
            <tr className="dh-total">
              <td className="dh-name">总共</td>
              <td>—</td>
              <td className="num">—</td>
              <td className="num">—</td>
              <td className="num">—</td>
              <td className="num">{NUM(day.total_value)}</td>
              <td className={`num ${pnlCls(totalPnl)}`}>{totalPnl >= 0 ? '+' : ''}{NUM(totalPnl)}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function DailyHoldingsPanel({ dailyHoldings, defaultOpen = false }) {
  const days = useMemo(
    () => (Array.isArray(dailyHoldings) ? [...dailyHoldings].reverse() : []),
    [dailyHoldings],
  );
  const [open, setOpen] = useState(defaultOpen);
  const [limit, setLimit] = useState(10); // 倒序分页:先展示最近 10 天,按需加载更早

  if (!days.length) return null;

  return (
    <details
      className="dh-wrap"
      open={open}
      onToggle={(e) => setOpen(e.target.open)}
    >
      <summary className="dh-summary">
        持仓&amp;收益(每日)
        <span className="dh-meta mono">{days.length} 个交易日{days.length > limit && open ? '(显示最近 ' + limit + ' 天)' : ''}</span>
      </summary>
      {open && (
        <>
          {days.slice(0, limit).map((day) => (
            <DayGroup key={day.date} day={day} />
          ))}
          {days.length > limit && (
            <button type="button" className="dh-more" onClick={() => setLimit((l) => l + 20)}>
              加载更早 {Math.min(20, days.length - limit)} 天(剩余 {days.length - limit})
            </button>
          )}
        </>
      )}
      {!open && <div className="dh-collapsed-hint">点击展开每日持仓明细(品种/数量/可用/收盘价/市值/盈亏 + 当日盈亏)</div>}
    </details>
  );
}
