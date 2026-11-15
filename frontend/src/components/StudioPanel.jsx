// 策略工坊(StudioPanel):手动模式 —— 用户自己编辑策略代码、自行选择回测与分析,
// 全程不依赖 LLM。对应需求:"项目并不是全 agent 的,用户也可以自己编辑策略代码
// 然后自行选择如何回测如何分析"。
// - 代码编辑器(CodeEditor,自实现行号 + Tab 缩进) + 三套起步模板(可过校验、可成交);
// - 本地策略库:localStorage `aquant_studio_strategies`([{name, code, ts}]),
//   保存(命名)/载入/删除/导出 .py;编辑草稿自动存 `aquant_studio_draft`,刷新恢复;
// - 回测设置:起止日期 + 初始资金;高级折叠区(保守参数覆盖 → overrides,
//   留空 = 平台默认,激进值由后端 422 拦截并展示 findings);
// - 分析方式自选(至少一项):基础回测(必选)/ 分层验证(/api/verify)/ K 线行情;
// - 提交按勾选项顺序执行,loading 分步展示;422 拦截条 + findings、400 bundle 指引、
//   超时友好提示。顶部说明条强调:所有回测仍经四层校验(保守参数强制)。
import { useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { STUDIO_TEMPLATES, DEFAULT_START, DEFAULT_END } from '../samples.js';
import CodeEditor from './CodeEditor.jsx';
import MetricsCards from './MetricsCards.jsx';
import KlineChart from './KlineChart.jsx';
import FindingsList from './FindingsList.jsx';
import CodeBlock from './CodeBlock.jsx';
import VerifyResult from './VerifyResult.jsx';

// localStorage 键:本地策略库(数组)/ 编辑草稿(纯文本)
const LS_LIBRARY = 'aquant_studio_strategies';
const LS_DRAFT = 'aquant_studio_draft';

// 默认初始资金:100 万
const DEFAULT_CASH = '1000000';

// A 股 order_book_id 形态(与 ChatPanel 同口径):
// 宽松版用于从代码提取,严格版用于校验手动输入
const OID_EXTRACT_RE = /\b\d{6}\.(?:XSHE|XSHG|BJ)\b/;
const OID_STRICT_RE = /^\d{6}\.(XSHE|XSHG|BJ)$/;

// 从策略代码正则提取第一个 order_book_id(如 context.s1 = "000001.XSHE");无则 ''
function extractOid(code) {
  const m = OID_EXTRACT_RE.exec(String(code || '').toUpperCase());
  return m ? m[0] : '';
}

// 读取本地策略库(损坏/越权形态一律按空库处理,不崩)
function loadLibrary() {
  try {
    const arr = JSON.parse(localStorage.getItem(LS_LIBRARY) || '[]');
    if (!Array.isArray(arr)) return [];
    return arr.filter(
      (it) => it && typeof it.name === 'string' && typeof it.code === 'string',
    );
  } catch {
    return [];
  }
}

function persistLibrary(list) {
  try {
    localStorage.setItem(LS_LIBRARY, JSON.stringify(list));
    return true;
  } catch {
    return false; // 隐私模式 / 配额溢出:静默失败,界面仍可用
  }
}

// 导出文件名:替换文件系统非法字符,限长
function sanitizeFilename(name) {
  const base = String(name || 'strategy').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 50);
  return base || 'strategy';
}

// 时间戳 → 本地可读时间
function fmtTs(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '—';
  try {
    return new Date(n).toLocaleString('zh-CN', { hour12: false });
  } catch {
    return '—';
  }
}

