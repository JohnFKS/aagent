// 工坊 AI 自动优化面板(OptimizePanel):
// - 基于当前编辑器代码一键发起 POST /api/optimize:后端先跑基线回测,
//   再循环"LLM 分析(根据回测结果自动调整超参数/代码/逻辑)→ 改写 → 回测对照"N 轮,
//   返回基线/最优版本与每轮演进史;
// - 无需先执行回测(优化器自带基线);轮数可选 1/2/3(默认 2);
// - 结果:结论条(improved=true 绿 / false 琥珀)· 基线 vs 最优指标对照表
//   (复用 formatValue,正红负绿)· 每轮演进表(kept 绿↑采用 / 琥珀未超越 / 红 失败,
//   analysis 摘要截断可展开)· "采用最优版本"写入编辑器(improved=false 时禁用);
// - 错误:422 校验拦截(verdict findings)/ 400 bundle 缺失指引 / 502 LLM 故障 /
//   429·401·403 闸门,均为中文友好错误条;前端超时 900s,运行中显示已用时秒数。
import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { formatValue } from './MetricsCards.jsx';
import FindingsList from './FindingsList.jsx';

// 轮数可选项(后端约定 1~5,前端提供 1/2/3;默认 2)
const ROUND_OPTIONS = [1, 2, 3];
const DEFAULT_ROUNDS = 2;

// 基线 vs 最优对照表行(指标口径与 MetricsCards/VerifyResult 一致;signed 指标正红负绿)
const COMPARE_ROWS = [
  { key: 'total_returns', label: '总收益', kind: 'percent', signed: true },
  { key: 'annualized_returns', label: '年化收益', kind: 'percent', signed: true },
  { key: 'sharpe', label: '夏普比率', kind: 'num2' },
  { key: 'max_drawdown', label: '最大回撤', kind: 'percent' },
  { key: 'total_commission', label: '总佣金', kind: 'cny' },
];

// 演进表 analysis 摘要截断长度(超出折叠,可展开)
const ANALYSIS_TRUNCATE = 110;

// 对照单元格:signed 且数值可解析时正红负绿(A 股习惯,与指标卡一致)
function compareCell(row, metrics) {
  const v = metrics?.[row.key];
  let cls = '';
  if (row.signed && v !== undefined && v !== null && v !== '') {
    const n = Number(v);
    if (!Number.isNaN(n) && n !== 0) cls = n > 0 ? 'm-up' : 'm-down';
  }
  return <td className={`mono ${cls}`.trim()}>{formatValue(row.kind, v)}</td>;
}

// 每轮状态:kept=true → 采用;带 error/rejected → 失败;其余 → 跑完但未超越基线
function roundStatus(r) {
  if (r?.kept) return 'kept';
  if (r?.error || r?.rejected) return 'failed';
  return 'notbetter';
}

const ROUND_META = {
  kept: { cls: 'badge-pass', icon: '↑', label: '采用' },
  notbetter: { cls: 'badge-warn', icon: '=', label: '未超越' },
  failed: { cls: 'badge-fail', icon: '✗', label: '失败' },
};

