// 左栏:对话式策略编辑(ChatPanel)
// - 需求输入 + 起止日期 → POST /api/chat(编辑→校验→回测→评审→报告 一条管线);
// - 请求期间按钮 loading,StepProgress 按 steps 逐个点亮;
// - 结果区:指标卡片 / 净值曲线 / 可折叠"K 线(标的行情)" / verdict findings /
//   可折叠生成代码 / "查看完整报告"弹层(轻量 Markdown 渲染)。
import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { SAMPLE_REQUIREMENT, DEFAULT_START, DEFAULT_END } from '../samples.js';
import StepProgress from './StepProgress.jsx';
import MetricsCards from './MetricsCards.jsx';
import NetValueChart from './NetValueChart.jsx';
import KlineChart from './KlineChart.jsx';
import FindingsList from './FindingsList.jsx';
import CodeBlock from './CodeBlock.jsx';
import Markdown from '../markdown.jsx';

// A 股 order_book_id 形态:6 位数字 + 交易所后缀(用于从生成代码提取 / 校验输入)
const OID_EXTRACT_RE = /\b\d{6}\.(?:XSHE|XSHG|BJ)\b/;
const OID_STRICT_RE = /^\d{6}\.(XSHE|XSHG|BJ)$/;

// 从生成代码正则提取第一个 order_book_id(如 context.s1 = "000001.XSHE");无则 ''
function extractOid(code) {
  const m = OID_EXTRACT_RE.exec(String(code || '').toUpperCase());
  return m ? m[0] : '';
}

