// 多策略竞技场(ArenaPanel):docs/design.md §1.4"多 Agent 养蛊式大逃杀"的 UI 落地。
// - 候选编辑器:1~6 行动态列表(添加/删除行),每行 = 候选名 input + 等宽字体代码 textarea;
//   "载入示例"一键填入 2 个候选(合规双均线策略 + 含 .shift(-1) 未来函数的坏策略);
// - 日期区间(默认 2023-01-03 ~ 2024-06-28)+ "开赛"按钮:
//   请求期间按钮 disabled + loading 文案"并行回测中…"(/api/arena 超时放宽到 600s);
// - 前端预检:候选名非空且唯一、代码非空、区间合法(后端 pydantic / 400 校验兜底);
// - 结果区:ArenaResult 子组件渲染排名表 + 每候选状态徽章与可展开详情。
import { useState } from 'react';
import { api, newProgressId } from '../api.js';
import { ARENA_SAMPLES, DEFAULT_START, DEFAULT_END } from '../samples.js';
import ProgressPanel from './ProgressPanel.jsx';
import ArenaResult from './ArenaResult.jsx';

const MAX_CANDIDATES = 6;

// 候选行自增 id(模块级计数器即可,仅用于 React key / 行定位)
let rowSeq = 1;
const newRow = () => ({ id: rowSeq++, name: '', code: '' });