export default function StudioPanel({ notify }) {
  // ---- 编辑器与草稿 ----
  // 首次进入:恢复上次编辑草稿;无草稿时载入默认模板(双均线,最直观)
  const [code, setCode] = useState(() => {
    try {
      const draft = localStorage.getItem(LS_DRAFT);
      if (typeof draft === 'string' && draft.trim()) return draft;
    } catch {
      /* localStorage 不可用:直接用默认模板 */
    }
    return STUDIO_TEMPLATES[0].code;
  });
  const [templateKey, setTemplateKey] = useState(''); // 模板下拉是"动作",选完即复位占位项

  // 草稿自动保存(400ms 防抖,避免每个键击都写 localStorage)
  useEffect(() => {
    const timer = setTimeout(() => {
      try {
        localStorage.setItem(LS_DRAFT, code);
      } catch {
        /* 忽略写入失败(隐私模式/配额) */
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [code]);

  // ---- 本地策略库 ----
  const [library, setLibrary] = useState(loadLibrary);
  const [saveName, setSaveName] = useState('');

  // ---- 回测设置 ----
  const [start, setStart] = useState(DEFAULT_START);
  const [end, setEnd] = useState(DEFAULT_END);
  const [cash, setCash] = useState(DEFAULT_CASH);
  const [advOpen, setAdvOpen] = useState(false); // 高级(保守参数)折叠区
  const [slippage, setSlippage] = useState(''); // 留空 = 平台默认 0.002
  const [volumePercent, setVolumePercent] = useState(''); // 留空 = 平台默认

  // ---- 分析方式(基础回测必选,其余自选)----
  const [doVerify, setDoVerify] = useState(false);
  const [doKline, setDoKline] = useState(false);

  // ---- 执行状态 ----
  const [phase, setPhase] = useState(''); // '' | 'basic' | 'verify' | 'kline'
  const [error, setError] = useState(''); // 网络 / HTTP 层错误条

  // ---- 结果 ----
  const [btRes, setBtRes] = useState(null); // {kind:'ok'|'rejected'|'error', ...}
  const [verifyFail, setVerifyFail] = useState(null); // {kind:'rejected'|'error', detail, verdict?}
  const [verifyRes, setVerifyRes] = useState(null); // /api/verify 200 响应

  // ---- K 线(勾选后执行;标的优先从代码提取,提取不到给输入框)----
  const [klineOid, setKlineOid] = useState('');
  const [klineDraft, setKlineDraft] = useState('');
  const [klineCandles, setKlineCandles] = useState(null);
  const [klineLoading, setKlineLoading] = useState(false);
  const [klineError, setKlineError] = useState('');

  const running = phase !== '';
  // 代码中可自动识别的标的(每次渲染实时计算,用于决定是否显示手动输入框)
  const autoOid = useMemo(() => extractOid(code), [code]);

  // ---- 模板 ----
  function applyTemplate(key) {
    const tpl = STUDIO_TEMPLATES.find((t) => t.key === key);
    if (!tpl) return;
    // 覆盖非空且不同的自定义代码前先确认,防误丢手写内容
    if (code.trim() && code !== tpl.code && !window.confirm(`载入模板"${tpl.name}"会覆盖当前编辑内容,是否继续?`)) {
      return;
    }
    setCode(tpl.code);
    notify('info', `已载入模板:${tpl.name}(草稿已同步)。`);
  }

  function onTemplateSelect(e) {
    const key = e.target.value;
    setTemplateKey(''); // 下拉复位到占位项(纯动作语义)
    if (key) applyTemplate(key);
  }

  // ---- 本地策略库:保存 / 载入 / 删除 / 导出 ----
  function saveStrategy() {
    const name = saveName.trim();
    if (!name) {
      setError('保存前请先填写策略名。');
      return;
    }
    if (!code.trim()) {
      setError('当前编辑器为空,没有可保存的代码。');
      return;
    }
    const item = { name, code, ts: Date.now() };
    const next = [...library.filter((it) => it.name !== name), item]; // 同名覆盖(更新)
    if (persistLibrary(next)) {
      setLibrary(next);
      notify('success', `已保存策略"${name}"到本地策略库。`);
    } else {
      setError('保存失败:浏览器 localStorage 不可用(隐私模式或配额已满)。');
    }
  }

  function loadStrategy(item) {
    if (code.trim() && code !== item.code && !window.confirm(`载入"${item.name}"会覆盖当前编辑内容,是否继续?`)) {
      return;
    }
    setCode(item.code);
    setSaveName(item.name);
    setError('');
    notify('info', `已载入本地策略:${item.name}。`);
  }

  function deleteStrategy(name) {
    const next = library.filter((it) => it.name !== name);
    if (persistLibrary(next)) {
      setLibrary(next);
      notify('info', `已删除本地策略:${name}。`);
    } else {
      setError('删除失败:浏览器 localStorage 不可用。');
    }
  }

  // 导出 .py:Blob + 隐藏 <a download>(无需后端)
  function exportStrategy(item) {
    try {
      const blob = new Blob([item.code], { type: 'text/x-python;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${sanitizeFilename(item.name)}.py`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      notify('error', '导出失败:浏览器下载能力不可用。');
    }
  }

  // ---- 高级参数 → overrides(只随填写项发送;激进值由后端 422 拦截)----
  function buildOverrides() {
    const sim = {};
    if (slippage.trim() !== '') sim.slippage = Number(slippage);
    if (volumePercent.trim() !== '') sim.volume_percent = Number(volumePercent);
    return Object.keys(sim).length > 0 ? { sys_simulation: sim } : null;
  }

  // ---- K 线拉取(手动模式:/api/kline 直接查真实价日线)----
  async function fetchKline(oid) {
    setKlineLoading(true);
    setKlineError('');
    try {
      const data = await api.kline({ oid, start, end });
      setKlineCandles(Array.isArray(data?.candles) ? data.candles : []);
    } catch (e) {
      setKlineCandles(null);
      setKlineDraft(oid); // 出错时回填输入框,便于改后重试
      setKlineError(e.message);
    } finally {
      setKlineLoading(false);
    }
  }

  function submitKlineOid(e) {
    e.preventDefault();
    const value = klineDraft.trim().toUpperCase();
    if (!OID_STRICT_RE.test(value)) {
      setKlineError('标的代码格式应为 6 位数字 + 交易所后缀,如 000001.XSHE。');
      return;
    }
    setKlineOid(value);
    fetchKline(value);
  }

  // ---- 执行所选分析(按勾选项顺序;基础回测被拦/失败即短路)----
  async function runAnalyses() {
    // 前端预检(与后端校验互补:格式类错误就地拦截)
    if (!code.trim()) {
      setError('请先编写或载入策略代码(可从模板下拉起步)。');
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
    const cashNum = Number(cash);
    if (!Number.isFinite(cashNum) || cashNum <= 0) {
      setError('初始资金应为正数(默认 1000000)。');
      return;
    }
    if (slippage.trim() !== '' && !Number.isFinite(Number(slippage))) {
      setError('滑点应为数字(如 0.003);留空使用平台默认 0.002。');
      return;
    }
    if (volumePercent.trim() !== '' && !Number.isFinite(Number(volumePercent))) {
      setError('成交量占比应为数字(如 0.1);留空使用平台默认。');
      return;
    }

    setError('');
    setBtRes(null);
    setVerifyRes(null);
    setVerifyFail(null);
    setKlineCandles(null);
    setKlineError('');

    const overrides = buildOverrides();
    const payload = { code, start, end, cash: cashNum };
    if (overrides) payload.overrides = overrides;

    // 1) 基础回测(必选)
    let blocked = false;
    setPhase('basic');
    try {
      const data = await api.backtest(payload);
      setBtRes({ kind: 'ok', data: data || {} });
      notify(
        'success',
        `基础回测完成${data?.trades_count != null ? `,成交 ${data.trades_count} 笔` : ''}。`,
      );
    } catch (e) {
      if (e.data && e.data.verdict) {
        // 422:运行时校验拦截,错误体带 verdict → 拦截条 + findings
        setBtRes({ kind: 'rejected', detail: e.message, verdict: e.data.verdict });
        notify('warn', '基础回测被校验拦截(422),已跳过后续分析。');
      } else {
        setBtRes({ kind: 'error', detail: e.message, status: e.status });
        notify('error', e.message);
      }
      blocked = true; // 校验拦截 / bundle 缺失 / 网络故障:后续分析无意义,短路
    }

    // 2) 分层验证(可选;两次回测,较慢)
    if (!blocked && doVerify) {
      setPhase('verify');
      try {
        const data = await api.verify(payload);
        setVerifyRes(data || {});
        const st = String(data?.status || '').toLowerCase();
        if (st === 'pass') notify('success', '分层验证完成:全部检查通过。');
        else if (st === 'fail') notify('error', '分层验证完成:存在未通过检查。');
        else notify('warn', '分层验证完成:存在警告级检查,详见对照表。');
      } catch (e) {
        if (e.data && e.data.verdict) {
          setVerifyFail({ kind: 'rejected', detail: e.message, verdict: e.data.verdict });
          notify('warn', '分层验证被校验拦截(422)。');
        } else {
          setVerifyFail({ kind: 'error', detail: e.message, status: e.status });
          notify('error', `分层验证失败:${e.message}`);
        }
        // 验证失败不阻断 K 线(行情查询与策略校验相互独立)
      }
    }

    // 3) K 线行情(可选;标的优先从代码正则提取)
    if (doKline) {
      setPhase('kline');
      const oid = autoOid || extractOid(code);
      if (oid) {
        setKlineOid(oid);
        await fetchKline(oid);
      } else {
        // 提取不到:展示输入框,由用户填写后单独加载
        setKlineOid('');
        setKlineDraft('');
        setKlineCandles(null);
        setKlineError('');
      }
    }

    setPhase('');
  }

  // loading 文案:分步展示当前正在执行的分析
  function phaseLabel() {
    switch (phase) {
      case 'basic':
        return '回测中…';
      case 'verify':
        return '分层验证中…(较慢)';
      case 'kline':
        return '拉取 K 线…';
      default:
        return '执行所选分析';
    }
  }

  // 分步进度:每个勾选项的状态(待执行/执行中/完成/被拦截/失败)
  const progressItems = [];
  {
    const basicState = running && phase === 'basic'
      ? 'running'
      : btRes
        ? btRes.kind === 'ok'
          ? 'done'
          : btRes.kind === 'rejected'
            ? 'rejected'
            : 'failed'
        : 'pending';
    progressItems.push({ label: '基础回测', state: basicState });
    if (doVerify) {
      const st = running && phase === 'verify'
        ? 'running'
        : verifyRes
          ? 'done'
          : verifyFail
            ? verifyFail.kind === 'rejected'
              ? 'rejected'
              : 'failed'
            : 'pending';
      progressItems.push({ label: '分层验证', state: st });
    }
    if (doKline) {
      const st = running && phase === 'kline'
        ? 'running'
        : klineLoading
          ? 'running'
          : klineCandles
            ? 'done'
            : klineError
              ? 'failed'
              : 'pending';
      progressItems.push({ label: 'K 线行情', state: st });
    }
  }

  const hasAnyResult = !!(btRes || verifyRes || verifyFail || klineCandles || klineError || klineLoading);

  function clearAll() {
    setCode('');
    setBtRes(null);
    setVerifyRes(null);
    setVerifyFail(null);
    setKlineOid('');
    setKlineDraft('');
    setKlineCandles(null);
    setKlineLoading(false);
    setKlineError('');
    setError('');
    setSaveName('');
    setSlippage('');
    setVolumePercent('');
    setAdvOpen(false);
  }

  // 超时/连接类错误附加场景提示
  const isTimeoutish = /超时|无法连接/.test(error);

  return (
    <>
      {/* ---- 编辑 + 设置面板 ---- */}
      <section className="panel">
        <div className="panel-head">
          <h2>策略工坊 · 手动模式</h2>
          <div className="panel-actions">
            <button
              type="button"
              className="btn btn-ghost"
              onClick={clearAll}
              disabled={running}
              title="清空编辑器与本次结果(不影响本地策略库)"
            >
              清空
            </button>
          </div>
        </div>

        {/* 顶部说明条 */}
        <div className="studio-note">
          策略工坊 = 手动模式:自己写代码,自己选回测与分析;所有回测仍经四层校验(保守参数强制)。
        </div>

        {/* 编辑器工具行:模板下拉 + 策略名 + 保存 */}
        <div className="form-row form-row-wrap studio-editor-tools">
          <label className="field">
            <span>模板</span>
            <select
              className="studio-select"
              value={templateKey}
              onChange={onTemplateSelect}
              disabled={running}
              title="载入起步代码:空白模板 / 双均线择时 / RSI 超卖"
            >
              <option value="">载入模板…</option>
              {STUDIO_TEMPLATES.map((t) => (
                <option key={t.key} value={t.key}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <input
            className="studio-save-input"
            placeholder="策略名(保存到本地库)"
            value={saveName}
            disabled={running}
            onChange={(e) => setSaveName(e.target.value)}
            aria-label="策略名"
          />
          <button
            type="button"
            className="btn btn-ghost"
            onClick={saveStrategy}
            disabled={running}
            title="把当前代码按策略名保存到浏览器本地策略库(同名覆盖)"
          >
            保存
          </button>
        </div>

        {/* 代码编辑器(行号 + Tab 缩进) */}
        <CodeEditor
          value={code}
          onChange={setCode}
          placeholder="在此编写 RQAlpha 策略(含 init / handle_bar);可先从上方模板载入起步代码…"
          onCtrlEnter={runAnalyses}
        />
        <div className="form-hint">
          Tab 键插入 4 空格;编辑内容自动存为草稿(localStorage: aquant_studio_draft),刷新后自动恢复。
        </div>

        {/* 本地策略库 */}
        <div className="studio-lib">
          <div className="studio-lib-head">
            本地策略库({library.length})
            <span className="studio-lib-note mono">localStorage: aquant_studio_strategies</span>
          </div>
          {library.length === 0 ? (
            <div className="studio-lib-empty">
              暂无已保存策略:填写策略名后点「保存」,即可跨会话复用(载入 / 导出 .py / 删除)。
            </div>
          ) : (
            <ul className="studio-lib-list">
              {library.map((item) => (
                <li key={`${item.name}-${item.ts}`} className="studio-lib-item">
                  <span className="studio-lib-name" title={item.name}>
                    {item.name}
                  </span>
                  <span className="studio-lib-ts mono">{fmtTs(item.ts)}</span>
                  <span className="studio-lib-btns">
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => loadStrategy(item)}
                      disabled={running}
                      title="载入到编辑器(覆盖前会确认)"
                    >
                      载入
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => exportStrategy(item)}
                      disabled={running}
                      title={`导出 ${sanitizeFilename(item.name)}.py 下载`}
                    >
                      导出 .py
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost studio-lib-del"
                      onClick={() => deleteStrategy(item.name)}
                      disabled={running}
                      title="从本地策略库删除"
                    >
                      删除
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* 回测设置 */}
        <div className="studio-settings">
          <div className="studio-settings-title">回测设置</div>
          <div className="form-row form-row-wrap">
            <label className="field field-date">
              <span>起始</span>
              <input
                type="date"
                value={start}
                disabled={running}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label className="field field-date">
              <span>结束</span>
              <input
                type="date"
                value={end}
                disabled={running}
                onChange={(e) => setEnd(e.target.value)}
              />
            </label>
            <label className="field">
              <span>初始资金</span>
              <input
                className="studio-num-input mono"
                type="number"
                min="1"
                step="10000"
                value={cash}
                disabled={running}
                onChange={(e) => setCash(e.target.value)}
                aria-label="初始资金"
              />
            </label>
          </div>

          {/* 高级(保守参数覆盖)折叠区 */}
          <button
            type="button"
            className="kline-toggle studio-adv-toggle"
            onClick={() => setAdvOpen((o) => !o)}
            aria-expanded={advOpen}
          >
            <span className="kline-toggle-title">高级(保守参数覆盖,可选)</span>
            <span className="kline-toggle-arrow" aria-hidden="true">
              {advOpen ? '▾' : '▸'}
            </span>
          </button>
          {advOpen && (
            <div className="studio-adv-body">
              <div className="form-row form-row-wrap">
                <label className="field">
                  <span>滑点</span>
                  <input
                    className="studio-num-input mono"
                    type="number"
                    step="0.001"
                    min="0"
                    placeholder="平台默认 0.002"
                    value={slippage}
                    disabled={running}
                    onChange={(e) => setSlippage(e.target.value)}
                    aria-label="滑点"
                  />
                </label>
                <label className="field">
                  <span>成交量占比</span>
                  <input
                    className="studio-num-input mono"
                    type="number"
                    step="0.01"
                    min="0"
                    placeholder="平台默认(≤0.2)"
                    value={volumePercent}
                    disabled={running}
                    onChange={(e) => setVolumePercent(e.target.value)}
                    aria-label="成交量占比"
                  />
                </label>
              </div>
              <div className="form-hint">
                留空 = 平台默认(保守)。仅允许更保守:滑点 ≥ 0.002、成交量占比 ≤ 0.2;
                填写后随 overrides 发送(如 {'{ sys_simulation: { slippage: 0.005, volume_percent: 0.1 } }'}),
                激进值会被后端 422 拦截并在此展示原因。
              </div>
            </div>
          )}
        </div>

        {/* 分析方式自选 */}
        <div className="studio-analyses">
          <div className="studio-settings-title">分析方式(提交时执行勾选项,至少一项)</div>
          <div className="form-row form-row-wrap studio-check-row">
            <label className="checkbox" title="基础回测为必选项:返回指标卡、策略代码与校验发现">
              <input type="checkbox" checked disabled readOnly />
              基础回测(/api/backtest)→ 指标 + 代码 + findings
            </label>
            <label className="checkbox" title="分层验证:样本内/样本外对照 + 规则检查 + 随机基准 + 模拟盘清单(两次回测,较慢)">
              <input
                type="checkbox"
                checked={doVerify}
                disabled={running}
                onChange={(e) => setDoVerify(e.target.checked)}
              />
              分层验证(Layer 4,较慢:两次回测)
            </label>
            <label className="checkbox" title="K 线行情:标的自动从代码识别,识别不到时给输入框">
              <input
                type="checkbox"
                checked={doKline}
                disabled={running}
                onChange={(e) => setDoKline(e.target.checked)}
              />
              K 线行情(/api/kline)
            </label>
          </div>
        </div>

        {/* 提交行 */}
        <div className="form-row studio-submit-row">
          <button
            type="button"
            className="btn btn-primary"
            onClick={runAnalyses}
            disabled={running}
            title="按勾选顺序执行:基础回测 → 分层验证 → K 线"
          >
            {running ? (
              <>
                <i className="spinner" aria-hidden="true" />
                {phaseLabel()}
              </>
            ) : (
              '执行所选分析'
            )}
          </button>
          <span className="studio-submit-hint">
            Ctrl / Cmd + Enter 快捷执行;超时上限:回测 120 秒 / 分层验证 300 秒
          </span>
        </div>

        {/* 分步进度 */}
        {(running || hasAnyResult) && (
          <div className="studio-progress" aria-live="polite">
            {progressItems.map((it) => (
              <span key={it.label} className={`studio-progress-item st-${it.state}`}>
                {it.state === 'running' ? (
                  <i className="spinner" aria-hidden="true" />
                ) : (
                  <span className="studio-progress-icon" aria-hidden="true">
                    {it.state === 'done'
                      ? '✓'
                      : it.state === 'rejected'
                        ? '⚠'
                        : it.state === 'failed'
                          ? '✗'
                          : '·'}
                  </span>
                )}
                {it.label}
                {it.state === 'running'
                  ? phase === 'verify'
                    ? '(较慢)'
                    : ''
                  : ''}
              </span>
            ))}
          </div>
        )}

        {/* 网络 / HTTP 错误条(同时已 toast) */}
        {error && (
          <div className="error-bar" role="alert">
            <span className="bar-icon">✗</span>
            <span>{error}</span>
            <button
              type="button"
              className="bar-close"
              onClick={() => setError('')}
              aria-label="关闭"
            >
              ✕
            </button>
          </div>
        )}
        {error && isTimeoutish && (
          <div className="form-hint studio-err-hint">
            提示:分层验证需两次回测(约 5~20 秒,真实 bundle 更慢);后端超时后可直接重试。
          </div>
        )}

        {/* K 线手动输入:勾选了 K 线且标的识别不到 / 上次查询报错 */}
        {doKline && (!klineOid || klineError) && (
          <form className="kline-oid-form" onSubmit={submitKlineOid}>
            {!autoOid && (
              <span className="studio-kline-hint">代码中未识别到标的(形如 000001.XSHE),请输入:</span>
            )}
            <input
              className="kline-oid-input mono"
              value={klineDraft}
              placeholder="标的代码,如 000001.XSHE"
              onChange={(e) => setKlineDraft(e.target.value)}
              aria-label="K 线标的代码"
            />
            <button type="submit" className="btn btn-ghost" disabled={klineLoading || running}>
              加载 K 线
            </button>
          </form>
        )}
      </section>

      {/* ---- 结果面板 ---- */}
      <section className="panel">
        {!hasAnyResult && (
          <div className="arena-empty">
            <div className="arena-empty-main">编辑策略 → 选分析方式 → 执行所选分析</div>
            <div className="arena-empty-sub">
              可从「模板」下拉载入起步代码;保存后的策略在本地策略库随时载入。结果将展示在这里。
            </div>
          </div>
        )}

        {/* 基础回测:200 → 指标卡 + 校验发现 + 策略代码 */}
        {btRes?.kind === 'ok' && (
          <section className="backtest-result">
            <div className="verdict-head">
              <span className={`badge ${btRes.data.passed ? 'badge-pass' : 'badge-fail'}`}>
                {btRes.data.passed ? '✓ 基础回测完成' : '✗ 回测异常'}
              </span>
              <span className="verdict-counts mono">成交笔数 {btRes.data.trades_count ?? '—'}</span>
            </div>
            <MetricsCards metrics={btRes.data.metrics} />
            {btRes.data.verdict && (
              <>
                <div className="verdict-counts mono sub-head">校验结论(四层校验)</div>
                <FindingsList findings={btRes.data.verdict.findings} emptyText="无发现。" />
              </>
            )}
            {code.trim() && <CodeBlock code={code} title="本次执行的策略代码" />}
          </section>
        )}

        {/* 基础回测:422 拦截 → 拦截条 + findings */}
        {btRes?.kind === 'rejected' && (
          <section className="backtest-result">
            <div className="warn-bar" role="alert">
              <span className="bar-icon">⚠</span>
              <span>基础回测被运行时校验拦截:{btRes.detail}</span>
            </div>
            <FindingsList findings={btRes.verdict?.findings} emptyText="无发现。" />
          </section>
        )}

        {/* 基础回测:400 bundle 缺失 / 5xx / 超时 */}
        {btRes?.kind === 'error' && (
          <section className="backtest-result">
            <div className="error-bar" role="alert">
              <span className="bar-icon">✗</span>
              <span>基础回测失败:{btRes.detail}</span>
            </div>
            {btRes.status === 400 && (
              <div className="form-hint studio-err-hint">
                本机回测需要数据 bundle:请以 AQUANT_BUNDLE_PATH 指向已构建的 bundle 启动后端
                (构建方式见 README / python -m aquant.data.build_bundle --help)。
              </div>
            )}
          </section>
        )}

        {/* 分层验证:200 → VerifyResult(徽章 + IS/OOS 对照 + checks + 随机基准 + 清单) */}
        {verifyRes && <VerifyResult data={verifyRes} />}

        {/* 分层验证:422 拦截 / 失败 */}
        {verifyFail?.kind === 'rejected' && (
          <section className="backtest-result">
            <div className="warn-bar" role="alert">
              <span className="bar-icon">⚠</span>
              <span>分层验证被运行时校验拦截:{verifyFail.detail}</span>
            </div>
            <FindingsList findings={verifyFail.verdict?.findings} emptyText="无发现。" />
          </section>
        )}
        {verifyFail?.kind === 'error' && (
          <section className="backtest-result">
            <div className="error-bar" role="alert">
              <span className="bar-icon">✗</span>
              <span>分层验证失败:{verifyFail.detail}</span>
            </div>
          </section>
        )}

        {/* K 线(标的行情) */}
        {doKline && (
          <section className="studio-kline">
            <div className="sub-head">K 线(标的行情)</div>
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
            {!klineLoading && !klineError && Array.isArray(klineCandles) && klineCandles.length === 0 && (
              <div className="kline-status kline-empty">区间内无行情数据(可能全部停牌或日期越界)。</div>
            )}
            <KlineChart candles={klineCandles} />
          </section>
        )}
      </section>
    </>
  );
}
