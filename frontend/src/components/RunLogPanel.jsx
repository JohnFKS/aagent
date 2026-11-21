// 运行日志面板(聚宽式回测日志):【初始化】【开盘前】【成交】【撤单】【收盘结算】
// 分段日志 + 策略 print,等宽深底终端样式,INFO/WARN 着色,可折叠/复制。
// 数据来自 /api/backtest、/api/chat、会话回测响应的 run_log
// ({lines: [...], total, truncated, strategy_prints: [...]}),缺失/空时不渲染。
import { useMemo, useState } from 'react';

function classify(line) {
  if (line.includes('WARNING') || line.includes('【拒单】') || line.includes('【撤单】')) return 'rl-warn';
  if (line.includes('【成交】')) return 'rl-trade';
  if (line.includes('=====')) return 'rl-divider';
  return '';
}

export default function RunLogPanel({ runLog, defaultOpen = false }) {
  const lines = useMemo(() => (runLog?.lines ?? []), [runLog]);
  const prints = useMemo(() => (runLog?.strategy_prints ?? []), [runLog]);
  const [open, setOpen] = useState(defaultOpen);
  const [copied, setCopied] = useState(false);

  if (!lines.length && !prints.length) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(lines.join('\n'));
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* 剪贴板不可用时静默 */
    }
  };

  return (
    <details className="rl-wrap" open={open} onToggle={(e) => setOpen(e.target.open)}>
      <summary className="rl-summary">
        运行日志
        <span className="rl-meta mono">
          {lines.length} 行
          {runLog?.truncated ? '(已截断)' : ''}
          {prints.length > 0 ? ` · 策略输出 ${prints.length} 行` : ''}
        </span>
        <span
          className="rl-copy"
          role="button"
          tabIndex={0}
          onClick={(e) => { e.preventDefault(); e.stopPropagation(); copy(); }}
          onKeyDown={(e) => { if (e.key === 'Enter') copy(); }}
        >
          {copied ? '✓ 已复制' : '复制'}
        </span>
      </summary>
      {open && (
        <div className="rl-body mono">
          {lines.map((line, i) => (
            <div key={i} className={`rl-line ${classify(line)}`}>{line}</div>
          ))}
          {prints.length > 0 && (
            <>
              <div className="rl-section-head">—— 策略 print / logger 输出 ——</div>
              {prints.map((line, i) => (
                <div key={`p${i}`} className="rl-line rl-strategy">{line}</div>
              ))}
            </>
          )}
        </div>
      )}
      {!open && <div className="rl-collapsed-hint">点击展开逐日运行日志(初始化/开盘前/成交/收盘结算)</div>}
    </details>
  );
}
