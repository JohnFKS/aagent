// 顶栏:平台名 + 版本号 + 后端状态灯(绿=在线 / 灰=离线 / 闪烁=检测中)
export default function Header({ status, version }) {
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
        <span className="version-tag mono">{version ? `v${version}` : 'v—'}</span>
        <span className="status-pill">
          <i className={lampCls} aria-hidden="true" />
          {statusText}
        </span>
      </div>
    </header>
  );
}
