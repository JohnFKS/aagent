// 多轮策略对话工作台:
// - 左栏:多轮对话消息 + 需求输入,每轮自动「Agent 改码 → 校验 → 回测 → 落版本」;
// - 中栏:当前策略全程可见可编辑(保存即新版本)+ 版本时间线(回退/设为对比基准/查看);
// - 右栏:最近一轮回测结果与策略变更 diff;
// 会话与版本存后端 ~/.aquant/sessions,刷新页面不丢;隐藏 Tab 时保持挂载。
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import CodeEditor from './CodeEditor.jsx';
import MetricsCards, { formatValue } from './MetricsCards.jsx';
import FindingsList from './FindingsList.jsx';
import NetValueChart from './NetValueChart.jsx';
import MonthlyReturnsTable from './MonthlyReturnsTable.jsx';
import RunLogPanel from './RunLogPanel.jsx';
import TradesTable from './TradesTable.jsx';
import VersionTimeline from './VersionTimeline.jsx';
import DiffView from './DiffView.jsx';

const DEFAULT_START = '2023-01-03';
const DEFAULT_END = '2024-06-28';

const EXAMPLES = [
  '生成一个双均线择时策略,仓位 90%',
  '把快线改成 7,慢线改成 30',
  '加一条 60 日均线趋势过滤,只在趋势向上时开仓',
];

const BUSY_LABEL = {
  create: '正在创建会话…',
  load: '正在加载会话…',
  turn: 'Agent 正在修改策略并回测(可能需要 1~3 分钟)…',
  edit: '正在保存手动修改…',
  backtest: '正在回测当前版本…',
  rollback: '正在回退版本…',
  diff: '正在计算版本差异…',
};

function metricsLine(metrics) {
  if (!metrics) return '';
  return `夏普 ${formatValue('num2', metrics.sharpe)} · 年化 ${formatValue('percent', metrics.annualized_returns)} · 回撤 ${formatValue('percent', metrics.max_drawdown)}`;
}

