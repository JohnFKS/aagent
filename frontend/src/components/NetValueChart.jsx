// 收益图(lightweight-charts v4):策略净值 Area 图 + A股基准 Line 图 + 回撤分析。
// 对标成熟量化平台(聚宽/米筐回测报告、TradingView)的净值图交互:
// - **主图**:策略净值(蓝,首日归一 1.0)+ 基准净值(金)+ 1.0 虚线基线 +
//   **最大回撤区间阴影带**(峰值→谷底,红色半透明,谷底标记 −x.x%)+
//   **交易标记**(B 红上箭头 / S 绿下箭头,来自 /api/backtest 的 trades 明细);
// - **水下副图**(默认开启):全程回撤百分比曲线(1 - 净值/历史峰值),
//   与主图 timeScale 双向联动、十字线同步,直观看到每段回撤的深度与时长;
// - **固定 legend 信息条**:默认显示最新值,鼠标悬停显示对齐日期的策略/基准/回撤/相对强弱
//   (TradingView 式,浮动 tooltip 保留给逐点详情);
// - **高度可拖拽**:底部拖柄上下拉伸(180~720px,localStorage 记忆,双击复位);
// - **时间轴交互**:鼠标左右拖拽平移日期段 + 滚轮/捏合缩放(TradingView 式),
//   附区间快捷按钮(1月/3月/6月/1年/全部);水下副图保持只读并与主图联动;
// - 数据来自 /api/backtest 与 /api/chat 响应(portfolio_series/benchmark_series/
//   trades);字段缺失/为空时对应元素不渲染(图表是非关键路径);
// - 时区陷阱:日期字符串 'yyyy-mm-dd' 直接传给 lightweight-charts(按 UTC 解析),
//   不做 new Date() 本地时区换算;颜色全部从 styles.css CSS 变量读取。
import { useEffect, useMemo, useRef, useState } from 'react';
import { createChart, ColorType, CrosshairMode, LineStyle } from 'lightweight-charts';
import { computeDrawdown, normalizeSeries, tradeMarkers, underwaterSeries } from '../chartUtils.js';

// 高度记忆(localStorage 键)+ 高度范围
const LS_HEIGHT = 'aquant_chart_height';
const HEIGHT_MIN = 180;
const HEIGHT_MAX = 720;
const HEIGHT_DEFAULT = 320;
const UNDERWATER_HEIGHT = 110; // 水下副图固定高

function clampHeight(h) {
  if (!Number.isFinite(h)) return HEIGHT_DEFAULT;
  return Math.min(HEIGHT_MAX, Math.max(HEIGHT_MIN, Math.round(h)));
}

function readStoredHeight() {
  try {
    return clampHeight(Number(localStorage.getItem(LS_HEIGHT)));
  } catch {
    return HEIGHT_DEFAULT;
  }
}

