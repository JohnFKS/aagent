// 图表数据工具:净值序列归一化、最大回撤计算、月度收益表、交易标记清洗。
// 被NetValueChart(主图/水下副图)、MonthlyReturnsTable、ArenaCurveChart 共用。
//
// 口径约定:
// - 点形态统一为 {time: 'yyyy-mm-dd', value: number}(时间升序唯一,lightweight-charts 要求);
// - 回撤 = 1 - 净值/历史峰值(≤0,水下图直接画该值);
// - 月度收益 = 相邻月末净值环比 - 1;首月末以序列首日为基点。

// 只接受 'yyyy-mm-dd' 形态的日期,其余一律丢弃
export const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// 清洗后端序列([{date, net_value}]):过滤非法项,时间升序去重
export function normalizeSeries(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const points = [];
  for (const item of raw) {
    const date = typeof item?.date === 'string' ? item.date.slice(0, 10) : '';
    const value = Number(item?.net_value);
    if (!DATE_RE.test(date) || !Number.isFinite(value) || value <= 0 || seen.has(date)) continue;
    seen.add(date);
    points.push({ time: date, value });
  }
  points.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  return points;
}

// 交易明细清洗([{date, side, price, quantity}] → 图表 markers 形态):
// BUY → 红色上箭头在点下方('B',A 股语义买红);SELL → 绿色下箭头在点上方('S')。
// 多笔同日合并为一条标记(文本 ×N)。时间升序(setMarkers 要求)。
export function tradeMarkers(trades) {
  if (!Array.isArray(trades)) return [];
  const byDay = new Map();
  for (const t of trades) {
    const date = typeof t?.date === 'string' ? t.date.slice(0, 10) : '';
    const side = String(t?.side || '').toUpperCase();
    if (!DATE_RE.test(date) || (side !== 'BUY' && side !== 'SELL')) continue;
    const key = `${date}|${side}`;
    const cur = byDay.get(key);
    if (cur) cur.n += 1;
    else byDay.set(key, { time: date, side, n: 1 });
  }
  const out = [];
  for (const { time, side, n } of byDay.values()) {
    out.push({
      time,
      position: side === 'BUY' ? 'belowBar' : 'aboveBar',
      color: side === 'BUY' ? 'rgba(255, 93, 93, 0.95)' : 'rgba(62, 207, 142, 0.95)',
      shape: side === 'BUY' ? 'arrowUp' : 'arrowDown',
      text: `${side === 'BUY' ? 'B' : 'S'}${n > 1 ? `×${n}` : ''}`,
      _sortSide: side,
    });
  }
  // 同日既有买又有卖时按 卖在前/买在后 排(与 K 线图习惯一致,同日标记也要时间稳定)
  out.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : a._sortSide < b._sortSide ? -1 : 1));
  for (const m of out) delete m._sortSide;
  return out;
}

// 最大回撤:返回 {maxDd, peak, trough, window}
// - maxDd: 最大回撤幅度(正数,如 0.183)
// - peak/trough: {time, value} 峰值点/谷底点(谷底 = 窗口内净值最低点)
// - window: [{time, value}] 峰值→谷底区间的历史高水线(主图阴影带数据);
//   空回撤/数据不足时 maxDd = 0、window = []
export function computeDrawdown(points) {
  const empty = { maxDd: 0, peak: null, trough: null, window: [] };
  if (!Array.isArray(points) || points.length < 2) return empty;
  let peakValue = points[0].value;
  let peak = points[0];
  let best = empty;
  let window = [];
  for (const p of points) {
    if (p.value >= peakValue) {
      // 新高:重置峰值,清空当前窗口
      peakValue = p.value;
      peak = p;
      window = [];
      continue;
    }
    const dd = peakValue > 0 ? 1 - p.value / peakValue : 0;
    if (!window.length) window.push({ time: peak.time, value: peakValue });
    window.push({ time: p.time, value: peakValue }); // 高水线(阴影带上沿)
    if (best === empty || dd > best.maxDd) {
      best = { maxDd: dd, peak, trough: p, window: [...window] };
    }
  }
  return best;
}

// 全程回撤水下线:逐日 1 - 净值/历史峰值(≤0),水下副图数据
export function underwaterSeries(points) {
  if (!Array.isArray(points) || points.length < 2) return [];
  let peak = points[0].value;
  const out = [];
  for (const p of points) {
    if (p.value > peak) peak = p.value;
    out.push({ time: p.time, value: peak > 0 ? p.value / peak - 1 : 0 });
  }
  return out;
}

// 月度收益:相邻月末净值环比。返回 [{year, months: Array(12), yearReturn}](升序);
// months[i] ∈ [null, 收益小数];首年以序列首日净值为基点(年收益可算,不丢首年行)。
export function monthlyReturns(points) {
  if (!Array.isArray(points) || points.length < 2) return [];
  // 每月最后一个交易日净值
  const monthEnd = new Map();
  const first = points[0];
  for (const p of points) {
    const key = p.time.slice(0, 7); // 'yyyy-mm'
    monthEnd.set(key, p.value);
  }
  const keys = [...monthEnd.keys()].sort();
  const rows = [];
  let prevValue = null;
  let yearStart = null;
  let year = null;
  let curMonths = new Array(12).fill(null);
  const flushYear = () => {
    if (year === null) return;
    const base = yearStart ?? first.value; // 首年基点 = 序列首日
    rows.push({
      year,
      months: curMonths,
      yearReturn: base > 0 && prevValue != null ? prevValue / base - 1 : null,
    });
  };
  for (const key of keys) {
    const [y, m] = key.split('-');
    const value = monthEnd.get(key);
    const yNum = Number(y);
    if (yNum !== year) {
      flushYear();
      year = yNum;
      curMonths = new Array(12).fill(null);
      yearStart = prevValue; // 上年月末作为本年基点;首年为 null → 用序列首日
    }
    const base = prevValue ?? first.value;
    curMonths[Number(m) - 1] = base > 0 ? value / base - 1 : null;
    prevValue = value;
  }
  flushYear();
  return rows;
}