export default function ArenaPanel({ notify }) {
  // 候选行 [{id, name, code}],初始 2 行空候选
  const [rows, setRows] = useState([newRow(), newRow()]);
  const [start, setStart] = useState(DEFAULT_START);
  const [end, setEnd] = useState(DEFAULT_END);
  const [loading, setLoading] = useState(false); // 开赛请求中
  const [progressId, setProgressId] = useState('');
  const [progressActive, setProgressActive] = useState(false);
  const [result, setResult] = useState(null); // /api/arena 响应
  const [error, setError] = useState('');

  // 一键载入示例:合规双均线 vs 未来函数坏策略(演示"入榜 + 校验拦截"两种结局)
  function loadSamples() {
    setRows(ARENA_SAMPLES.map((s) => ({ id: rowSeq++, name: s.name, code: s.code })));
    setResult(null);
    setError('');
  }

  function addRow() {
    if (rows.length >= MAX_CANDIDATES) return;
    setRows((rs) => [...rs, newRow()]);
  }

  function removeRow(id) {
    // 至少保留 1 行(后端要求 1~6 个候选)
    setRows((rs) => (rs.length <= 1 ? rs : rs.filter((r) => r.id !== id)));
  }

  function patchRow(id, patch) {
    setRows((rs) => rs.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  // 开赛:前端预检 → POST /api/arena → ArenaResult 渲染
  async function runArena() {
    const cands = rows.map((r) => ({ name: r.name.trim(), code: r.code }));
    if (cands.length < 1 || cands.length > MAX_CANDIDATES) {
      setError(`候选数量需在 1~${MAX_CANDIDATES} 个之间。`);
      return;
    }
    const noName = cands.find((c) => !c.name);
    if (noName) {
      setError('每个候选都需要填写候选名。');
      return;
    }
    const dup = cands.find((c, i) => cands.findIndex((o) => o.name === c.name) !== i);
    if (dup) {
      setError(`候选名重复:"${dup.name}",同场竞技的候选名必须唯一。`);
      return;
    }
    const noCode = cands.find((c) => !c.code.trim());
    if (noCode) {
      setError(`候选"${noCode.name}"缺少策略代码。`);
      return;
    }
    if (!start || !end) {
      setError('请选择回测起始与结束日期。');
      return;
    }
    if (start > end) {
      setError('起始日期不能晚于结束日期。');
      return;
    }
    setError('');
    setResult(null);
    setLoading(true);
    const pid = newProgressId();
    setProgressId(pid);
    setProgressActive(true);
    try {
      const data = await api.arena({ candidates: cands, start, end }, pid);
      setResult(data || {});
      const ranked = data?.ranked_count ?? 0;
      const total = data?.count ?? cands.length;
      if (ranked > 0) {
        notify('success', `竞技场结束:${ranked}/${total} 个候选入榜。`);
      } else {
        notify('warn', '竞技场结束:没有候选成功入榜,详见各候选状态。');
      }
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      {/* 候选编辑器 */}
      <section className="panel">
        <div className="panel-head">
          <h2>多策略竞技场 · 同场回测排名</h2>
          <div className="panel-actions">
            <button
              type="button"
              className="btn btn-ghost"
              onClick={loadSamples}
              disabled={loading}
              title="填入 2 个示例候选:合规双均线策略 + 含 .shift(-1) 未来函数的坏策略"
            >
              载入示例
            </button>
          </div>
        </div>
        <p className="arena-sub">
          一次提交 1~{MAX_CANDIDATES} 个候选策略:全部先过 Layer 1 校验门禁,通过者并行回测,
          按(年化收益, 夏普)降序排名。校验被拒 / 执行失败只淘汰自己,不中断全场竞技。
        </p>

        <div className="arena-candidates">
          {rows.map((r, i) => (
            <div key={r.id} className="arena-cand">
              <div className="arena-cand-head">
                <span className="arena-cand-index mono" title={`候选 ${i + 1}`}>
                  {i + 1}
                </span>
                <input
                  className="arena-name-input"
                  placeholder={`候选 ${i + 1} 名称(同场唯一)`}
                  value={r.name}
                  disabled={loading}
                  onChange={(e) => patchRow(r.id, { name: e.target.value })}
                />
                <button
                  type="button"
                  className="btn btn-ghost arena-cand-del"
                  onClick={() => removeRow(r.id)}
                  disabled={loading || rows.length <= 1}
                  title={rows.length <= 1 ? '至少保留 1 个候选' : '删除该候选'}
                >
                  删除
                </button>
              </div>
              <textarea
                className="code-input arena-cand-code"
                placeholder="RQAlpha 策略代码(含 init / handle_bar)…"
                rows={7}
                spellCheck={false}
                value={r.code}
                disabled={loading}
                onChange={(e) => patchRow(r.id, { code: e.target.value })}
              />
            </div>
          ))}
        </div>

        {/* 工具行:添加候选 + 日期区间 + 开赛 */}
        <div className="form-row form-row-wrap arena-toolbar">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={addRow}
            disabled={loading || rows.length >= MAX_CANDIDATES}
            title={rows.length >= MAX_CANDIDATES ? `最多 ${MAX_CANDIDATES} 个候选` : '添加一个空候选行'}
          >
            + 添加候选({rows.length}/{MAX_CANDIDATES})
          </button>
          <label className="field field-date">
            <span>起始</span>
            <input type="date" value={start} disabled={loading} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label className="field field-date">
            <span>结束</span>
            <input type="date" value={end} disabled={loading} onChange={(e) => setEnd(e.target.value)} />
          </label>
          <button
            type="button"
            className="btn btn-primary arena-run"
            onClick={runArena}
            disabled={loading}
            title="全部候选过校验门禁后并行回测并排名(候选数 × 回测耗时)"
          >
            {loading ? (
              <>
                <i className="spinner" aria-hidden="true" />
                并行回测中…
              </>
            ) : (
              '开赛'
            )}
          </button>
        </div>

        {/* 网络 / HTTP 错误条(同时已 toast) */}
        {error && (
          <div className="error-bar" role="alert">
            <span className="bar-icon">✗</span>
            <span>{error}</span>
            <button type="button" className="bar-close" onClick={() => setError('')} aria-label="关闭">
              ✕
            </button>
          </div>
        )}
      </section>

      {/* 结果区:空态 / 回测中 / 结果 */}
      <section className="panel">
        {loading && !result && (
          <div className="arena-loading">
            <i className="spinner" aria-hidden="true" />
            <span>
              并行回测中…候选需先过 Layer 1 校验,再并行回测(候选数 × 回测耗时,约 10~60 秒)。
            </span>
          </div>
        )}
        {progressId && (loading || progressActive) && (
          <ProgressPanel progressId={progressId} active={progressActive} compact />
        )}
        {!loading && !result && (
          <div className="arena-empty">
            <div className="arena-empty-main">添加 2~6 个策略候选,同时回测排名</div>
            <div className="arena-empty-sub">
              点上方"载入示例"可一键填入合规策略与未来函数坏策略,体验排名 + 校验拦截。
            </div>
          </div>
        )}
        {result && <ArenaResult result={result} />}
      </section>
    </>
  );
}