// 演进表单行:轮次 / 结果徽章(+ 该轮指标摘要、失败原因)/ analysis 摘要(截断可展开)
function RoundRow({ r, idx }) {
  const [open, setOpen] = useState(false);
  const status = roundStatus(r);
  const meta = ROUND_META[status];
  const analysis = typeof r?.analysis === 'string' ? r.analysis.trim() : '';
  const long = analysis.length > ANALYSIS_TRUNCATE;
  const shown = open || !long ? analysis : `${analysis.slice(0, ANALYSIS_TRUNCATE)}…`;
  const errMsg = typeof r?.error === 'string' && r.error ? r.error : r?.rejected ? '该轮被运行时校验拦截。' : '';
  const s = r?.summary && typeof r.summary === 'object' ? r.summary : null;

  return (
    <tr className={`opt-round opt-round-${status}`}>
      <td className="mono">第 {r?.round ?? idx + 1} 轮</td>
      <td className="opt-round-status">
        <span className={`badge ${meta.cls}`}>
          <span aria-hidden="true">{meta.icon}</span>
          {meta.label}
        </span>
        {s && (
          <div className="opt-round-metrics mono">
            夏普 {formatValue('num2', s.sharpe)} · 年化 {formatValue('percent', s.annualized_returns)}
          </div>
        )}
        {errMsg && <div className="opt-round-err" title={errMsg}>{errMsg}</div>}
      </td>
      <td className="opt-round-analysis">
        {analysis ? (
          <>
            <span>{shown}</span>
            {long && (
              <button
                type="button"
                className="btn btn-ghost opt-expand-btn"
                onClick={() => setOpen((o) => !o)}
                aria-expanded={open}
              >
                {open ? '收起 ▴' : '展开 ▾'}
              </button>
            )}
          </>
        ) : (
          <span className="opt-no-analysis">—</span>
        )}
      </td>
    </tr>
  );
}