export default function ChatPanel({ notify, onSeedValidate }) {
  const [text, setText] = useState('');
  const [start, setStart] = useState(DEFAULT_START);
  const [end, setEnd] = useState(DEFAULT_END);
  const [loading, setLoading] = useState(false);
  // /api/chat 的返回:被拒/生成失败时也是 200,rejected/error 字段判断语义
  const [result, setResult] = useState(null);
  const [error, setError] = useState(''); // 网络/HTTP 层错误条
  const [showReport, setShowReport] = useState(false); // 报告弹层开关
  const [copied, setCopied] = useState(false); // "复制代码"轻提示

  // K 线折叠区状态(docs/design.md §6"K 线图"):默认折叠;
  // 标的优先从生成代码推断,推断不出时由用户在输入框填写
  const [klineOpen, setKlineOpen] = useState(false);
  const [klineOid, setKlineOid] = useState(''); // 已确认要查询的标的(order_book_id)
  const [klineDraft, setKlineDraft] = useState(''); // 标的输入框草稿
  const [klineCandles, setKlineCandles] = useState(null); // /api/kline 的 candles
  const [klineLoading, setKlineLoading] = useState(false);
  const [klineError, setKlineError] = useState('');

  // Esc 关闭报告弹层
  useEffect(() => {
    if (!showReport) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') setShowReport(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [showReport]);

  // 发起 生成并回测
  async function runChat() {
    if (!text.trim()) {
      setError('请先输入策略需求描述,可直接点击"填入需求示例"。');
      return;
    }
    if (!start || !end) {
      setError('请选择回测起始与结束日期(YYYY-MM-DD)。');
      return;
    }
    if (start > end) {
      setError('起始日期不能晚于结束日期。');
      return;
    }
    setError('');
    setResult(null);
    setShowReport(false);
    setLoading(true);
    try {
      const data = await api.chat({ text: text.trim(), start, end });
      setResult(data || {});
      // 业务语义:ok=false 表示管线被拦截或生成失败(仍是 HTTP 200)
      if (data && data.ok === false) {
        notify('warn', data.error || '管线未完整执行:被校验拦截或生成失败。');
      } else {
        notify('success', '生成与回测管线执行完成。');
      }
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setLoading(false);
    }
  }

  // 回测被拒语义:顶层 rejected=true,或 backtest 步自带 rejected
  const rejected = !!(
    result &&
    (result.rejected === true ||
      (Array.isArray(result.steps) &&
        result.steps.some((s) => s && s.step === 'backtest' && s.rejected === true)))
  );

  // 复制生成代码到剪贴板(复制后轻提示 2 秒)
  async function copyCode() {
    if (!result?.code) return;
    try {
      await navigator.clipboard.writeText(result.code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      notify('error', '复制失败:浏览器剪贴板不可用,请手动选择代码复制。');
    }
  }

  const verdict = result?.verdict;

  // 新结果到达 / 结果清空:重置 K 线折叠区,并预填从生成代码推断的标的
  useEffect(() => {
    setKlineOpen(false);
    setKlineOid(extractOid(result?.code || ''));
    setKlineDraft('');
    setKlineCandles(null);
    setKlineLoading(false);
    setKlineError('');
  }, [result]);

  // 展开且标的有效时拉取 K 线(聊天用的 start/end 口径);依赖变化自动重取
  useEffect(() => {
    if (!klineOpen || !OID_STRICT_RE.test(klineOid)) return undefined;
    let cancelled = false;
    setKlineLoading(true);
    setKlineError('');
    api
      .kline({ oid: klineOid, start, end })
      .then((data) => {
        if (cancelled) return;
        setKlineCandles(Array.isArray(data?.candles) ? data.candles : []);
      })
      .catch((e) => {
        if (cancelled) return;
        setKlineCandles(null);
        setKlineDraft(klineOid); // 出错时把输入框回填为当前标的,便于修改重试
        setKlineError(e.message);
      })
      .finally(() => {
        if (!cancelled) setKlineLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [klineOpen, klineOid, start, end]);

  // 标的输入框提交:严格校验 6 位数字 + 交易所后缀
  function submitKlineOid(e) {
    e.preventDefault();
    const value = klineDraft.trim().toUpperCase();
    if (!OID_STRICT_RE.test(value)) {
      setKlineError('标的代码格式应为 6 位数字 + 交易所后缀,如 000001.XSHE。');
      return;
    }
    setKlineError('');
    setKlineOid(value);
  }

  // K 线区错误提示的关闭(清错后可重新展开输入框)
  function dismissKlineError() {
    setKlineError('');
  }

  return (
    <section className="panel chat-panel">
      <div className="panel-head">
        <h2>策略对话 · 生成并回测</h2>
        <div className="panel-actions">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => setText(SAMPLE_REQUIREMENT)}
            disabled={loading}
          >
            填入需求示例
          </button>
        </div>
      </div>

      {/* 需求输入 + 回测区间 */}
      <div className="chat-form">
        <textarea
          className="req-input"
          placeholder="用自然语言描述策略需求,例如:写一个000001.XSHE的双均线择时策略,金叉满仓死叉清仓"
          rows={3}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Ctrl/Cmd + Enter 快捷提交
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !loading) runChat();
          }}
        />
        <div className="form-row">
          <label className="field">
            <span>起始</span>
            <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label className="field">
            <span>结束</span>
            <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          </label>
          <button
            type="button"
            className="btn btn-primary btn-run"
            onClick={runChat}
            disabled={loading}
          >
            {loading ? (
              <>
                <i className="spinner" aria-hidden="true" />
                回测中…
              </>
            ) : (
              '生成并回测'
            )}
          </button>
        </div>
        <div className="form-hint">Ctrl / Cmd + Enter 快捷提交;超时上限 120 秒</div>
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

      {/* 步骤进度条(常驻:未请求时为 pending) */}
      <StepProgress steps={result?.steps} rejected={rejected} running={loading} />

      {/* 管线被拦截 / 提前终止的警告条(HTTP 200 内的业务失败) */}
      {result && result.ok === false && (
        <div className="warn-bar" role="alert">
          <span className="bar-icon">⚠</span>
          <span>{result.error || '管线未完整执行(被校验拦截或生成失败)。'}</span>
        </div>
      )}

      {/* 结果区:仅在有结果后渲染 */}
      {result && (
        <div className="chat-result">
          <MetricsCards metrics={result.metrics} />

          {/* 净值曲线:指标卡与校验结论之间;portfolio_series 为 null/空时组件自身不渲染 */}
          <NetValueChart series={result.portfolio_series} />

          {/* K 线(标的行情):默认折叠;展开后自动从生成代码推断标的拉取蜡烛图,
              推断不出(或上次查询报错)时给出输入框让用户填写;空数据只提示不渲染图 */}
          <section className="kline-section">
            <button
              type="button"
              className="kline-toggle"
              onClick={() => setKlineOpen((o) => !o)}
              aria-expanded={klineOpen}
            >
              <span className="kline-toggle-title">K 线(标的行情)</span>
              <span className="kline-toggle-arrow" aria-hidden="true">
                {klineOpen ? '▾' : '▸'}
              </span>
            </button>
            {klineOpen && (
              <div className="kline-body">
                {(!klineOid || klineError) && (
                  <form className="kline-oid-form" onSubmit={submitKlineOid}>
                    <input
                      className="kline-oid-input mono"
                      value={klineDraft}
                      placeholder="标的代码,如 000001.XSHE"
                      onChange={(e) => setKlineDraft(e.target.value)}
                      aria-label="K 线标的代码"
                    />
                    <button type="submit" className="btn btn-ghost" disabled={klineLoading}>
                      加载
                    </button>
                  </form>
                )}
                {klineOid && !klineError && (
                  <div className="kline-meta mono">
                    {klineOid} · {start} ~ {end}
                  </div>
                )}
                {klineLoading && (
                  <div className="kline-status">
                    <i className="spinner" aria-hidden="true" />
                    K 线加载中…
                  </div>
                )}
                {klineError && (
                  <div className="error-bar kline-error-bar" role="alert">
                    <span className="bar-icon">✗</span>
                    <span>{klineError}</span>
                    <button
                      type="button"
                      className="bar-close"
                      onClick={dismissKlineError}
                      aria-label="关闭"
                    >
                      ✕
                    </button>
                  </div>
                )}
                {!klineLoading &&
                  !klineError &&
                  Array.isArray(klineCandles) &&
                  klineCandles.length === 0 && (
                    <div className="kline-status kline-empty">
                      区间内无行情数据(可能全部停牌或日期越界)。
                    </div>
                  )}
                <KlineChart candles={klineCandles} />
              </div>
            )}
          </section>

          {verdict && (
            <section className="verdict">
              <div className="verdict-head">
                <span className={`badge ${verdict.passed ? 'badge-pass' : 'badge-fail'}`}>
                  {verdict.passed ? '✓ Layer 1 校验通过' : '✗ Layer 1 校验未通过'}
                </span>
                <span className="verdict-counts mono">
                  错误 {verdict.error_count ?? 0} · 警告 {verdict.warning_count ?? 0} · 提示{' '}
                  {Array.isArray(verdict.findings)
                    ? verdict.findings.filter((f) => f?.severity === 'info').length
                    : 0}
                </span>
              </div>
              <FindingsList findings={verdict.findings} emptyText="无发现,校验全部通过。" />
            </section>
          )}

          {result.code && (
            <div className="code-actions">
              <CodeBlock code={result.code} />
              <div className="code-btns">
                <button type="button" className="btn btn-ghost" onClick={copyCode}>
                  {copied ? '已复制 ✓' : '复制代码'}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => onSeedValidate(result.code)}
                >
                  送入校验台 →
                </button>
              </div>
            </div>
          )}

          <div className="report-actions">
            <button
              type="button"
              className="btn btn-outline"
              onClick={() => setShowReport(true)}
              disabled={!result.report}
              title={result.report ? '查看完整回测报告' : '本次未生成报告'}
            >
              查看完整报告
            </button>
          </div>
        </div>
      )}

      {/* 报告弹层:白底 Markdown 渲染(自写轻量渲染器) */}
      {showReport && result?.report && (
        <div className="modal-backdrop" onClick={() => setShowReport(false)}>
          <div
            className="modal-card"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label="策略回测报告"
          >
            <div className="modal-head">
              <h3>策略回测报告</h3>
              <button type="button" className="btn btn-ghost" onClick={() => setShowReport(false)}>
                关闭 ✕
              </button>
            </div>
            <div className="report-view">
              <Markdown text={result.report} />
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
