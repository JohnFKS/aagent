// 净值曲线图(lightweight-charts v4 Area 图),对应 docs/design.md §6"K 线图"第一阶段:
// - 数据来自 /api/chat 响应的 portfolio_series([{date, net_value}],归一化首日 = 1.0);
//   回测被拒/跳过时该字段为 null / 缺失 / 空数组,本组件直接不渲染(图表是非关键路径);
// - 主题:颜色全部从 styles.css 的 CSS 变量读取,背景透明、网格淡色、曲线用主强调色;
//   A 股"涨红跌绿"在单 series 单色下不可行,统一用强调色(指标卡仍保留红绿语义);
// - 基线 1.0 用虚线 priceLine;十字线跟随 tooltip 自实现(日期 + 净值,较前一点涨红/跌绿);
// - 时区陷阱:日期字符串 'yyyy-mm-dd' 直接传给 lightweight-charts(按 UTC 解析),
//   展示与原始日期一致,不做任何 new Date('...') 本地时区转换;
// - 响应式:ResizeObserver 跟随容器尺寸重设图表,组件卸载时 chart.remove()。
import { useEffect, useMemo, useRef } from 'react';
import { createChart, ColorType, CrosshairMode, LineStyle } from 'lightweight-charts';

// 只接受 'yyyy-mm-dd' 形态的日期,其余一律丢弃
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// 清洗后端序列:过滤非法日期/非有限数值,日期升序去重(库要求 time 唯一递增)
function normalizeSeries(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const points = [];
  for (const item of raw) {
    const date = typeof item?.date === 'string' ? item.date.slice(0, 10) : '';
    const value = Number(item?.net_value);
    if (!DATE_RE.test(date) || !Number.isFinite(value) || seen.has(date)) continue;
    seen.add(date);
    points.push({ time: date, value });
  }
  points.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  return points;
}

// '#rrggbb' → 'rgba(r,g,b,a)';非 hex 时原样返回(防御:变量被改写成 rgb() 形态)
function hexToRgba(hex, alpha) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
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

export default function NetValueChart({ series }) {
  // useMemo/useRef/useEffect 恒定调用(hooks 顺序稳定),无数据时仅渲染 null
  const points = useMemo(() => normalizeSeries(series), [series]);
  const bodyRef = useRef(null);
  const tipRef = useRef(null);

  useEffect(() => {
    const body = bodyRef.current;
    const tip = tipRef.current;
    if (!points.length || !body || !tip) return undefined;

    // 主题色:运行时从 :root CSS 变量读取,与 styles.css 深色主题保持一致
    const css = getComputedStyle(document.documentElement);
    const readVar = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
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
        attributionLogo: false, // 关闭 TradingView 署名外链(4.1+ 官方选项;只读总览图不留可点击外链)
      },
      grid: { vertLines: { color: gridColor }, horzLines: { color: gridColor } },
      rightPriceScale: { borderColor },
      timeScale: { borderColor, rightMargin: 6, fixLeftEdge: true, fixRightEdge: true },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: { color: hexToRgba(accent, 0.55), labelBackgroundColor: borderColor },
        horzLine: { color: hexToRgba(accent, 0.55), labelBackgroundColor: borderColor },
      },
      // 总览图不劫持页面滚动/捏合,只读展示(crosshair 不受影响)
      handleScroll: false,
      handleScale: false,
    });

    const area = chart.addAreaSeries({
      lineColor: accent,
      topColor: hexToRgba(accent, 0.26),
      bottomColor: 'rgba(58, 162, 255, 0)',
      lineWidth: 2,
      priceLineVisible: false, // 不画"最新值"水平线,只保留 1.0 基线
      lastValueVisible: true,
    });
    area.setData(points);

    // 基线 1.0:虚线价格线(净值图习惯,一眼看出相对首日盈亏)
    area.createPriceLine({
      price: 1.0,
      color: textColor,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: '基准 1.0',
    });

    chart.timeScale().fitContent();

    // tooltip:十字线移动跟随;点的坐标取自 param.point,取值用 param.seriesData
    // (不依赖 param.time 的具体形态做查值,仅用于展示日期)
    const indexByTime = new Map(points.map((p, i) => [p.time, i]));
    const hideTip = () => {
      tip.style.opacity = '0';
    };
    const onCrosshair = (param) => {
      if (!param.point || param.point.x < 0 || param.point.y < 0 || !param.time) {
        hideTip();
        return;
      }
      const bar = param.seriesData ? param.seriesData.get(area) : null;
      const value = bar ? Number(bar.value) : NaN;
      if (!Number.isFinite(value)) {
        hideTip();
        return;
      }
      const date = formatDate(param.time);
      // 较前一点涨红 / 跌绿(首点与 1.0 基线比较)
      const idx = indexByTime.get(date) ?? 0;
      const prev = idx > 0 ? points[idx - 1].value : 1.0;
      const cls = value > prev ? 'tip-up' : value < prev ? 'tip-down' : '';

      tip.replaceChildren();
      const d1 = document.createElement('div');
      d1.className = 'tip-date';
      d1.textContent = date;
      const d2 = document.createElement('div');
      d2.className = `tip-value ${cls}`.trim();
      d2.textContent = value.toFixed(4);
      tip.append(d1, d2);

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
  }, [points]);

  // 空数据 / null:不渲染任何东西
  if (!points.length) return null;

  return (
    <section className="netvalue-chart" aria-label="策略净值曲线">
      <div className="netvalue-chart-head">
        <span className="netvalue-title">净值曲线</span>
        <span className="netvalue-legend">
          <i className="legend-swatch" aria-hidden="true" />
          策略净值(首日归一 = 1.0)
          <i className="legend-line" aria-hidden="true" />
          基准 1.0
        </span>
      </div>
      <div className="netvalue-chart-body" ref={bodyRef}>
        <div className="netvalue-tooltip" ref={tipRef} />
      </div>
    </section>
  );
}
