// 顶栏:平台名 + 版本号 + 后端状态灯(绿=在线 / 灰=离线 / 闪烁=检测中)
export default function Header({ status, version, onOpenSettings, llmLabel }) {
  const lampCls =
    status === 'ok' ? 'lamp lamp-ok' : status === 'down' ? 'lamp lamp-down' : 'lamp lamp-check';
  const statusText =
    status === 'ok' ? '后端在线' : status === 'down' ? '后端离线' : '检测中…';

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 16 16" width="18" height="18">
            <rect width="16" height="16" rx="3" fill="var(--accent-soft)" />
            <path
              d="M3 11.5l3-4 2.6 2.6L12.5 4"
              stroke="var(--accent)"
              fill="none"
              strokeWidth="1.7"
              strokeLinecap="round"
            />
          </svg>
        </span>
        <span className="brand-name">AQ-Platform</span>
        <span className="brand-sep">·</span>
        <span className="brand-sub">A股回测正确性平台</span>
      </div>
      <div className="top-meta">
        <span className="llm-chip" title="当前推理模型">{llmLabel || '确定性模型'}</span>
        <span className="version-tag mono">{version ? `v${version}` : 'v—'}</span>
        <span className="status-pill">
          <i className={lampCls} aria-hidden="true" />
          {statusText}
        </span>
        <button className="icon-button" type="button" onClick={onOpenSettings} title="大模型 API 设置" aria-label="大模型 API 设置">
          <svg viewBox="0 0 20 20" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.6">
            <circle cx="10" cy="10" r="2.6" />
            <path d="M10 2.6v2.2M10 15.2v2.2M2.6 10h2.2M15.2 10h2.2M4.8 4.8l1.6 1.6M13.6 13.6l1.6 1.6M15.2 4.8l-1.6 1.6M6.4 13.6l-1.6 1.6" />
          </svg>
        </button>
      </div>
    </header>
  );
}