function storeHeight(h) {
  try {
    localStorage.setItem(LS_HEIGHT, String(h));
  } catch {
    /* 隐私模式/配额:静默失败(高度不记忆,不影响功能) */
  }
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

// 固定 legend 信息条的一次刷新(直接写 DOM,不走 React state,避免高频重渲染)
function renderLegendRow(el, snapshot) {
  if (!el) return;
  const seg = (label, value, cls) =>
    `<span class="nl-item${cls ? ` ${cls}` : ''}"><b>${label}</b>${value}</span>`;
  const parts = [`<span class="nl-date">${snapshot.date}</span>`];
  if (snapshot.strategy != null) parts.push(seg('策略', snapshot.strategy.toFixed(4), 'nl-strategy'));
  if (snapshot.benchmark != null) parts.push(seg(snapshot.bmLabel, snapshot.benchmark.toFixed(4), 'nl-bm'));
  if (snapshot.dd != null && snapshot.dd > 0.0005) {
    parts.push(seg('回撤', `-${(snapshot.dd * 100).toFixed(2)}%`, 'nl-dd'));
  }
  if (snapshot.strategy != null && snapshot.benchmark != null) {
    const diff = snapshot.strategy - snapshot.benchmark;
    const cls = diff > 0 ? 'tip-up' : diff < 0 ? 'tip-down' : '';
    parts.push(seg('相对', `${diff >= 0 ? '+' : ''}${(diff * 100).toFixed(2)}%`, cls));
  }
  el.innerHTML = parts.join('');
}

export default function NetValueChart({
  series,
  benchmarkSeries,
  benchmarkLabel = 'A股基准',
  trades,
  showUnderwater = true,
}) {
  const points = useMemo(() => normalizeSeries(series), [series]);
  const bmPoints = useMemo(() => normalizeSeries(benchmarkSeries), [benchmarkSeries]);
  const tradesMarkers = useMemo(() => tradeMarkers(trades), [trades]);
  const dd = useMemo(() => computeDrawdown(points), [points]);
  const underPoints = useMemo(
    () => (showUnderwater && points.length >= 2 ? underwaterSeries(points) : []),
    [points, showUnderwater],
  );

  const [bodyHeight, setBodyHeight] = useState(readStoredHeight);
  const bodyRef = useRef(null); // 主图容器
  const underRef = useRef(null); // 水下副图容器
  const tipRef = useRef(null); // 浮动 tooltip
  const legendRef = useRef(null); // 固定 legend 信息条
  const draggingRef = useRef(null); // 拖柄拖拽状态({startY, startH})
  const chartRef = useRef(null); // 主图实例(区间按钮用,交互平移/缩放后复位)

  // 区间快捷按钮:按交易日点数切窗(1月≈21 / 3月≈63 / 6月≈126 / 1年≈244 / 全部=fit)
  const applyRange = (n) => {
    const chart = chartRef.current;
    if (!chart) return;
    if (n == null) {
      chart.timeScale().fitContent();
      return;
    }
    const len = points.length || bmPoints.length;
    if (!len) return;
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, len - n), to: len + 1 });
  };

  // ---- 拖拽调高:pointer 事件 + window 监听(捕获阶段,拖出容器也生效) ----
  useEffect(() => {
    if (!points.length && !bmPoints.length) return undefined;
    const onMove = (e) => {
      const drag = draggingRef.current;
      if (!drag) return;
      const next = clampHeight(drag.startH + (drag.startY - e.clientY));
      setBodyHeight(next);
    };
    const onUp = () => {
      if (!draggingRef.current) return;
      draggingRef.current = null;
      document.body.classList.remove('chart-resizing');
      setBodyHeight((h) => {
        storeHeight(h);
        return h;
      });
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      document.body.classList.remove('chart-resizing');
    };
  }, [points.length, bmPoints.length]);

  useEffect(() => {
    const body = bodyRef.current;
    const underBody = underRef.current;
    const tip = tipRef.current;
    if ((!points.length && !bmPoints.length) || !body || !tip) return undefined;

    const css = getComputedStyle(document.documentElement);
    const readVar = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    const accent = readVar('--accent', '#3aa2ff');
    const bmColor = readVar('--warn', '#e6b450');
    const upColor = readVar('--up', '#ff5d5d');
    const textColor = readVar('--text-dim', '#93a4b5');
    const gridColor = hexToRgba(readVar('--border-soft', '#1a2530'), 0.55);
    const borderColor = readVar('--border', '#223041');

    // 主图交互:拖拽平移 + 滚轮/捏合缩放(仅时间轴;价格轴 autoScale 不缩放)
    const interactive = {
      handleScroll: { mouseDrag: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: {
        mouseWheel: true,
        pinch: true,
        axisPressedMouseMove: { time: true, price: false },
        axisDoubleClickReset: { time: true, price: false },
      },
    };
    const baseOptions = (mode) => ({
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
      ...(mode === 'interactive' ? interactive : { handleScroll: false, handleScale: false }),
    });

    const chart = createChart(body, {
      ...baseOptions('interactive'),
      width: body.clientWidth,
      height: body.clientHeight,
    });
    chartRef.current = chart;

    // 策略净值:Area 主视觉(蓝色)
    let area = null;
    if (points.length) {
      area = chart.addAreaSeries({
        lineColor: accent,
        topColor: hexToRgba(accent, 0.26),
        bottomColor: 'rgba(58, 162, 255, 0)',
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
      });
      area.setData(points);
      // 1.0 保本基线(虚线)
      area.createPriceLine({
        price: 1.0,
        color: textColor,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: '基准 1.0',
      });
    }

    // 最大回撤区间:峰值→谷底 高水线之下的红色半透明"阴影带"(线透明,只有填充)
    let ddBand = null;
    if (area && dd.window.length >= 2) {
      ddBand = chart.addAreaSeries({
        lineColor: 'rgba(0,0,0,0)',
        topColor: hexToRgba(upColor, 0.16),
        bottomColor: 'rgba(255, 93, 93, 0.01)',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      ddBand.setData(dd.window);
    }

    // A股基准:金色细实线
    let bmLine = null;
    if (bmPoints.length) {
      bmLine = chart.addLineSeries({
        color: bmColor,
        lineWidth: 1.6,
        lineStyle: LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: true,
      });
      bmLine.setData(bmPoints);
    }

    // 标记:交易 B/S + 回撤峰值/谷底(时间升序;合并同日)
    if (area) {
      const markers = [...tradesMarkers];
      if (dd.maxDd > 0.0005 && dd.peak && dd.trough) {
        markers.push({
          time: dd.peak.time,
          position: 'aboveBar',
          color: hexToRgba(upColor, 0.9),
          shape: 'circle',
          text: '峰',
        });
        markers.push({
          time: dd.trough.time,
          position: 'belowBar',
          color: hexToRgba(upColor, 0.9),
          shape: 'square',
          text: `谷 -${(dd.maxDd * 100).toFixed(1)}%`,
        });
      }
      if (markers.length) {
        markers.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
        area.setMarkers(markers);
      }
    }

    chart.timeScale().fitContent();

    // ---- 水下副图:回撤百分比(≤0),timeScale 双向联动 + 十字线同步 ----
    let underChart = null;
    let underArea = null;
    if (underBody && underPoints.length) {
      underChart = createChart(underBody, {
        ...baseOptions('readonly'),
        width: underBody.clientWidth,
        height: underBody.clientHeight,
        rightPriceScale: { ...baseOptions.rightPriceScale, visible: false },
        timeScale: { ...baseOptions.timeScale, visible: false }, // 时间轴由主图承担
        localization: {
          priceFormatter: (v) => `${(v * 100).toFixed(1)}%`,
        },
      });
      underArea = underChart.addAreaSeries({
        lineColor: hexToRgba(upColor, 0.85),
        topColor: 'rgba(255, 93, 93, 0.02)',
        bottomColor: hexToRgba(upColor, 0.28),
        lineWidth: 1.4,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      underArea.setData(underPoints);
      underChart.timeScale().fitContent();

      // timeScale 双向联动(防回流:仅当范围差异明显才回写)
      let syncing = false;
      const syncFromMain = (range) => {
        if (syncing || !range) return;
        syncing = true;
        try {
          underChart.timeScale().setVisibleLogicalRange(range);
        } finally {
          syncing = false;
        }
      };
      const syncFromUnder = (range) => {
        if (syncing || !range) return;
        syncing = true;
        try {
          chart.timeScale().setVisibleLogicalRange(range);
        } finally {
          syncing = false;
        }
      };
      chart.timeScale().subscribeVisibleLogicalRangeChange(syncFromMain);
      underChart.timeScale().subscribeVisibleLogicalRangeChange(syncFromUnder);
      // 十字线同步:主图移动 → 副图跟随(副图 crosshair 不需要反向,只读展示)
      chart.subscribeCrosshairMove((param) => {
        if (!param || !param.time || !underArea) {
          underChart.clearCrosshairPosition();
          return;
        }
        const row = underPoints.find((p) => p.time === formatDate(param.time));
        if (row) underChart.setCrosshairPosition(row.value, row.time, underArea);
        else underChart.clearCrosshairPosition();
      });
    }

    // ---- 固定 legend 信息条:默认最新值;crosshair 移动更新;移出恢复 ----
    const indexByTime = new Map(points.map((p, i) => [p.time, i]));
    const bmByTime = new Map(bmPoints.map((p) => [p.time, p.value]));
    const underByTime = new Map(underPoints.map((p) => [p.time, p.value]));
    const snapshotAt = (date) => {
      const idx = indexByTime.get(date);
      const strategy = idx != null ? points[idx].value : null;
      const bench = bmByTime.get(date);
      const u = underByTime.get(date);
      return {
        date,
        strategy,
        benchmark: bench,
        bmLabel: benchmarkLabel,
        dd: u != null ? -u : null,
      };
    };
    const showSnapshot = (date) => renderLegendRow(legendRef.current, snapshotAt(date));
    if (points.length) {
      showSnapshot(points[points.length - 1].time);
    } else if (bmPoints.length) {
      showSnapshot(bmPoints[bmPoints.length - 1].time);
    }

    // ---- 浮动 tooltip:逐点详情(策略涨跌着色 + 基准 + 相对强弱) ----
    const hideTip = () => {
      tip.style.opacity = '0';
    };
    const onCrosshair = (param) => {
      if (!param.point || param.point.x < 0 || param.point.y < 0 || !param.time) {
        hideTip();
        if (points.length) showSnapshot(points[points.length - 1].time);
        return;
      }
      const date = formatDate(param.time);
      showSnapshot(date);
      const bar = param.seriesData && area ? param.seriesData.get(area) : null;
      const bmBar = param.seriesData && bmLine ? param.seriesData.get(bmLine) : null;
      const value = bar ? Number(bar.value) : NaN;
      const bmValue = bmBar ? Number(bmBar.value) : bmByTime.get(date);
      if (!Number.isFinite(value) && !Number.isFinite(bmValue)) {
        hideTip();
        return;
      }
      const idx = indexByTime.get(date) ?? 0;
      const prev = idx > 0 ? points[idx - 1].value : 1.0;
      const cls = value > prev ? 'tip-up' : value < prev ? 'tip-down' : '';

      tip.replaceChildren();
      const d1 = document.createElement('div');
      d1.className = 'tip-date';
      d1.textContent = date;
      tip.append(d1);
      if (Number.isFinite(value)) {
        const d2 = document.createElement('div');
        d2.className = `tip-value ${cls}`.trim();
        d2.textContent = `策略 ${value.toFixed(4)}`;
        tip.append(d2);
      }
      if (Number.isFinite(bmValue)) {
        const d3 = document.createElement('div');
        d3.className = 'tip-benchmark';
        d3.textContent = `${benchmarkLabel} ${Number(bmValue).toFixed(4)}`;
        tip.append(d3);
        if (Number.isFinite(value)) {
          const d4 = document.createElement('div');
          const diff = value - Number(bmValue);
          d4.className = `tip-value ${diff > 0 ? 'tip-up' : diff < 0 ? 'tip-down' : ''}`.trim();
          d4.textContent = `${diff >= 0 ? '跑赢' : '跑输'} ${Math.abs(diff * 100).toFixed(2)}%`;
          tip.append(d4);
        }
      }
      const u = underByTime.get(date);
      if (u != null && -u > 0.0005) {
        const d5 = document.createElement('div');
        d5.className = 'tip-value tip-dd';
        d5.textContent = `回撤 -${(-u * 100).toFixed(2)}%`;
        tip.append(d5);
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

    // ---- 响应式:容器尺寸变化(含拖拽调高)时同步图表宽高 ----
    const ro = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) {
        chart.applyOptions({ width: Math.floor(width), height: Math.floor(height) });
      }
    });
    ro.observe(body);
    let roUnder = null;
    if (underChart && underBody) {
      roUnder = new ResizeObserver((entries) => {
        const entry = entries[0];
        if (!entry) return;
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          underChart.applyOptions({ width: Math.floor(width), height: Math.floor(height) });
        }
      });
      roUnder.observe(underBody);
    }

    return () => {
      ro.disconnect();
      if (roUnder) roUnder.disconnect();
      if (underChart) underChart.remove();
      chart.remove();
      chartRef.current = null;
    };
  }, [points, bmPoints, tradesMarkers, dd, underPoints, benchmarkLabel]);

  // 空数据 / null:不渲染任何东西
  if (!points.length && !bmPoints.length) return null;

  const hasUnder = underPoints.length > 0;

  return (
    <section className="netvalue-chart" aria-label="策略净值与基准收益曲线">
      <div className="netvalue-chart-head">
        <span className="netvalue-title">收益图</span>
        <div className="chart-range-btns" role="group" aria-label="显示区间">
          {[
            { label: '1月', n: 21 },
            { label: '3月', n: 63 },
            { label: '6月', n: 126 },
            { label: '1年', n: 244 },
            { label: '全部', n: null },
          ].map((it) => (
            <button key={it.label} type="button" className="chart-range-btn" onClick={() => applyRange(it.n)}>
              {it.label}
            </button>
          ))}
        </div>
        <span className="netvalue-legend">
          {points.length > 0 && (
            <>
              <i className="legend-swatch" aria-hidden="true" />
              策略净值
              {tradesMarkers.length > 0 && (
                <>
                  <i className="legend-trade-b" aria-hidden="true">B</i>
                  <i className="legend-trade-s" aria-hidden="true">S</i>
                </>
              )}
              {dd.maxDd > 0.0005 && (
                <span className="legend-dd">最大回撤 -{(dd.maxDd * 100).toFixed(2)}%</span>
              )}
            </>
          )}
          {bmPoints.length > 0 && (
            <>
              <i className="legend-line legend-line-bm" aria-hidden="true" />
              {benchmarkLabel}
            </>
          )}
        </span>
      </div>
      {/* 固定 legend 信息条(TradingView 式,十字线跟随更新) */}
      <div className="netvalue-legend-row mono" ref={legendRef} />
      <div className="netvalue-chart-body" ref={bodyRef} style={{ height: bodyHeight }}>
        <div className="netvalue-tooltip" ref={tipRef} />
      </div>
      {hasUnder && (
        <div className="netvalue-under" title="水下图:全程回撤百分比(与主图时间轴联动)">
          <div className="netvalue-under-label mono">回撤(水下)</div>
          <div className="netvalue-under-body" ref={underRef} />
        </div>
      )}
      {/* 底部拖柄:上下拉伸主图高度,双击复位 */}
      <div
        className="chart-resize-handle"
        role="separator"
        aria-label="拖拽调整图表高度,双击复位"
        title="拖拽调整高度(180~720),双击复位"
        onPointerDown={(e) => {
          e.preventDefault();
          draggingRef.current = { startY: e.clientY, startH: bodyHeight };
          document.body.classList.add('chart-resizing');
        }}
        onDoubleClick={() => {
          setBodyHeight(HEIGHT_DEFAULT);
          storeHeight(HEIGHT_DEFAULT);
        }}
      />
    </section>
  );
}