export default function ConversationPanel({ notify }) {
  const [sessions, setSessions] = useState([]);
  const [sid, setSid] = useState('');
  const [session, setSession] = useState(null);
  const [title, setTitle] = useState('');
  const [start, setStart] = useState(DEFAULT_START);
  const [end, setEnd] = useState(DEFAULT_END);
  const [text, setText] = useState('');
  const [editMessage, setEditMessage] = useState('');
  const [backtestAfterTurn, setBacktestAfterTurn] = useState(true);
  const [draft, setDraft] = useState('');
  const [dirty, setDirty] = useState(false);
  const [viewVid, setViewVid] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [lastResult, setLastResult] = useState(null);
  const [compareFrom, setCompareFrom] = useState('');
  const [compareTo, setCompareTo] = useState('');
  const [diff, setDiff] = useState(null);
  const [editingTid, setEditingTid] = useState('');
  const [turnDraft, setTurnDraft] = useState('');
  const [editingVid, setEditingVid] = useState('');
  const [versionDraft, setVersionDraft] = useState({ message: '', analysis: '' });
  const bottomRef = useRef(null);

  const versions = session?.versions || [];
  const currentVersion = versions.find((v) => v.id === session?.current_version);
  const viewVersion = viewVid ? versions.find((v) => v.id === viewVid) : null;
  const shownCode = viewVersion ? viewVersion.code || '' : draft;
  const versionOptions = versions.map((v) => v.id);

  const refreshSessions = useCallback(async () => {
    try {
      const data = await api.sessions();
      setSessions(data.sessions || []);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  // 会话切换后,未处于编辑脏状态时将编辑器同步到当前版本代码
  useEffect(() => {
    if (session && !dirty) {
      const current = session.versions?.find((v) => v.id === session.current_version);
      setDraft(current?.code || '');
    }
  }, [session, dirty]);

  useEffect(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollTop = bottomRef.current.scrollHeight;
    }
  }, [session?.turns?.length]);

  const applySession = useCallback((data) => {
    setSession(data);
    setDirty(false);
    setViewVid('');
    setError('');
    const agentTurn = [...(data.turns || [])].reverse().find((t) => t.role === 'agent');
    setLastResult(agentTurn?.result || null);
    setCompareTo(data.current_version || '');
    setCompareFrom(
      data.versions?.length >= 2 ? data.versions[data.versions.length - 2].id : '',
    );
    setDiff(null);
  }, []);

  const loadSession = useCallback(async (id) => {
    if (!id) {
      setSid('');
      setSession(null);
      setDraft('');
      setDirty(false);
      setLastResult(null);
      setDiff(null);
      return;
    }
    setBusy('load');
    try {
      const data = await api.sessionGet(id);
      setSid(id);
      setTitle(data.session.title || '');
      setStart(data.session.start || DEFAULT_START);
      setEnd(data.session.end || DEFAULT_END);
      applySession(data.session);
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [applySession, notify]);

  const createSession = useCallback(async () => {
    setBusy('create');
    setError('');
    try {
      const payload = { start, end, title: title || '新策略会话' };
      if (draft.trim()) payload.code = draft;
      const data = await api.sessionCreate(payload);
      await refreshSessions();
      await loadSession(data.session.id);
      notify('success', `会话已创建:${data.session.id}`);
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [start, end, title, draft, refreshSessions, loadSession, notify]);

  const deleteSession = useCallback(async () => {
    if (!sid) return;
    setBusy('load');
    try {
      await api.sessionDelete(sid);
      await refreshSessions();
      await loadSession('');
      notify('success', '会话已删除。');
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, refreshSessions, loadSession, notify]);

  const applyRange = useCallback(async () => {
    if (!sid) return;
    try {
      const data = await api.sessionUpdate(sid, { title, start, end });
      setSession(data.session);
      notify('success', '会话标题与回测区间已更新。');
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    }
  }, [sid, title, start, end, notify]);

  const fetchDiff = useCallback(async (targetSid, from, to) => {
    if (!targetSid || !from || !to) return;
    setBusy('diff');
    try {
      setDiff(await api.sessionDiff(targetSid, from, to));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  }, []);

  const sendTurn = useCallback(async () => {
    const requestText = text.trim();
    if (!requestText || busy) return;
    setBusy('turn');
    setError('');
    try {
      let targetSid = sid;
      if (!targetSid) {
        const payload = { start, end, title: title || '对话策略会话' };
        if (draft.trim()) payload.code = draft;
        const created = await api.sessionCreate(payload);
        targetSid = created.session.id;
        await refreshSessions();
      }
      const previousVersion = session?.current_version || '';
      const data = await api.sessionTurn(targetSid, {
        text: requestText,
        backtest: backtestAfterTurn,
      });
      setSid(targetSid);
      setSession(data.session);
      setDirty(false);
      setViewVid('');
      setLastResult(data);
      setText('');
      setEditMessage('');
      if (!previousVersion && data.version_id) {
        setCompareFrom('');
        setCompareTo(data.version_id);
      } else if (data.version_id && data.version_id !== previousVersion) {
        setCompareFrom(previousVersion);
        setCompareTo(data.version_id);
        await fetchDiff(targetSid, previousVersion, data.version_id);
      }
      notify(
        data.ok ? 'success' : 'warn',
        data.ok
          ? `第 ${data.session.turns.filter((t) => t.role === 'user').length} 轮完成,已生成版本 ${data.version_id || '—'}。`
          : `本轮未采纳修改:${data.error || '校验未通过'}`,
      );
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [text, busy, sid, start, end, title, draft, session, backtestAfterTurn, refreshSessions, fetchDiff, notify]);

  const saveDraft = useCallback(async () => {
    if (!sid || !dirty || viewVersion || busy) return;
    setBusy('edit');
    setError('');
    try {
      const data = await api.sessionEditCode(sid, {
        code: draft,
        message: editMessage || '手动编辑策略代码',
        backtest: false,
      });
      setSession(data.session);
      setDirty(false);
      setEditMessage('');
      notify('success', `手动修改已保存为版本 ${data.version.id}。`);
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, dirty, viewVersion, busy, draft, editMessage, notify]);

  const runBacktest = useCallback(async () => {
    if (!sid || busy) return;
    setBusy('backtest');
    setError('');
    try {
      const data = await api.sessionBacktest(sid);
      setSession(data.session);
      setLastResult(data);
      notify('success', `回测完成:${metricsLine(data.metrics)}`);
    if (data?.bundle_extension?.extended) {
      const ext = data.bundle_extension;
      notify(
        'info',
        `数据 bundle 已自动更新:${ext.prior.end} → ${ext.now.end}(拉取最新行情,旧数据已清理)。`,
      );
    }
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, busy, notify]);

  const doRollback = useCallback(async (versionId) => {
    if (!sid || busy) return;
    setBusy('rollback');
    setError('');
    try {
      const data = await api.sessionRollback(sid, { version_id: versionId });
      setSession(data.session);
      setDirty(false);
      setViewVid('');
      notify('success', `已回退到版本 ${versionId},历史版本仍全部保留。`);
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, busy, notify]);

  const doCompare = useCallback(async () => {
    await fetchDiff(sid, compareFrom, compareTo);
  }, [fetchDiff, sid, compareFrom, compareTo]);

  // ---- 永久删除会话与历史对话/版本 CRUD --------------------------------
  const purgeSession = useCallback(async () => {
    if (!sid || !window.confirm(
      `永久删除会话 ${sid}?\n所有策略版本与对话将被彻底抹除,不可恢复。`,
    )) return;
    setBusy('load');
    try {
      await api.sessionDelete(sid, true);
      await refreshSessions();
      await loadSession('');
      notify('success', '会话已永久删除,不可恢复。');
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, refreshSessions, loadSession, notify]);

  const addNote = useCallback(async () => {
    const note = text.trim();
    if (!sid || !note || busy) return;
    setBusy('edit');
    try {
      const data = await api.sessionTurnAdd(sid, { role: 'note', text: note });
      setSession(data.session);
      setText('');
      notify('success', '笔记已追加到对话历史。');
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, text, busy, notify]);

  const saveTurnEdit = useCallback(async (tid) => {
    if (!sid || !turnDraft.trim()) return;
    setBusy('edit');
    try {
      const data = await api.sessionTurnUpdate(sid, tid, { text: turnDraft });
      setSession(data.session);
      setEditingTid('');
      notify('success', `对话 ${tid} 已修改。`);
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, turnDraft, notify]);

  const removeTurn = useCallback(async (tid) => {
    if (!sid || !window.confirm(`永久删除对话消息 ${tid}?此操作不可恢复。`)) return;
    setBusy('edit');
    try {
      const data = await api.sessionTurnDelete(sid, tid);
      setSession(data.session);
      notify('success', `对话 ${tid} 已永久删除。`);
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, notify]);

  const saveVersionMeta = useCallback(async (vid) => {
    if (!sid) return;
    setBusy('edit');
    try {
      const data = await api.sessionVersionUpdate(sid, vid, versionDraft);
      setSession(data.session);
      setEditingVid('');
      notify('success', `版本 ${vid} 说明已更新。`);
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, versionDraft, notify]);

  const removeVersion = useCallback(async (vid) => {
    if (!sid || !window.confirm(`永久删除策略版本 ${vid}?\n若它是当前版本,将自动回退到父版本;此操作不可恢复。`)) return;
    setBusy('rollback');
    try {
      const data = await api.sessionVersionDelete(sid, vid);
      setSession(data.session);
      setDirty(false);
      setViewVid('');
      setDiff(null);
      notify('success', `版本 ${vid} 已永久删除。`);
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setBusy('');
    }
  }, [sid, notify]);

  return (
    <section className="conv-layout">
      {/* 左栏:多轮对话 */}
      <div className="conv-col conv-chat">
        <header className="conv-head">
          <h2>多轮策略对话</h2>
          <span className="conv-head-sub">每轮自动改码 → 校验 → 回测 → 落版本</span>
        </header>
        <div className="conv-session-bar">
          <select
            value={sid}
            onChange={(e) => loadSession(e.target.value)}
            aria-label="选择会话"
            disabled={!!busy}
          >
            <option value="">＋ 新建会话</option>
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title} ({s.id}) · {s.version_count} 版
              </option>
            ))}
          </select>
          <button type="button" className="btn btn-ghost" onClick={createSession} disabled={!!busy}>
            新建
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              if (window.confirm(`删除会话 ${sid}?策略与对话将移入归档(可恢复)。`)) deleteSession();
            }}
            disabled={!sid || !!busy}
          >
            删除
          </button>
          <button
            type="button"
            className="btn btn-danger"
            onClick={purgeSession}
            disabled={!sid || !!busy}
            title="不可恢复:彻底抹除策略版本与全部对话"
          >
            永久删除
          </button>
        </div>
        <div className="conv-meta-row">
          <input
            aria-label="会话标题"
            placeholder="会话标题"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <input aria-label="回测起始日" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          <input aria-label="回测结束日" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          <button type="button" className="btn btn-ghost" onClick={applyRange} disabled={!sid}>
            应用
          </button>
        </div>

        <div className="conv-messages" ref={bottomRef} aria-live="polite">
          {!session && (
            <div className="conv-empty">
              输入需求开始:首轮生成策略,之后每轮都基于当前策略修改(历史与版本永久保留)。
            </div>
          )}
          {session?.turns?.map((turn) => (
            <div key={turn.id} className={`conv-msg conv-msg-${turn.role}`}>
              <div className="conv-msg-head">
                <span className="conv-role">{turn.role === 'user' ? '你' : 'Agent'}</span>
                <span className="conv-time mono">{String(turn.created_at || '').slice(5, 16)}</span>
                {turn.version_id && (
                  <button
                    type="button"
                    className="conv-vlink mono"
                    onClick={() => setViewVid(turn.version_id)}
                    title="在编辑器中查看该版本代码"
                  >
                    {turn.version_id}
                  </button>
                )}
              </div>
              {editingTid === turn.id ? (
                <div className="conv-turn-edit">
                  <textarea
                    aria-label="编辑对话文本"
                    value={turnDraft}
                    onChange={(e) => setTurnDraft(e.target.value)}
                    disabled={!!busy}
                  />
                  <button type="button" className="btn btn-primary" onClick={() => saveTurnEdit(turn.id)} disabled={!!busy}>
                    保存
                  </button>
                  <button type="button" className="btn btn-ghost" onClick={() => setEditingTid('')} disabled={!!busy}>
                    取消
                  </button>
                </div>
              ) : (
                <div className="conv-msg-body">{turn.text}</div>
              )}
              {turn.result?.metrics && (
                <div className="conv-msg-metrics mono">{metricsLine(turn.result.metrics)}</div>
              )}
              {turn.result?.error && <div className="conv-msg-error">{turn.result.error}</div>}
              {editingTid !== turn.id && (
                <div className="conv-msg-actions">
                  <button
                    type="button"
                    className="conv-mini-btn"
                    onClick={() => { setEditingTid(turn.id); setTurnDraft(turn.text); }}
                    disabled={!!busy}
                  >
                    编辑
                  </button>
                  <button type="button" className="conv-mini-btn conv-mini-danger" onClick={() => removeTurn(turn.id)} disabled={!!busy}>
                    删除
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>

        {busy && <div className="conv-busy">{BUSY_LABEL[busy] || '处理中…'}</div>}
        {error && <div className="conv-error" role="alert">{error}</div>}

        <div className="conv-composer">
          <textarea
            aria-label="本轮修改要求"
            placeholder="例如:把均线改成 7/30,并加入 5% 止损"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                e.preventDefault();
                sendTurn();
              }
            }}
            disabled={!!busy}
          />
          <div className="conv-composer-actions">
            <label className="conv-check">
              <input
                type="checkbox"
                checked={backtestAfterTurn}
                onChange={(e) => setBacktestAfterTurn(e.target.checked)}
              />
              修改后立即回测
            </label>
            <div className="conv-composer-buttons">
              <button
                type="button"
                className="btn btn-ghost"
                onClick={addNote}
                disabled={!sid || !text.trim() || !!busy}
                title="把输入内容作为笔记追加到对话历史,不触发 Agent 改码"
              >
                + 笔记
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={sendTurn}
                disabled={!text.trim() || !!busy}
              >
                发送并修改策略
              </button>
            </div>
          </div>
          <div className="conv-examples">
            {EXAMPLES.map((ex) => (
              <button key={ex} type="button" className="conv-example" onClick={() => setText(ex)}>
                {ex}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* 中栏:可编辑策略 + 版本时间线 */}
      <div className="conv-col conv-editor">
        <header className="conv-head">
          <h2>策略代码</h2>
          <span className="conv-badge mono">
            {viewVersion ? `${viewVersion.id} (历史)` : `当前 ${session?.current_version || '—'}`}
            {dirty && !viewVersion ? ' · 未保存' : ''}
          </span>
        </header>
        {viewVersion && (
          <div className="conv-viewbar">
            正在查看历史版本 {viewVersion.id}
            <button type="button" className="btn btn-ghost" onClick={() => setViewVid('')}>
              返回当前版本
            </button>
          </div>
        )}
        <div className="conv-editor-wrap">
          <CodeEditor
            value={shownCode}
            onChange={(value) => {
              setDraft(value);
              setDirty(true);
            }}
            disabled={!!viewVersion || busy === 'turn'}
            ariaLabel="多轮对话策略编辑器"
            placeholder="会话首轮由 Agent 生成策略,或先手动粘贴代码再新建会话。"
            autoHeight={false}
          />
        </div>
        <div className="conv-edit-actions">
          <input
            aria-label="修改说明"
            placeholder="修改说明(可选)"
            value={editMessage}
            onChange={(e) => setEditMessage(e.target.value)}
            disabled={!sid || !!viewVersion}
          />
          <button type="button" className="btn btn-primary" onClick={saveDraft} disabled={!sid || !dirty || !!viewVersion || !!busy}>
            保存为新版本
          </button>
          <button type="button" className="btn btn-ghost" onClick={runBacktest} disabled={!sid || !!busy}>
            回测当前版本
          </button>
        </div>
        <div className="conv-versions">
          <h3>版本时间线 <span className="conv-muted">({versions.length} 版,历史永不删除)</span></h3>
          <VersionTimeline
            versions={versions}
            currentId={session?.current_version}
            compareFrom={compareFrom}
            onCompare={(vid) => {
              setCompareFrom(vid);
              if (!compareTo) setCompareTo(session?.current_version || '');
            }}
            onRollback={doRollback}
            onView={(vid) => setViewVid(vid)}
            onEditMeta={saveVersionMeta}
            onDelete={removeVersion}
          />
        </div>
      </div>

      {/* 右栏:回测结果 + 变更对比 */}
      <div className="conv-col conv-result">
        <header className="conv-head">
          <h2>回测结果与变更对比</h2>
        </header>
        {lastResult?.steps && (
          <div className="conv-steps">
            {lastResult.steps.map((s, i) => (
              <span key={`${s.step}-${i}`} className={`conv-step ${s.ok || s.passed ? 'step-ok' : ''} ${s.rejected ? 'step-fail' : ''}`}>
                {s.rejected || s.ok === false ? '✗' : s.skipped ? '○' : '✓'} {s.step}
              </span>
            ))}
          </div>
        )}
        {lastResult?.analysis && (
          <div className="conv-analysis">
            <strong>Agent 修改说明</strong>
            <p>{lastResult.analysis}</p>
          </div>
        )}
        {/* 收益图(策略净值 vs A股基准 + 回撤/交易标记):优先本轮结果,否则当前版本存档 */}
        <NetValueChart
          series={lastResult?.portfolio_series ?? currentVersion?.backtest?.portfolio_series}
          benchmarkSeries={lastResult?.benchmark_series ?? currentVersion?.backtest?.benchmark_series}
          benchmarkLabel={
            lastResult?.benchmark && lastResult.benchmark !== 'none'
              ? `A股基准(${lastResult.benchmark_label || lastResult.benchmark})`
              : currentVersion?.backtest?.benchmark && currentVersion.backtest.benchmark !== 'none'
                ? `A股基准(${currentVersion.backtest.benchmark_label || currentVersion.backtest.benchmark})`
                : ''
          }
          trades={lastResult?.trades ?? currentVersion?.backtest?.trades}
        />
        <MonthlyReturnsTable
          series={lastResult?.portfolio_series ?? currentVersion?.backtest?.portfolio_series}
        />
        <MetricsCards metrics={lastResult?.metrics || currentVersion?.metrics || null} />
        <TradesTable trades={lastResult?.trades ?? currentVersion?.backtest?.trades} />
        <RunLogPanel runLog={lastResult?.run_log ?? currentVersion?.backtest?.run_log} />
        {lastResult?.verdict?.findings && (
          <FindingsList findings={lastResult.verdict.findings} emptyText="本轮无校验发现。" />
        )}
        {lastResult?.report && (
          <details className="conv-report">
            <summary>完整报告</summary>
            <pre className="mono">{lastResult.report}</pre>
          </details>
        )}
        <div className="conv-diff-panel">
          <h3>策略变更对比</h3>
          <div className="conv-compare-bar">
            <select value={compareFrom} onChange={(e) => setCompareFrom(e.target.value)} aria-label="对比基准版本">
              <option value="">选择基准版本…</option>
              {versionOptions.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
            <span className="conv-arrow">→</span>
            <select value={compareTo} onChange={(e) => setCompareTo(e.target.value)} aria-label="对比目标版本">
              <option value="">选择目标版本…</option>
              {versionOptions.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
            <button type="button" className="btn btn-ghost" onClick={doCompare} disabled={!sid || !compareFrom || !compareTo || !!busy}>
              对比
            </button>
          </div>
          <DiffView diff={diff} loading={busy === 'diff'} />
        </div>
      </div>
    </section>
  );
}
