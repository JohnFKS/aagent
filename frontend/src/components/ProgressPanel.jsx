// 回测实时进度与日志面板:传入 progressId 后自主轮询 /api/progress/{id}?since=N,
// 增量拉取阶段/百分比/当前交易日与实时日志,用于所有回测入口(工坊/后台/对话/竞技场)。
// - since 用已收到的最大 event seq,避免重复渲染;
// - 任务完成后停止轮询,但仍保留已收集的事件供回看;
// - 日志区分 level(ERROR/WARNING)高亮,自动滚动到底部。
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api.js';

const POLL_MS = 600;

const STAGE_LABELS = {
  pending: '等待中', validating: '校验策略', validated: '校验通过',
  starting: '启动引擎', initializing: '初始化环境', running: '逐日回测中',
  finalizing: '汇总结果', done: '已完成',
};
const STATUS_LABELS = {
  running: '进行中', completed: '已完成', failed: '失败', cancelled: '已取消',
};

function levelClass(level) {
  const l = String(level || '').toUpperCase();
  if (l.includes('ERROR')) return 'pg-error';
  if (l.includes('WARN')) return 'pg-warn';
  if (l.includes('TRADE') || l.includes('成交')) return 'pg-trade';
  return '';
}

export default function ProgressPanel({ progressId, active = true, compact = false }) {
  const [snap, setSnap] = useState(null);
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState('');
  const sinceRef = useRef(0);
  const logEndRef = useRef(null);
  const doneRef = useRef(false);
  const lastIdRef = useRef('');

  const poll = useCallback(async (force = false) => {
    if (!progressId || (doneRef.current && !force)) return;
    try {
      const data = await api.progress(progressId, sinceRef.current);
      setSnap(data);
      const fresh = (data.events || []).filter((e) => e.type === 'log' || e.type === 'error');
      if (fresh.length) {
        setLogs((prev) => [...prev, ...fresh].slice(-500));
        sinceRef.current = Math.max(sinceRef.current, ...fresh.map((e) => e.seq));
      }
      // 也推进 since 覆盖 stage/progress 事件,避免它们被重复拉取
      if (data.events && data.events.length) {
        sinceRef.current = Math.max(sinceRef.current, ...data.events.map((e) => e.seq));
      }
      setError('');
      if (data.status !== 'running' && data.status !== 'pending') doneRef.current = true;
    } catch (e) {
      // 404(进度未创建/已过期)等不反复弹错,静默等待下一轮或结束
      if (e.status !== 404) setError(e.message);
    }
  }, [progressId]);

  // 重置只在 id 真正变化时进行;active 由 true→false(任务结束)时保留已拉取的数据,
  // 并再拉一次最终快照(任务可能在最后一轮轮询与 active 变化之间完成)。
  useEffect(() => {
    if (!progressId) return undefined;
    if (lastIdRef.current !== progressId) {
      lastIdRef.current = progressId;
      sinceRef.current = 0;
      doneRef.current = false;
      setLogs([]);
      setSnap(null);
      setError('');
    }
    poll(!active); // 任务已结束(active=false)时强制拉取最终快照
    if (!active) return undefined;
    const timer = setInterval(() => poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [progressId, active, poll]);

  useEffect(() => {
    if (logEndRef.current) logEndRef.current.scrollTop = logEndRef.current.scrollHeight;
  }, [logs]);

  if (!progressId) return null;

  const pct = snap ? Math.min(100, Math.max(0, snap.percent || 0)) : 0;
  const stageText = snap
    ? (STAGE_LABELS[snap.stage] || snap.stage_label || snap.stage || '准备中')
    : '准备中';
  const statusText = snap ? (STATUS_LABELS[snap.status] || snap.status) : '准备中';

  return (
    <section className={`progress-panel ${compact ? 'progress-compact' : ''}`} aria-live="polite">
      <div className="progress-head">
        <span className="progress-title">回测进度</span>
        <span className="progress-status">{statusText}</span>
        <span className="progress-stage mono">{stageText}</span>
        <span className="progress-pct mono">{pct}%</span>
      </div>
      <div className="progress-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div className={`progress-fill ${snap?.status === 'failed' ? 'pf-failed' : ''} ${snap?.status === 'cancelled' ? 'pf-cancelled' : ''}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="progress-meta mono">
        {snap?.current_date ? `当前交易日 ${snap.current_date}` : '等待首个交易日'}
        {snap?.total_days ? ` · 第 ${snap.done_days}/${snap.total_days} 日` : (snap?.done_days ? ` · 已完成 ${snap.done_days} 日` : '')}
      </div>
      {error && <div className="progress-error">进度查询失败:{error}</div>}
      <div className="progress-logs" ref={logEndRef}>
        {logs.length === 0 && <div className="progress-logs-empty">等待实时日志输出…</div>}
        {logs.map((e) => (
          <div key={e.seq} className={`progress-log-line ${levelClass(e.level)}`}>
            <span className="progress-log-ts mono">{e.ts ? e.ts.slice(11, 23) : ''}</span>
            <span className="progress-log-msg">{e.message}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
