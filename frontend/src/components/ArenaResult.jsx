// 竞技场结果展示(ArenaResult):
// - 元信息行:区间 / 候选数 / 入榜数;
// - 排名表:排名/候选/年化收益/夏普/最大回撤/总收益/换手率,数字等宽(tabular-nums),
//   收益类指标正红负绿(A股习惯),榜首行高亮;
// - 可折叠"文本排名表"(后端 table 字段的 <pre> 渲染,便于复制);
// - 候选状态卡片:ranked 绿✓已入榜 / rejected 红✗校验拦截 / failed 黄⚠执行失败;
//   rejected / failed 默认展开(展示 verdict findings 摘要 / 失败原因),ranked 默认收起,
//   点开显示指标卡(MetricsCards)与校验结论(FindingsList)。
import { useState } from 'react';
import MetricsCards from './MetricsCards.jsx';
import FindingsList from './FindingsList.jsx';
import ArenaCurveChart from './ArenaCurveChart.jsx';

// 状态徽章元数据(与后端 details[name].status 对应)
const STATUS_META = {
  ranked: { cls: 'badge-pass', icon: '✓', label: '已入榜' },
  rejected: { cls: 'badge-fail', icon: '✗', label: '校验拦截' },
  failed: { cls: 'badge-warn', icon: '⚠', label: '执行失败' },
};

// 数值解析:缺失 / 非法 → null(渲染为 '—')
function num(v) {
  if (v === undefined || v === null || v === '') return null;
  const n = Number(v);
  return Number.isNaN(n) ? null : n;
}

function fmtPct(v) {
  const n = num(v);
  return n === null ? '—' : `${(n * 100).toFixed(2)}%`;
}

function fmt2(v) {
  const n = num(v);
  return n === null ? '—' : n.toFixed(2);
}

// 收益类正红负绿(A股习惯:涨=红、跌=绿)
function retCls(v) {
  const n = num(v);
  if (n === null || n === 0) return '';
  return n > 0 ? 'm-up' : 'm-down';
}

