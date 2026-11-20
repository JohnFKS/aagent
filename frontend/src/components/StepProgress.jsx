// 管线步骤进度条:edit → validate → backtest → review → report
// - 请求期间(running)全部为 pending,按顺序做脉冲动画;
// - 响应到达后按后端 steps 数组逐个点亮,并在下方列出每步中文 summary;
// - backtest 步被拒(rejected=true 且顶层 rejected)标红显示"校验拦截"。
// 健壮性:兼容 ok / passed / rejected / skipped 字段的不同组合;
// 缺失的步骤(如 LLM 三次生成失败时无 backtest 及后续)显示"未执行"。

// 五个固定步骤的中文标签
const STEP_DEFS = [
  { key: 'edit', label: '生成代码' },
  { key: 'validate', label: 'Layer 1 校验' },
  { key: 'backtest', label: '回测执行' },
  { key: 'review', label: '评审复核' },
  { key: 'report', label: '报告生成' },
];

function labelOf(key) {
  const def = STEP_DEFS.find((d) => d.key === key);
  return def ? def.label : key;
}

// 由单个 step 元素推导状态:'done' | 'failed' | 'blocked' | 'skipped'
function deriveStatus(apiStep, isBacktest, rejected) {
  if (!apiStep) return 'absent'; // 响应里没有该步骤(提前终止)
  if (apiStep.rejected === true) return 'blocked'; // 回测被校验拦截
  if (apiStep.skipped === true) return 'skipped'; // 未注入 runner 等场景的跳过
  // ok 优先;validate 步在后端实现里用的是 passed 字段;review 等无布尔字段的步骤默认视为完成
  const ok =
    typeof apiStep.ok === 'boolean'
      ? apiStep.ok
      : typeof apiStep.passed === 'boolean'
        ? apiStep.passed
        : true;
  if (!ok) return isBacktest && rejected ? 'blocked' : 'failed';
  return 'done';
}

// 各状态的展示元数据(图标 / CSS 类 / 附加角标)
const STATUS_META = {
  pending: { icon: '', cls: 'st-pending' },
  done: { icon: '✓', cls: 'st-done' },
  failed: { icon: '✗', cls: 'st-failed' },
  blocked: { icon: '✗', cls: 'st-blocked', tag: '校验拦截' },
  skipped: { icon: '◦', cls: 'st-skipped', tag: '跳过' },
  absent: { icon: '–', cls: 'st-absent', tag: '未执行' },
};

export default function StepProgress({ steps, rejected = false, running = false }) {
  const list = Array.isArray(steps) ? steps : [];
  const byKey = {};
  list.forEach((s) => {
    if (s && s.step) byKey[s.step] = s;
  });

  // 汇总每步状态:请求中 → pending;未发起请求(steps 为空)→ pending;响应后 → 实际状态
  const statuses = STEP_DEFS.map((def) => {
    if (running || list.length === 0) return 'pending';
    return deriveStatus(byKey[def.key], def.key === 'backtest', rejected);
  });

  return (
    <div className={`steps${running ? ' steps-running' : ''}`}>
      <div className="steps-bar">
        {STEP_DEFS.map((def, idx) => {
          const meta = STATUS_META[statuses[idx]];
          const apiStep = byKey[def.key];
          return (
            <div
              key={def.key}
              className={`step ${meta.cls}`}
              title={apiStep?.summary || def.label}
            >
              <span className="step-dot" aria-hidden="true">
                {meta.icon}
              </span>
              <span className="step-label">{def.label}</span>
              {meta.tag && <span className="step-tag">{meta.tag}</span>}
              {idx < STEP_DEFS.length - 1 && <i className="step-link" aria-hidden="true" />}
            </div>
          );
        })}
      </div>
      {/* 请求期间的等待提示 */}
      {running && (
        <div className="steps-hint">
          管线执行中:生成 → 校验 → 回测 → 评审 → 报告(真实 LLM 最长约 7 分钟,请耐心等待)…
        </div>
      )}
      {/* 响应后展示后端返回的每步中文 summary */}
      {!running && list.length > 0 && (
        <ul className="step-summaries">
          {list.map((s, i) => (
            <li key={i}>
              <b>{labelOf(s.step)}</b>
              {s.summary ? ` — ${s.summary}` : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
