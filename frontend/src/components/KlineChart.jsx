// K 线蜡烛图(lightweight-charts v4 CandlestickSeries),对应 docs/design.md §6"K 线图"计划项:
// - 数据来自 GET /api/kline 的 candles([{date,open,close,high,low,volume}],真实价日线,
//   后端已按 500 根上限抽样);空数组 / null / 全非法数据时不渲染(非关键路径);
// - A 股配色习惯:收涨(close >= open)红 #ff5d5d,收跌(绿 #3ecf8e)——
//   lightweight-charts v4 以 open <= close 判定 up,直接把 upColor 设为红即符合;
//   颜色复用 styles.css 的 --up/--down CSS 变量,其余主题色与 NetValueChart 同法;
// - 时区陷阱:与 NetValueChart 一致,日期字符串 'yyyy-mm-dd' 直接传给
//   lightweight-charts(按 UTC 解析),不做 new Date('...') 本地时区转换;
// - 十字线 tooltip 自实现:日期 + 开高低收 + 成交量,按收涨/收跌着色;
// - 响应式:ResizeObserver 跟随容器尺寸重设图表,组件卸载时 chart.remove()。
import { useEffect, useMemo, useRef } from 'react';
import { createChart, ColorType, CrosshairMode } from 'lightweight-charts';

// 只接受 'yyyy-mm-dd' 形态的日期,其余一律丢弃
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// 清洗后端 candles:过滤非法日期/非有限数值,日期升序去重(库要求 time 唯一递增)
function normalizeCandles(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const bars = [];
  for (const item of raw) {
    const date = typeof item?.date === 'string' ? item.date.slice(0, 10) : '';
    const open = Number(item?.open);
    const close = Number(item?.close);
    const high = Number(item?.high);
    const low = Number(item?.low);
    const volume = Number(item?.volume);
    const ok =
      DATE_RE.test(date) &&
      [open, close, high, low].every((v) => Number.isFinite(v) && v > 0) &&
      Number.isFinite(volume) &&
      low <= Math.min(open, close) &&
      Math.max(open, close) <= high;
    if (!ok || seen.has(date)) continue;
    seen.add(date);
    bars.push({ time: date, open, close, high, low, volume });
  }
  bars.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  return bars;
}

// '#rrggbb' → 'rgba(r,g,b,a)';非 hex 时原样返回(防御:变量被改写成 rgb() 形态)
function hexToRgba(hex, alpha) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