function RankingTable({ ranking }) {
  return (
    <div className="arena-table-wrap">
      <table className="arena-table">
        <thead>
          <tr>
            <th>排名</th>
            <th>候选</th>
            <th>年化收益</th>
            <th>夏普</th>
            <th>最大回撤</th>
            <th>总收益</th>
            <th>换手率</th>
          </tr>
        </thead>
        <tbody>
          {ranking.map((r) => (
            <tr key={r.name} className={r.rank === 1 ? 'arena-rank-1' : ''}>
              <td className="mono">{r.rank ?? '—'}</td>
              <td className="arena-table-name">{r.name}</td>
              <td className={`mono ${retCls(r.annualized_returns)}`}>{fmtPct(r.annualized_returns)}</td>
              <td className="mono">{fmt2(r.sharpe)}</td>
              <td className="mono">{fmtPct(r.max_drawdown)}</td>
              <td className={`mono ${retCls(r.total_returns)}`}>{fmtPct(r.total_returns)}</td>
              <td className="mono">{fmtPct(r.turnover)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CandidateCard({ name, status, info, open, onToggle }) {
  const meta = STATUS_META[status] || { cls: 'badge-warn', icon: '?', label: status || '未知' };
  const verdict = info?.verdict || null;
  const findings = Array.isArray(verdict?.findings) ? verdict.findings : [];
  return (
    <div className={`arena-detail arena-detail-${status}`}>
      <div className="arena-detail-head">
        <span className={`badge ${meta.cls}`}>
          <span aria-hidden="true">{meta.icon}</span>
          {meta.label}
        </span>
        <span className="arena-detail-name">{name}</span>
        <button
          type="button"
          className="btn btn-ghost arena-detail-toggle"
          onClick={onToggle}
          aria-expanded={open}
        >
          {open ? '收起 ▾' : '详情 ▸'}
        </button>
      </div>
      {open && (
        <div className="arena-detail-body">
          {status === 'ranked' && (
            <>
              <MetricsCards metrics={info?.metrics} />
              {findings.length > 0 && (
                <>
                  <div className="verdict-counts mono sub-head">校验结论</div>
                  <FindingsList findings={findings} emptyText="无发现。" />
                </>
              )}
            </>
          )}
          {status === 'rejected' && (
            <>
              {verdict?.summary ? <p className="summary-text">{verdict.summary}</p> : null}
              <FindingsList findings={findings} emptyText="无发现明细。" />
            </>
          )}
          {status === 'failed' && (
            <div className="warn-bar" role="alert">
              <span className="bar-icon">⚠</span>
              <span>{info?.error || '执行失败,原因未知。'}</span>
            </div>
          )}
          {!['ranked', 'rejected', 'failed'].includes(status) && (
            <div className="warn-bar" role="alert">
              <span className="bar-icon">?</span>
              <span>未知状态 "{status}"。</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ArenaResult({ result }) {
  // 文本排名表折叠开关
  const [showRaw, setShowRaw] = useState(false);
  // 候选卡片展开状态 {name: bool};未设置时 rejected/failed 默认展开,ranked 默认收起
  const [expandMap, setExpandMap] = useState({});

  const ranking = Array.isArray(result.ranking) ? result.ranking : [];
  const details =
    result.details && typeof result.details === 'object' ? result.details : {};

  // 卡片顺序:入榜(按名次)→ 校验拦截 → 执行失败 → 其他未知状态
  const rankedNames = new Set(ranking.map((r) => r.name));
  const rest = Object.entries(details).filter(([name]) => !rankedNames.has(name));
  const cards = [
    ...ranking.map((r) => ({ name: r.name, status: 'ranked', info: details[r.name] || {} })),
    ...rest
      .filter(([, v]) => v?.status === 'rejected')
      .map(([name, v]) => ({ name, status: 'rejected', info: v })),
    ...rest
      .filter(([, v]) => v?.status === 'failed')
      .map(([name, v]) => ({ name, status: 'failed', info: v })),
    ...rest
      .filter(([, v]) => v && v.status !== 'rejected' && v.status !== 'failed')
      .map(([name, v]) => ({ name, status: v.status, info: v })),
  ];

  const isOpen = (name, status) => {
    const v = expandMap[name];
    return v === undefined ? status !== 'ranked' : v;
  };

  const toggle = (name, status) => {
    setExpandMap((m) => ({ ...m, [name]: !isOpen(name, status) }));
  };

  return (
    <>
      <div className="panel-head">
        <h2>竞技结果</h2>
        <div className="panel-actions">
          {result.table ? (
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setShowRaw((s) => !s)}
              title="后端渲染的文本排名表,便于复制"
            >
              {showRaw ? '隐藏文本表' : '文本排名表'}
            </button>
          ) : null}
        </div>
      </div>

      {/* 元信息:区间 / 候选数 / 入榜数 */}
      <div className="verdict-head">
        <span className="verdict-counts mono">
          区间 {result.start || '—'} ~ {result.end || '—'} · 候选 {result.count ?? cards.length} · 入榜 {result.ranked_count ?? ranking.length}
        </span>
      </div>

      {/* 排名表 / 全军覆没提示 */}
      {ranking.length > 0 ? (
        <RankingTable ranking={ranking} />
      ) : (
        <div className="warn-bar" role="alert">
          <span className="bar-icon">⚠</span>
          <span>没有候选成功入榜:全部被校验拦截或执行失败,详见下方各候选状态。</span>
        </div>
      )}

      {/* 净值曲线叠加:各候选同图对照 + 基准虚线(图例点选隐藏/显示) */}
      <ArenaCurveChart
        details={details}
        benchmarkSeries={result.benchmark_series}
        benchmarkLabel={result.benchmark ? `A股基准(${result.benchmark_label || result.benchmark})` : ''}
      />

      {/* 文本排名表(可折叠) */}
      {showRaw && result.table ? (
        <pre className="arena-raw-pre mono">{result.table}</pre>
      ) : null}

      {/* 候选状态卡片 */}
      <div className="verdict-counts mono sub-head">候选状态</div>
      <div className="arena-details">
        {cards.map((c) => (
          <CandidateCard
            key={c.name}
            name={c.name}
            status={c.status}
            info={c.info}
            open={isOpen(c.name, c.status)}
            onToggle={() => toggle(c.name, c.status)}
          />
        ))}
      </div>
    </>
  );
}
