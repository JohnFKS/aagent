// 竞技场净值曲线叠加图:多候选策略净值同图对照 + 基准线(金色)。
// 数据来自 /api/arena 响应:details[name].portfolio_series(各候选)+ benchmark_series。
// 候选按排名顺序配色(榜首蓝色主强调,其后依次紫/青/橙/粉/灰),图例可点选
// 临时隐藏某条曲线(只影响显示,不影响排名)。
import { useEffect, useMemo, useRef, useState } from 'react';
import { createChart, ColorType, CrosshairMode, LineStyle } from 'lightweight-charts';
import { normalizeSeries } from '../chartUtils.js';

// 候选配色(第 1 名用主强调蓝,后续与深色主题协调的区分度色板)
const PALETTE = [
  '#3aa2ff', // 主强调蓝
  '#7c6cff', // 紫(accent-2)
  '#e6b450', // 金
  '#3ecf8e', // 绿
  '#ff8fab', // 粉
  '#c9d1d9', // 灰
];

function hexToRgba(hex, alpha) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function formatDate(t) {
  if (typeof t === 'string') return t;
  if (t && typeof t === 'object' && t.year && t.month && t.day) {
    return `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`;
  }
  if (typeof t === 'number') return new Date(t * 1000).toISOString().slice(0, 10);
  return '';
}

// details {name: {status, portfolio_series?}} → [{name, points}](只收 ranked 且有数据的)
export default function ArenaCurveChart({ details, benchmarkSeries, benchmarkLabel = 'A股基准' }) {
  const seriesList = useMemo(() => {
    if (!details || typeof details !== 'object') return [];
    const out = [];
    for (const [name, info] of Object.entries(details)) {
      const points = normalizeSeries(info?.portfolio_series);
      if (points.length) out.push({ name, points });
    }
    return out.slice(0, PALETTE.length);
  }, [details]);
  const bmPoints = useMemo(() => normalizeSeries(benchmarkSeries), [benchmarkSeries]);

  // 临时隐藏的候选(图例点选切换;重新回测会重置)
  const [hidden, setHidden] = useState(() => new Set());
  const visible = seriesList.filter((s) => !hidden.has(s.name));

  const bodyRef = useRef(null);
  const tipRef = useRef(null);

  useEffect(() => {
    const body = bodyRef.current;
    const tip = tipRef.current;
    if (!visible.length || !body || !tip) return undefined;

    const css = getComputedStyle(document.documentElement);
    const readVar = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    const textColor = readVar('--text-dim', '#93a4b5');
    const gridColor = hexToRgba(readVar('--border-soft', '#1a2530'), 0.55);
    const borderColor = readVar('--border', '#223041');
    const accent = readVar('--accent', '#3aa2ff');

    const chart = createChart(body, {
      width: body.clientWidth,
      height: body.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor,
        fontSize: 11.5,
        fontFamily: "SFMono-Regular, 'JetBrains Mono', Consolas, monospace",
        attributionLogo: false,
      },
      grid: { vertLines: { color: gridColor }, horzLines: { color: gridColor } },
      rightPriceScale: { borderColor },
      timeScale: { borderColor, rightMargin: 6, fixLeftEdge: true, fixRightEdge: true },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: { color: hexToRgba(accent, 0.55), labelBackgroundColor: borderColor },
        horzLine: { color: hexToRgba(accent, 0.55), labelBackgroundColor: borderColor },
      },
      handleScroll: false,
      handleScale: false,
    });

    const lines = [];
    for (let i = 0; i < visible.length; i += 1) {
      const color = PALETTE[i % PALETTE.length];
      const line = chart.addLineSeries({
        color,
        lineWidth: visible.length > 3 ? 1.6 : 2,
        priceLineVisible: false,
        lastValueVisible: true,
      });
      line.setData(visible[i].points);
      lines.push({ name: visible[i].name, line, color });
    }
    let bmLine = null;
    if (bmPoints.length) {
      bmLine = chart.addLineSeries({
        color: textColor,
        lineWidth: 1.2,
        lineStyle: LineStyle.Dashed,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      bmLine.setData(bmPoints);
    }
    chart.timeScale().fitContent();

    const onCrosshair = (param) => {
      if (!param.point || !param.time) {
        tip.style.opacity = '0';
        return;
      }
      const date = formatDate(param.time);
      tip.replaceChildren();
      const d0 = document.createElement('div');
      d0.className = 'tip-date';
      d0.textContent = date;
      tip.append(d0);
      for (const { name, line, color } of lines) {
        const bar = param.seriesData ? param.seriesData.get(line) : null;
        if (!bar) continue;
        const row = document.createElement('div');
        row.className = 'tip-value';
        row.style.color = color;
        row.textContent = `${name} ${Number(bar.value).toFixed(4)}`;
        tip.append(row);
      }
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
  }, [visible, bmPoints]);

  if (!visible.length) return null;

  const toggle = (name) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <section className="netvalue-chart arena-curve" aria-label="竞技场净值曲线叠加">
      <div className="netvalue-chart-head">
        <span className="netvalue-title">净值曲线叠加</span>
        <span className="netvalue-legend arena-curve-legend">
          {seriesList.map((s, i) => (
            <button
              key={s.name}
              type="button"
              className={`arena-curve-legend-item ${hidden.has(s.name) ? 'is-hidden' : ''}`}
              style={{ color: PALETTE[i % PALETTE.length] }}
              onClick={() => toggle(s.name)}
              title="点击隐藏/显示该候选曲线"
            >
              <i className="legend-swatch" style={{ background: PALETTE[i % PALETTE.length] }} aria-hidden="true" />
              {s.name}
            </button>
          ))}
          {bmPoints.length > 0 && (
            <span className="arena-curve-legend-item is-bm">
              <i className="legend-line" aria-hidden="true" />
              {benchmarkLabel}
            </span>
          )}
        </span>
      </div>
      <div className="netvalue-chart-body" ref={bodyRef}>
        <div className="netvalue-tooltip" ref={tipRef} />
      </div>
    </section>
  );
}