// 成交量格式化:股 → 万/亿(与 A 股行情软件口径一致)
function fmtVolume(v) {
  if (!Number.isFinite(v)) return '-';
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿股`;
  if (v >= 1e4) return `${(v / 1e4).toFixed(2)}万股`;
  return `${Math.round(v)}股`;
}

// crosshair 回调里的 time 可能是原始字符串 / BusinessDay 对象 / 内部 UTCTimestamp,
// 统一转回 'yyyy-mm-dd'(数值按 UTC 取 ISO 日期,不会偏移一天)
function formatDate(t) {
  if (typeof t === 'string') return t;
  if (t && typeof t === 'object' && t.year && t.month && t.day) {
    return `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`;
  }
  if (typeof t === 'number') {
    return new Date(t * 1000).toISOString().slice(0, 10);
  }
  return '';
}

export default function KlineChart({ candles }) {
  // useMemo/useRef/useEffect 恒定调用(hooks 顺序稳定),无数据时仅渲染 null
  const bars = useMemo(() => normalizeCandles(candles), [candles]);
  const bodyRef = useRef(null);
  const tipRef = useRef(null);

  useEffect(() => {
    const body = bodyRef.current;
    const tip = tipRef.current;
    if (!bars.length || !body || !tip) return undefined;

    // 主题色:运行时从 :root CSS 变量读取,与 styles.css 深色主题保持一致
    const css = getComputedStyle(document.documentElement);
    const readVar = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    const upColor = readVar('--up', '#ff5d5d'); // A 股:涨 = 红
    const downColor = readVar('--down', '#3ecf8e'); // A 股:跌 = 绿
    const accent = readVar('--accent', '#3aa2ff');
    const textColor = readVar('--text-dim', '#93a4b5');
    const gridColor = hexToRgba(readVar('--border-soft', '#1a2530'), 0.55);
    const borderColor = readVar('--border', '#223041');

    const chart = createChart(body, {
      width: body.clientWidth,
      height: body.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor,
        fontSize: 11.5,
        fontFamily: "SFMono-Regular, 'JetBrains Mono', Consolas, monospace",
        attributionLogo: false, // 关闭 TradingView 署名外链(4.1+ 官方选项)
      },
      grid: { vertLines: { color: gridColor }, horzLines: { color: gridColor } },
      rightPriceScale: { borderColor },
      timeScale: { borderColor, rightMargin: 6, fixLeftEdge: true, fixRightEdge: true },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: { color: hexToRgba(accent, 0.55), labelBackgroundColor: borderColor },
        horzLine: { color: hexToRgba(accent, 0.55), labelBackgroundColor: borderColor },
      },
      // 只读展示:不劫持页面滚动/捏合(crosshair 不受影响),与净值图一致
      handleScroll: false,
      handleScale: false,
    });

    // A 股习惯:收涨(close >= open)红、收跌绿;影线/边框同色
    const series = chart.addCandlestickSeries({
      upColor,
      downColor,
      borderUpColor: upColor,
      borderDownColor: downColor,
      wickUpColor: upColor,
      wickDownColor: downColor,
    });
    // volume 不进图(日线总览以价格为主),随 tooltip 展示
    series.setData(bars.map(({ time, open, close, high, low }) => ({ time, open, close, high, low })));

    chart.timeScale().fitContent();

    // tooltip:十字线移动跟随;OHLC 取值用 param.seriesData(不依赖 param.time 形态查值)
    const indexByTime = new Map(bars.map((b, i) => [b.time, i]));
    const hideTip = () => {
      tip.style.opacity = '0';
    };
    const onCrosshair = (param) => {
      if (!param.point || param.point.x < 0 || param.point.y < 0 || !param.time) {
        hideTip();
        return;
      }
      const bar = param.seriesData ? param.seriesData.get(series) : null;
      if (!bar || !Number.isFinite(Number(bar.close))) {
        hideTip();
        return;
      }
      const date = formatDate(param.time);
      // 收涨红 / 收跌绿(与蜡烛本体同规则)
      const idx = indexByTime.get(date) ?? 0;
      const cur = bars[idx] || bars[bars.length - 1];
      const cls = cur.close >= cur.open ? 'tip-up' : 'tip-down';

      tip.replaceChildren();
      const d1 = document.createElement('div');
      d1.className = 'tip-date';
      d1.textContent = date;
      const d2 = document.createElement('div');
      d2.className = `tip-value ${cls}`.trim();
      d2.textContent = `开 ${cur.open.toFixed(2)}  高 ${cur.high.toFixed(2)}  低 ${cur.low.toFixed(2)}  收 ${cur.close.toFixed(2)}`;
      const d3 = document.createElement('div');
      d3.className = 'tip-vol';
      d3.textContent = `量 ${fmtVolume(cur.volume)}`;
      tip.append(d1, d2, d3);

      // 位置跟随十字线,靠近右/上边缘时翻转,避免溢出容器
      const bw = body.clientWidth;
      const bh = body.clientHeight;
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      let x = param.point.x + 14;
      let y = param.point.y - th - 12;
      if (x + tw + 4 > bw) x = param.point.x - tw - 14;
      if (y < 4) y = param.point.y + 14;
      if (y + th + 4 > bh) y = bh - th - 4;
      tip.style.left = `${Math.max(2, x)}px`;
      tip.style.top = `${Math.max(2, y)}px`;
      tip.style.opacity = '1';
    };
    chart.subscribeCrosshairMove(onCrosshair);

    // 响应式:容器尺寸变化时同步图表宽高
    const ro = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) {
        chart.applyOptions({ width: Math.floor(width), height: Math.floor(height) });
      }
    });
    ro.observe(body);

    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [bars]);

  // 空数据 / null:不渲染任何东西(折叠区里由外层提示空态)
  if (!bars.length) return null;

  return (
    <section className="kline-chart" aria-label="K 线蜡烛图">
      <div className="kline-chart-head">
        <span className="kline-legend">
          <i className="legend-box legend-up" aria-hidden="true" />
          收涨(红)
          <i className="legend-box legend-down" aria-hidden="true" />
          收跌(绿)
          <span className="kline-note">真实价日线 · 最多 500 根</span>
        </span>
      </div>
      <div className="kline-chart-body" ref={bodyRef}>
        <div className="kline-tooltip" ref={tipRef} />
      </div>
    </section>
  );
}
