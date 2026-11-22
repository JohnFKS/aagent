// 日期区间 + 时间方向筛选条(成交明细 / 每日持仓共用)。
export default function DateRangeFilter({
  start, end, order, onStartChange, onEndChange, onOrderChange, onReset,
  total, shown, unit = '条',
}) {
  return (
    <div className="range-filter">
      <input type="date" aria-label="起始日期" value={start} onChange={(e) => onStartChange(e.target.value)} />
      <span className="range-filter-sep">至</span>
      <input type="date" aria-label="结束日期" value={end} onChange={(e) => onEndChange(e.target.value)} />
      <select aria-label="日期排序" value={order} onChange={(e) => onOrderChange(e.target.value)}>
        <option value="desc">从近到远</option>
        <option value="asc">从远到近</option>
      </select>
      <button type="button" className="range-filter-reset" onClick={onReset}>重置</button>
      <span className="range-filter-count mono">{shown}/{total} {unit}</span>
    </div>
  );
}
