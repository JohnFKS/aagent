// 成交明细表(trades):时间 / 标的(名称) / 方向 / 价格 / 数量 / 金额 / 费用。
// A 股语义:买入红 / 卖出绿;可折叠,默认展开最近 10 笔(倒序)。
// 数据来自回测响应的 trades([{date, order_book_id, symbol, side, price, quantity,
// amount, commission, tax}]),缺失/空时不渲染。
import { useMemo, useState } from 'react';

const SIDE_BUY = 'BUY';

export default function TradesTable({ trades, defaultOpen = true }) {
  const rows = useMemo(() => {
    if (!Array.isArray(trades)) return [];
    return trades.filter((t) => t && t.date);
  }, [trades]);
  const [open, setOpen] = useState(defaultOpen);

  if (!rows.length) return null;

  const display = [...rows].reverse(); // 最近成交在前

  return (
    <details className="td-wrap" open={open} onToggle={(e) => setOpen(e.target.open)}>
      <summary className="td-summary">
        成交明细
        <span className="td-meta mono">{rows.length} 笔(最近在前)</span>
      </summary>
      {open && (
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
              {display.map((t, i) => {
                const buy = String(t.side).toUpperCase() === SIDE_BUY;
                return (
                  <tr key={i} className={buy ? 'td-buy' : 'td-sell'}>
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
            </tbody>
          </table>
        </div>
      )}
    </details>
  );
}
