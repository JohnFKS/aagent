// 校验发现列表:✗ 错误(红) / ⚠ 警告(黄) / ℹ 提示(灰)。
// detail 展开策略(T9 系统测试改进项):severity=info 且 detail 为结构化对象时,
// JSON 默认折叠、点击"详情 ▸"展开(避免提示类长 JSON 喧宾夺主);error/warning
// 与字符串 detail 保持现状直接全显。供左栏(chat 的 verdict.findings)与右栏复用。
import { useState } from 'react';

const SEVERITY_META = {
  error: { icon: '✗', label: '错误', cls: 'finding-error' },
  warning: { icon: '⚠', label: '警告', cls: 'finding-warning' },
  info: { icon: 'ℹ', label: '提示', cls: 'finding-info' },
};

function severityOf(f) {
  const s = String(f?.severity || 'info').toLowerCase();
  return SEVERITY_META[s] ? s : 'info';
}

// detail 兼容:实测后端可能返回字符串或结构化对象(如 cfg.injected_default 的
// {key, value}),统一转为可读文本再渲染,避免"对象不能作为 React 子节点"崩溃。
function detailToText(d) {
  if (d === undefined || d === null || d === '') return '';
  if (typeof d === 'string') return d;
  try {
    return JSON.stringify(d, null, 2);
  } catch {
    return String(d);
  }
}

function FindingItem({ finding }) {
  const sev = severityOf(finding);
  const meta = SEVERITY_META[sev];
  const rawDetail = finding?.detail;
  // info + 对象 detail 才默认折叠;error/warning 与字符串 detail 全显(现状)
  const collapsible =
    sev === 'info' && rawDetail !== null && rawDetail !== undefined && typeof rawDetail === 'object';
  // useState 恒定调用(hooks 顺序稳定):非折叠项 open 恒为 false,不影响渲染分支
  const [open, setOpen] = useState(false);
  const detail = detailToText(rawDetail);
  return (
    <li className={`finding ${meta.cls}`}>
      <span className="finding-icon" aria-hidden="true">
        {meta.icon}
      </span>
      <div className="finding-body">
        <div className="finding-msg">{String(finding?.message ?? '(无消息)')}</div>
        <div className="finding-meta">
          <code className="mono">{String(finding?.rule || 'unknown.rule')}</code>
          <span className="finding-sev">{meta.label}</span>
        </div>
        {detail ? (
          collapsible && !open ? (
            <button
              type="button"
              className="finding-detail-toggle"
              onClick={() => setOpen(true)}
              aria-label="展开详情"
            >
              详情 ▸
            </button>
          ) : (
            <div className="finding-detail">{detail}</div>
          )
        ) : null}
      </div>
    </li>
  );
}

export default function FindingsList({ findings, emptyText }) {
  const list = Array.isArray(findings) ? findings : [];
  if (list.length === 0) {
    return <div className="findings-empty">{emptyText || '无发现。'}</div>;
  }
  return (
    <ul className="findings">
      {list.map((f, i) => (
        <FindingItem key={i} finding={f} />
      ))}
    </ul>
  );
}