export default function OptimizePanel({
  code,
  start,
  end,
  cash,
  overrides,
  disabled, // 主分析执行中(running)时禁用优化
  onBusyChange, // 优化进行中通知父组件(父级借此禁用主分析,双向互斥)
  onAdopt, // 采用最优版本:回调写入编辑器(best.code)
  notify,
}) {
  const [rounds, setRounds] = useState(String(DEFAULT_ROUNDS));
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0); // 已用时(秒)
  const [result, setResult] = useState(null); // /api/optimize 200 响应
  const [fail, setFail] = useState(null); // {kind:'rejected'|'error', detail, status, verdict}
  const [srcCode, setSrcCode] = useState(''); // 发起优化时的代码快照(采用前对比编辑器是否已改动)

  // 已用时计时器:loading 期间每秒 +1
  useEffect(() => {
    if (!loading) return undefined;
    setElapsed(0);
    const timer = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [loading]);

  // 发起 AI 自动优化
  async function run() {
    if (!code.trim()) {
      setFail({ kind: 'error', detail: '请先在编辑器中编写或载入策略代码。', status: 0 });
      return;
    }
    if (!start || !end) {
      setFail({ kind: 'error', detail: '请选择回测起始与结束日期。', status: 0 });
      return;
    }
    if (start > end) {
      setFail({ kind: 'error', detail: '起始日期不能晚于结束日期。', status: 0 });
      return;
    }
    const payload = { code, start, end, rounds: Number(rounds) };
    const cashNum = Number(cash);
    if (Number.isFinite(cashNum) && cashNum > 0) payload.cash = cashNum;
    if (overrides) payload.overrides = overrides;

    setFail(null);
    setResult(null);
    setSrcCode(code);
    setLoading(true);
    onBusyChange?.(true);
    try {
      const data = await api.optimize(payload);
      setResult(data || {});
      if (data?.improved) {
        notify('success', `AI 优化完成:已找到更优版本(耗时 ${data?.elapsed_seconds ?? '—'} 秒)。`);
      } else {
        notify('warn', 'AI 优化完成:未找到超越基线的改进(最优=基线)。');
      }
    } catch (e) {
      if (e.data && e.data.verdict) {
        // 422:基线代码未过运行时校验,错误体带 verdict → 拦截条 + findings
        setFail({ kind: 'rejected', detail: e.message, verdict: e.data.verdict, status: e.status });
        notify('warn', 'AI 优化被运行时校验拦截(422):请先修正当前代码。');
      } else {
        setFail({ kind: 'error', detail: e.message, status: e.status });
        notify('error', `AI 优化失败:${e.message}`);
      }
    } finally {
      setLoading(false);
      onBusyChange?.(false);
    }
  }

  // 采用最优版本:写入编辑器(编辑器在优化期间被改动过则先确认)
  function adoptBest() {
    const bestCode = result?.best?.code;
    if (!bestCode) return;
    if (code.trim() && code !== srcCode && !window.confirm('当前编辑器内容与优化时的代码不同,采用最优版本将覆盖,是否继续?')) {
      return;
    }
    onAdopt?.(bestCode);
  }

  const running = loading;
  const roundsList = Array.isArray(result?.rounds) ? result.rounds : [];
  // 结论条"第 N 轮":最后一个 kept=true 的轮次(最优版本诞生的轮次)
  const keptIdxList = roundsList.map((r, i) => (r?.kept ? i : -1)).filter((i) => i >= 0);
  const bestRound = keptIdxList.length
    ? roundsList[keptIdxList[keptIdxList.length - 1]]?.round ?? keptIdxList[keptIdxList.length - 1] + 1
    : null;
  const improved = !!result?.improved;
  const timeoutish = fail?.status === 0 && /超时|无法连接/.test(fail?.detail || '');

  return (
    <section className="studio-optimize" aria-label="AI 自动优化">
      <div className="studio-settings-title">AI 自动优化(根据回测结果自动调整超参数 / 代码 / 逻辑)</div>
      <div className="form-hint opt-intro-hint">
        无需先执行回测 —— 优化器会先对当前编辑器代码跑基线,再逐轮 LLM 改写并与基线对照;
        每轮 ≈ 1 次 LLM 生成(30~120 秒)+ 2 次回测,可能需要数分钟。沿用上方回测设置(区间 / 初始资金 / 保守参数覆盖)。
      </div>

      {/* 轮数选择 + 发起按钮 */}
      <div className="form-row form-row-wrap opt-controls">
        <label className="field">
          <span>优化轮数</span>
          <select
            className="studio-select"
            value={rounds}
            disabled={running || disabled}
            onChange={(e) => setRounds(e.target.value)}
            aria-label="优化轮数"
            title="每轮 = LLM 分析改写 + 基线 vs 新版两次回测"
          >
            {ROUND_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n} 轮{n === DEFAULT_ROUNDS ? '(默认)' : ''}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="btn btn-primary"
          onClick={run}
          disabled={running || disabled || !code.trim()}
          title={!code.trim() ? '请先在编辑器中编写策略代码' : '对当前编辑器代码发起 AI 自动优化'}
        >
          {running ? (
            <>
              <i className="spinner" aria-hidden="true" />
              优化中(每轮 = LLM 生成 + 两次回测),可能数分钟…
            </>
          ) : (
            'AI 自动优化'
          )}
        </button>
        {running && (
          <span className="opt-elapsed mono" aria-live="polite">
            已用时 {elapsed}s(上限 900s)
          </span>
        )}
      </div>

      {/* 前端预检类错误(本地输入问题,非后端响应) */}
      {fail?.status === 0 && (
        <div className="error-bar" role="alert">
          <span className="bar-icon">✗</span>
          <span>{fail.detail}</span>
          <button type="button" className="bar-close" onClick={() => setFail(null)} aria-label="关闭">
            ✕
          </button>
        </div>
      )}

      {/* 422 校验拦截:拦截条 + findings */}
      {fail?.kind === 'rejected' && (
        <section className="opt-error">
          <div className="warn-bar" role="alert">
            <span className="bar-icon">⚠</span>
            <span>AI 优化被运行时校验拦截:{fail.detail}</span>
          </div>
          <FindingsList findings={fail.verdict?.findings} emptyText="无发现。" />
        </section>
      )}

      {/* 网络 / HTTP 错误条 */}
      {fail?.kind === 'error' && fail.status !== 0 && (
        <section className="opt-error">
          <div className="error-bar" role="alert">
            <span className="bar-icon">✗</span>
            <span>AI 优化失败:{fail.detail}</span>
            <button type="button" className="bar-close" onClick={() => setFail(null)} aria-label="关闭">
              ✕
            </button>
          </div>
          {fail.status === 400 && (
            <div className="form-hint studio-err-hint">
              优化依赖真实回测:请以 AQUANT_BUNDLE_PATH 指向已构建的数据 bundle 启动后端
              (构建方式见 README / python -m aquant.data.build_bundle --help)。
            </div>
          )}
          {fail.status === 502 && (
            <div className="form-hint studio-err-hint">
              LLM 生成失败(502):底层大模型服务故障或不可达(如当前后端运行在 Deterministic 兜底模式),
              请检查后端 LLM 配置后重试。
            </div>
          )}
          {(fail.status === 404 || fail.status === 405) && (
            <div className="form-hint studio-err-hint">
              后端未提供 POST /api/optimize(可能版本过旧或 AI 优化端点尚未部署):请升级后端服务后重试。
            </div>
          )}
          {(fail.status === 401 || fail.status === 403) && (
            <div className="form-hint studio-err-hint">
              当前环境未获 LLM 优化授权(闸门限制):请确认登录态 / API 配额设置。
            </div>
          )}
          {fail.status === 429 && (
            <div className="form-hint studio-err-hint">
              优化请求过于频繁(429):请稍后再试,或减少优化轮数。
            </div>
          )}
          {timeoutish && (
            <div className="form-hint studio-err-hint">
              前端等待上限 900 秒:超时后后端可能仍在计算,可直接重试或减少优化轮数。
            </div>
          )}
        </section>
      )}

      {/* 结果区:结论条 + 对照表 + 演进表 + 采用按钮 */}
      {result && (
        <section className="opt-result">
          {/* 结论条 */}
          <div className={`opt-conclusion ${improved ? 'opt-conclusion-good' : 'opt-conclusion-flat'}`} role="status">
            {improved
              ? `✓ 已找到更优版本${bestRound ? `(第 ${bestRound} 轮)` : ''}`
              : '未找到超越基线的改进(最优=基线)'}
            <span className="opt-conclusion-meta mono">
              总耗时 {formatValue('sec', result.elapsed_seconds)} · 执行 {roundsList.length} 轮
            </span>
          </div>

          {/* 基线 vs 最优指标对照表 */}
          <div className="sub-head">基线 vs 最优</div>
          <div className="arena-table-wrap">
            <table className="arena-table studio-table opt-compare-table">
              <thead>
                <tr>
                  <th>指标</th>
                  <th>基线(当前代码)</th>
                  <th>{improved ? '最优(AI 优化版)' : '最优(= 基线)'}</th>
                </tr>
              </thead>
              <tbody>
                {COMPARE_ROWS.map((row) => (
                  <tr key={row.key}>
                    <td className="studio-table-label">{row.label}</td>
                    {compareCell(row, result.baseline?.summary || {})}
                    {compareCell(row, result.best?.summary || {})}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* 演进史:每轮结果 */}
          <div className="sub-head">优化演进({roundsList.length} 轮)</div>
          {roundsList.length > 0 ? (
            <div className="arena-table-wrap">
              <table className="arena-table opt-rounds-table">
                <thead>
                  <tr>
                    <th>轮次</th>
                    <th>结果</th>
                    <th>分析摘要</th>
                  </tr>
                </thead>
                <tbody>
                  {roundsList.map((r, i) => (
                    <RoundRow key={r?.round ?? i} r={r} idx={i} />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="findings-empty">后端未返回逐轮演进记录。</div>
          )}

          {/* 采用最优版本(improved=false 时禁用) */}
          <div className="opt-adopt-row">
            <button
              type="button"
              className="btn btn-primary"
              onClick={adoptBest}
              disabled={!improved || !result.best?.code}
              title={improved ? '把最优版本写入编辑器(草稿自动同步)' : '未找到超越基线的改进,无需采用'}
            >
              采用最优版本
            </button>
            {!improved && <span className="opt-adopt-hint">最优 = 基线,编辑器代码保持不变。</span>}
          </div>
        </section>
      )}
    </section>
  );
}
