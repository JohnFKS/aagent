// 回测记录:后台回测任务的统一视图 —— 进行中 / 已完成 / 失败 / 已取消。
// - 每 3 秒轮询后端任务列表,进行中的任务可「终止」(POST /api/backtests/{id}/cancel);
// - 已完成的任务可「查看」完整结果(复用工坊的净值图/指标/成交/持仓/日志组件);
// - 提交入口在「策略工坊 → 后台回测」,任务存于后端内存,服务重启后清空。
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import MetricsCards from './MetricsCards.jsx';
import NetValueChart from './NetValueChart.jsx';
import MonthlyReturnsTable from './MonthlyReturnsTable.jsx';
import TradesTable from './TradesTable.jsx';
import DailyHoldingsPanel from './DailyHoldingsPanel.jsx';
import RunLogPanel from './RunLogPanel.jsx';

const STATUS = {
  running: { label: '进行中', cls: 'st-running' },
  completed: { label: '已完成', cls: 'st-completed' },
  failed: { label: '失败', cls: 'st-failed' },
  cancelled: { label: '已取消', cls: 'st-cancelled' },
};

const FILTERS = [
  { value: '', label: '全部' },
  { value: 'running', label: '进行中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
];

const POLL_MS = 3000;

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN', { hour12: false });
}

function elapsedLabel(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}

export default function BacktestJobsPanel({ notify }) {
  const [jobs, setJobs] = useState([]);
  const [filter, setFilter] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [cancellingId, setCancellingId] = useState('');

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const data = await api.backtestJobs(filter || undefined);
      setJobs(Array.isArray(data?.jobs) ? data.jobs : []);
      setError('');
    } catch (e) {
      if (!silent) setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    refresh();
    const timer = setInterval(() => refresh(true), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  async function openDetail(jid) {
    setDetailBusy(true);
    try {
      const data = await api.getBacktestJob(jid);
      setDetail(data);
    } catch (e) {
      notify('error', `读取回测详情失败:${e.message}`);
    } finally {
      setDetailBusy(false);
    }
  }

  async function onCancel(jid) {
    setCancellingId(jid);
    try {
      const data = await api.cancelBacktestJob(jid);
      if (data?.ok) {
        notify('success', '终止信号已发送,回测进程即将停止。');
        refresh(true);
      } else {
        notify('warn', '该任务已结束或不存在,无需终止。');
      }
    } catch (e) {
      notify('error', `终止回测失败:${e.message}`);
    } finally {
      setCancellingId('');
      if (detail?.id === jid) openDetail(jid);
    }
  }

  const runningCount = jobs.filter((j) => j.status === 'running').length;
  const result = detail?.status === 'completed' ? detail.result : null;

  return (
    <>
      <section className="panel">
        <div className="panel-head">
          <h2>回测记录</h2>
          <div className="panel-actions">
            <span className="job-running-count">进行中 {runningCount}</span>
            <select
              className="job-filter"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              aria-label="状态筛选"
            >
              {FILTERS.map((f) => (
                <option key={f.value} value={f.value}>{f.label}</option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => refresh()}
              disabled={loading}
              title="立即刷新任务列表"
            >
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>
        </div>

        <div className="studio-note">
          这里展示后台回测任务:提交入口在「策略工坊 → 后台回测」。进行中的任务可随时终止;
          完成 / 失败 / 已取消的记录可点「查看」回看完整结果。任务存于后端内存,服务重启后清空。
        </div>

        {error && (
          <div className="error-bar" role="alert">
            <span className="bar-icon">✗</span><span>{error}</span>
          </div>
        )}

        {jobs.length === 0 && !loading ? (
          <div className="arena-empty">
            <div className="arena-empty-main">暂无回测记录</div>
            <div className="arena-empty-sub">
              到「策略工坊」编辑策略后点「后台回测」,任务会实时出现在这里。
            </div>
          </div>
        ) : (
          <div className="job-table-wrap">
            <table className="job-table">
              <thead>
                <tr>
                  <th>状态</th><th>策略</th><th>回测区间</th>
                  <th>创建时间</th><th>耗时</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <td>
                      <span className={`job-status ${STATUS[j.status]?.cls || ''}`}>
                        {STATUS[j.status]?.label || j.status}
                      </span>
                    </td>
                    <td title={j.strategy_name}>{j.strategy_name || 'platform_strategy'}</td>
                    <td className="mono">{j.start} ~ {j.end}</td>
                    <td className="mono">{fmtTime(j.created_at)}</td>
                    <td className="mono">{elapsedLabel(j.elapsed_seconds)}</td>
                    <td>
                      <div className="job-actions">
                        {j.status === 'running' && (
                          <button
                            type="button"
                            className="btn btn-ghost job-cancel"
                            onClick={() => onCancel(j.id)}
                            disabled={cancellingId === j.id}
                            title="终止该回测(停止后台子进程)"
                          >
                            {cancellingId === j.id ? '终止中…' : '终止'}
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() => openDetail(j.id)}
                          title="查看任务详情与结果"
                        >
                          查看
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {detail && (
        <section className="panel job-detail-panel">
          <div className="panel-head">
            <h2>回测详情 · {detail.id}</h2>
            <div className="panel-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setDetail(null)}>关闭</button>
            </div>
          </div>
          <div className="job-detail-meta mono">
            {STATUS[detail.status]?.label || detail.status} · {detail.start} ~ {detail.end} ·
            耗时 {elapsedLabel(detail.elapsed_seconds)} · 创建于 {fmtTime(detail.created_at)}
          </div>

          {detailBusy && (
            <div className="kline-status">
              <i className="spinner" aria-hidden="true" />加载详情中…
            </div>
          )}

          {detail.error && (
            <div className="error-bar" role="alert">
              <span className="bar-icon">✗</span><span>{detail.error}</span>
            </div>
          )}

          {result && (
            <div className="backtest-result">
              <div className="verdict-head">
                <span className="badge badge-pass">✓ 回测完成</span>
                <span className="verdict-counts mono">成交笔数 {result.trades_count ?? '—'}</span>
              </div>
              <NetValueChart
                series={result.portfolio_series}
                benchmarkSeries={result.benchmark_series}
                benchmarkLabel={
                  result.benchmark && result.benchmark !== 'none'
                    ? `A股基准(${result.benchmark_label || result.benchmark})`
                    : ''
                }
                trades={result.trades}
              />
              <MonthlyReturnsTable series={result.portfolio_series} />
              <MetricsCards metrics={result.metrics} />
              <TradesTable trades={result.trades} />
              <DailyHoldingsPanel dailyHoldings={result.daily_holdings} />
              <RunLogPanel runLog={result.run_log} />
            </div>
          )}
        </section>
      )}
    </>
  );
}
