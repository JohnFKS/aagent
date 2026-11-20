// 应用根组件:左侧工作台导航 + 右侧内容区 + 大模型设置抽屉。
// 所有面板常驻挂载,仅切换可见性,保证对话/编辑/回测状态不丢失。
import { useCallback, useEffect, useRef, useState } from 'react';
import Header from './components/Header.jsx';
import ChatPanel from './components/ChatPanel.jsx';
import ConversationPanel from './components/ConversationPanel.jsx';
import ValidatePanel from './components/ValidatePanel.jsx';
import ArenaPanel from './components/ArenaPanel.jsx';
import StudioPanel from './components/StudioPanel.jsx';
import SettingsPanel from './components/SettingsPanel.jsx';
import { api } from './api.js';

const TOAST_ICON = { error: '✗', success: '✓', warn: '⚠', info: 'ℹ' };

const TABS = [
  { key: 'chat', label: '策略对话', hint: '对话生成 + 校验台', icon: 'chat' },
  { key: 'conversation', label: '多轮工作台', hint: '持续迭代同一策略', icon: 'layers' },
  { key: 'arena', label: '策略竞技场', hint: '多候选同场回测', icon: 'arena' },
  { key: 'studio', label: '策略工坊', hint: '手动模式与 AI 优化', icon: 'studio' },
];

function NavIcon({ name }) {
  const common = { viewBox: '0 0 20 20', width: 18, height: 18, fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round' };
  if (name === 'chat') return <svg {...common}><path d="M3.5 4.5h13v9h-7l-4 3v-3h-2z" /></svg>;
  if (name === 'layers') return <svg {...common}><path d="M10 2.8 17 6l-7 3.2L3 6z" /><path d="m3 10 7 3.2L17 10M3 14l7 3.2L17 14" /></svg>;
  if (name === 'arena') return <svg {...common}><path d="M6 3h8v4a4 4 0 0 1-8 0z" /><path d="M6 4H3.5v2A2.5 2.5 0 0 0 6 8.5M14 4h2.5v2A2.5 2.5 0 0 1 14 8.5M10 11v3M7 17h6M8 14h4v3H8z" /></svg>;
  return <svg {...common}><path d="m7.5 6-4 4 4 4M12.5 6l4 4-4 4" /></svg>;
}

export default function App() {
  const [health, setHealth] = useState({ status: 'checking', version: null });
  const [llmLabel, setLlmLabel] = useState('确定性模型');
  const [toasts, setToasts] = useState([]);
  const [seed, setSeed] = useState({ code: '', n: 0 });
  const [studioSeed, setStudioSeed] = useState({ code: '', n: 0 });
  const [tab, setTab] = useState('chat');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const idRef = useRef(1);

  const notify = useCallback((kind, text) => {
    const id = idRef.current;
    idRef.current += 1;
    setToasts((ts) => [...ts.slice(-4), { id, kind, text }]);
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), 6500);
  }, []);

  const loadLlmLabel = useCallback(async () => {
    try {
      const data = await api.llmSettings();
      setLlmLabel(data.configured ? `${data.provider || 'custom'} · ${data.model}` : '确定性模型');
    } catch {
      setLlmLabel('模型状态未知');
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const data = await api.health();
        if (alive) setHealth({ status: 'ok', version: data?.version ?? null });
      } catch {
        if (alive) setHealth((h) => ({ status: 'down', version: h.version }));
      }
    };
    check();
    loadLlmLabel();
    const timer = setInterval(() => { check(); loadLlmLabel(); }, 30000);
    return () => { alive = false; clearInterval(timer); };
  }, [loadLlmLabel]);

  const sendToValidator = useCallback((code) => {
    setSeed((s) => ({ code, n: s.n + 1 }));
    notify('info', '已把生成代码送入右侧校验台。');
  }, [notify]);

  const sendToStudio = useCallback((code) => {
    setStudioSeed((s) => ({ code, n: s.n + 1 }));
    setTab('studio');
    notify('info', '已把生成代码送入策略工坊编辑器。');
  }, [notify]);

  return (
    <div className="app app-shell">
      <aside className="app-sidebar">
        <div className="sidebar-brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 16 16" width="20" height="20">
              <rect width="16" height="16" rx="4" fill="var(--accent-soft)" />
              <path d="M3 11.5l3-4 2.6 2.6L12.5 4" stroke="var(--accent)" fill="none" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </span>
          <div>
            <div className="sidebar-title">AQ Platform</div>
            <div className="sidebar-subtitle">A股回测正确性平台</div>
          </div>
        </div>

        <div className="sidebar-section-label">工作空间</div>
        <nav className="main-tabs" role="tablist" aria-label="功能页签">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={tab === t.key}
              className={`main-tab nav-item ${tab === t.key ? 'main-tab-active' : ''}`}
              onClick={() => setTab(t.key)}
              title={t.hint}
            >
              <NavIcon name={t.icon} />
              <span>{t.label}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <button type="button" className="nav-item settings-nav" onClick={() => setSettingsOpen(true)}>
            <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6"><circle cx="10" cy="10" r="2.6" /><path d="M10 2.6v2.2M10 15.2v2.2M2.6 10h2.2M15.2 10h2.2M4.8 4.8l1.6 1.6M13.6 13.6l1.6 1.6M15.2 4.8l-1.6 1.6M6.4 13.6l-1.6 1.6" /></svg>
            <span>大模型设置</span>
          </button>
          <div className="sidebar-foot-note">四层校验 · 真实数据 · 可溯源回测</div>
        </div>
      </aside>

      <div className="app-main">
        <Header status={health.status} version={health.version} llmLabel={llmLabel} onOpenSettings={() => setSettingsOpen(true)} />
        <main className="content">
          <section className={`layout ${tab === 'chat' ? '' : 'layout-hidden'}`}>
            <ChatPanel notify={notify} onSeedValidate={sendToValidator} onSeedStudio={sendToStudio} />
            <ValidatePanel notify={notify} seed={seed} />
          </section>
          <section className={`conv-layout-wrap ${tab === 'conversation' ? '' : 'layout-hidden'}`}>
            <ConversationPanel notify={notify} />
          </section>
          <section className={`arena-layout ${tab === 'arena' ? '' : 'layout-hidden'}`}>
            <ArenaPanel notify={notify} />
          </section>
          <section className={`studio-layout ${tab === 'studio' ? '' : 'layout-hidden'}`}>
            <StudioPanel notify={notify} studioSeed={studioSeed} />
          </section>
        </main>
      </div>

      <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} notify={notify} />
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} onClick={() => setToasts((ts) => ts.filter((x) => x.id !== t.id))}>
            <span className="toast-icon">{TOAST_ICON[t.kind] || 'ℹ'}</span>
            <span className="toast-text">{t.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
