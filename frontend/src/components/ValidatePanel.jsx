// 右栏:校验台(ValidatePanel)
// - 策略代码粘贴区 + "仅校验"按钮 → POST /api/validate(passed 徽章 + findings 分级列表 + summary);
// - "校验并回测"按钮 → POST /api/backtest(200 指标 / 422 拦截 verdict,同样健壮展示);
// - "填入坏策略示例"一键填入含 .shift(-1) 未来函数的 RQAlpha 代码,便于演示拦截;
// - 支持接收左栏"送入校验台"的生成代码。
import { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import { SAMPLE_BAD_CODE, DEFAULT_START, DEFAULT_END } from '../samples.js';
import FindingsList from './FindingsList.jsx';
import MetricsCards from './MetricsCards.jsx';

export default function ValidatePanel({ notify, seed }) {
  const [code, setCode] = useState('');
  const [start, setStart] = useState(DEFAULT_START);
  const [end, setEnd] = useState(DEFAULT_END);
  const [withRange, setWithRange] = useState(true); // 是否把区间一并发给 /api/validate(完整校验含配置规则)
  const [loading, setLoading] = useState(false); // 仅校验请求中
  const [btLoading, setBtLoading] = useState(false); // 回测请求中
  const [vres, setVres] = useState(null); // /api/validate 结果
  const [bres, setBres] = useState(null); // /api/backtest 归一化结果 {kind:'ok'|'rejected', ...}
  const [error, setError] = useState('');
  const taRef = useRef(null);

  // 接收左栏"送入校验台":seed 变化时填入代码并聚焦
  useEffect(() => {
    if (seed && seed.n > 0 && seed.code) {
      setCode(seed.code);
      if (taRef.current) taRef.current.focus();
    }
  }, [seed?.n]); // eslint-disable-line react-hooks/exhaustive-deps

  // 仅校验(校验不通过也是 HTTP 200,passed=false)
  async function runValidate() {
    if (!code.trim()) {
      setError('请先粘贴需要校验的策略代码。');
      return;
    }
    setError('');
    setVres(null);
    setBres(null);
    setLoading(true);
    try {
      const payload = { code };
      if (withRange && start && end) {
        payload.start = start;
        payload.end = end;
      }
      const data = await api.validate(payload);
      setVres(data || {});
      notify(
        data?.passed ? 'success' : 'warn',
        data?.passed ? 'Layer 1 校验通过。' : 'Layer 1 校验未通过:存在错误级发现。',
      );
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setLoading(false);
    }
  }

  // 校验并回测:200 → 指标;422 → 运行时校验拦截(verdict 在错误体里)
  async function runBacktest() {
    if (!code.trim()) {
      setError('请先粘贴需要回测的策略代码。');
      return;
    }
    if (!start || !end) {
      setError('回测需要起始与结束日期。');
      return;
    }
    if (start > end) {
      setError('起始日期不能晚于结束日期。');
      return;
    }
    setError('');
    setVres(null);
    setBres(null);
    setBtLoading(true);
    try {
      const data = await api.backtest({ code, start, end });
      setBres({ kind: 'ok', data: data || {} });
      notify('success', `回测完成${data?.trades_count != null ? `,成交 ${data.trades_count} 笔` : ''}。`);
    } catch (e) {
      if (e.data && e.data.verdict) {
        // 422:回测前运行时校验拦截,错误体带 verdict
        setBres({ kind: 'rejected', detail: e.message, verdict: e.data.verdict });
        notify('warn', '回测被运行时校验拦截。');
      } else {
        setError(e.message);
        notify('error', e.message);
      }
    } finally {
      setBtLoading(false);
    }
  }

  const verdictOf = (obj) => obj?.verdict || null;

  return (
    <section className="panel validate-panel">
      <div className="panel-head">
        <h2>校验台 · Layer 1</h2>
        <div className="panel-actions">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => setCode(SAMPLE_BAD_CODE)}
            disabled={loading || btLoading}
            title="填入含 .shift(-1) 未来函数的坏策略,演示校验拦截"
          >
            填入坏策略示例
          </button>
        </div>
      </div>

      {/* 代码粘贴区 */}
      <textarea
        ref={taRef}
        className="code-input mono"
        placeholder="在此粘贴 RQAlpha 策略代码(含 init / handle_bar)…"
        rows={12}
        spellCheck={false}
        value={code}
        onChange={(e) => setCode(e.target.value)}
      />

      {/* 区间选项 + 操作按钮 */}
      <div className="form-row form-row-wrap">
        <label className="field field-date">
          <span>起始</span>
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="field field-date">
          <span>结束</span>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={withRange}
            onChange={(e) => setWithRange(e.target.checked)}
          />
          附带区间做完整校验(含配置规则)
        </label>
      </div>
      <div className="form-row">
        <button
          type="button"
          className="btn btn-primary"
          onClick={runValidate}
          disabled={loading || btLoading}
        >
          {loading ? (
            <>
              <i className="spinner" aria-hidden="true" />
              校验中…
            </>
          ) : (
            '仅校验'
          )}
        </button>
        <button
          type="button"
          className="btn btn-outline"
          onClick={runBacktest}
          disabled={loading || btLoading}
          title="对粘贴的代码直接执行真实回测(/api/backtest)"
        >
          {btLoading ? (
            <>
              <i className="spinner" aria-hidden="true" />
              回测中…
            </>
          ) : (
            '校验并回测'
          )}
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => {
            setCode('');
            setVres(null);
            setBres(null);
            setError('');
          }}
          disabled={loading || btLoading}
        >
          清空
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

      {/* /api/validate 结果:passed 徽章 + summary + findings */}
      {vres && (
        <section className="validate-result">
          <div className="verdict-head">
            <span className={`badge ${vres.passed ? 'badge-pass' : 'badge-fail'}`}>
              {vres.passed ? '✓ 校验通过' : '✗ 校验未通过'}
            </span>
            <span className="verdict-counts mono">
              错误 {vres.error_count ?? 0} · 警告 {vres.warning_count ?? 0}
            </span>
          </div>
          {vres.summary && <p className="summary-text">{vres.summary}</p>}
          <FindingsList findings={vres.findings} emptyText="无发现,校验全部通过。" />
        </section>
      )}

      {/* /api/backtest 结果(200) */}
      {bres?.kind === 'ok' && (
        <section className="backtest-result">
          <div className="verdict-head">
            <span className={`badge ${bres.data.passed ? 'badge-pass' : 'badge-fail'}`}>
              {bres.data.passed ? '✓ 回测完成' : '✗ 回测异常'}
            </span>
            <span className="verdict-counts mono">
              成交笔数 {bres.data.trades_count ?? '—'}
            </span>
          </div>
          <MetricsCards metrics={bres.data.metrics} />
          {verdictOf(bres.data) && (
            <>
              <div className="verdict-counts mono sub-head">校验结论</div>
              <FindingsList findings={bres.data.verdict.findings} emptyText="无发现。" />
            </>
          )}
        </section>
      )}

      {/* /api/backtest 被拦截(422):拦截原因 + verdict findings */}
      {bres?.kind === 'rejected' && (
        <section className="backtest-result">
          <div className="warn-bar" role="alert">
            <span className="bar-icon">⚠</span>
            <span>回测被运行时校验拦截:{bres.detail}</span>
          </div>
          <FindingsList findings={bres.verdict?.findings} emptyText="无发现。" />
        </section>
      )}
    </section>
  );
}
