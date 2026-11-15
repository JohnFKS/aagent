// 应用根组件:
// - 顶栏(平台名 / 版本号 / 后端状态灯,来自 GET /api/health,30s 轮询);
// - 顶部 Tab:"策略对话"(默认,两栏:左 ChatPanel + 右 ValidatePanel)| "多策略竞技场"(ArenaPanel)
//   | "策略工坊"(StudioPanel,手动模式:自写代码、自选回测与分析);
//   无路由库,useState 切换;三个 Tab 都保持挂载(仅切换可见性),来回切换不丢对话/编辑状态;
// - 跨栏联动:左栏生成代码可"送入校验台"(seed 自增触发右栏填充),
//   也可"送入策略工坊编辑"(studioSeed 自增触发工坊编辑器填充并切到工坊 Tab);
// - 全局 toast:fetch 错误统一中文提示(6.5s 自动消失,点击可关闭)。
import { useCallback, useEffect, useRef, useState } from 'react';
import Header from './components/Header.jsx';
import ChatPanel from './components/ChatPanel.jsx';
import ValidatePanel from './components/ValidatePanel.jsx';
import ArenaPanel from './components/ArenaPanel.jsx';
import StudioPanel from './components/StudioPanel.jsx';
import { api } from './api.js';

// toast 图标按类型区分
const TOAST_ICON = { error: '✗', success: '✓', warn: '⚠', info: 'ℹ' };

// 顶部 Tab 定义(key → 文案);chat 为默认 Tab(保持原两栏页)
const TABS = [
  { key: 'chat', label: '策略对话', hint: '对话生成策略 + 校验台' },
  { key: 'arena', label: '多策略竞技场', hint: '多候选同场回测排名' },
  { key: 'studio', label: '策略工坊', hint: '手动模式:自写代码、自选回测与分析' },
];

export default function App() {
  // 后端健康状态:'checking' | 'ok' | 'down'
  const [health, setHealth] = useState({ status: 'checking', version: null });
  // toast 队列 [{id, kind, text}]
  const [toasts, setToasts] = useState([]);
  // 左栏 → 右栏的代码投递:{code, n}(n 自增触发右栏 useEffect)
  const [seed, setSeed] = useState({ code: '', n: 0 });
  // 左栏 → 工坊 Tab 的代码投递:{code, n}(n 自增触发 StudioPanel 填充编辑器)
  const [studioSeed, setStudioSeed] = useState({ code: '', n: 0 });
  // 顶部 Tab:'chat'(默认)| 'arena'
  const [tab, setTab] = useState('chat');
  const idRef = useRef(1);

  // 全局通知:新 toast 入队,6.5 秒后自动移除
  const notify = useCallback((kind, text) => {
    const id = idRef.current;
    idRef.current += 1;
    setToasts((ts) => [...ts.slice(-4), { id, kind, text }]);
    setTimeout(() => {
      setToasts((ts) => ts.filter((t) => t.id !== id));
    }, 6500);
  }, []);

  const dismissToast = useCallback((id) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
  }, []);

  // 左栏"送入校验台"回调
  const sendToValidator = useCallback((code) => {
    setSeed((s) => ({ code, n: s.n + 1 }));
    notify('info', '已把生成代码送入右侧校验台。');
  }, [notify]);

  // 左栏"送入策略工坊编辑"回调:写入 studioSeed 并切到工坊 Tab(工坊常驻挂载,状态不丢)
  const sendToStudio = useCallback((code) => {
    setStudioSeed((s) => ({ code, n: s.n + 1 }));
    setTab('studio');
    notify('info', '已把生成代码送入策略工坊编辑器,可继续修改 / 回测 / AI 优化。');
  }, [notify]);

  // 健康检查:挂载即查 + 每 30 秒轮询(短超时,快速判定在线/离线)
  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const data = await api.health();
        if (alive) setHealth({ status: 'ok', version: data?.version ?? null });
      } catch {
        // 后端不在:状态灯转灰,版本号保留上次值
        if (alive) setHealth((h) => ({ status: 'down', version: h.version }));
      }
    };
    check();
    const timer = setInterval(check, 30000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  return (
    <div className="app">
      <Header status={health.status} version={health.version} />
      {/* 顶部 Tab 条:策略对话(默认)| 多策略竞技场 */}
      <nav className="main-tabs" role="tablist" aria-label="功能页签">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={`main-tab ${tab === t.key ? 'main-tab-active' : ''}`}
            onClick={() => setTab(t.key)}
            title={t.hint}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {/* Tab 一:两栏对话页(默认)。竞技场 Tab 下隐藏但保持挂载,状态不丢。 */}
      <main className={`layout ${tab === 'chat' ? '' : 'layout-hidden'}`}>
        <ChatPanel notify={notify} onSeedValidate={sendToValidator} onSeedStudio={sendToStudio} />
        <ValidatePanel notify={notify} seed={seed} />
      </main>

      {/* Tab 二:多策略竞技场(对话 Tab 下隐藏但保持挂载,后台回测结果不丢) */}
      <section className={`arena-layout ${tab === 'arena' ? '' : 'layout-hidden'}`}>
        <ArenaPanel notify={notify} />
      </section>

      {/* Tab 三:策略工坊(手动模式;隐藏但保持挂载,编辑器/草稿/结果不丢) */}
      <section className={`studio-layout ${tab === 'studio' ? '' : 'layout-hidden'}`}>
        <StudioPanel notify={notify} studioSeed={studioSeed} />
      </section>

      {/* 全局 toast 层 */}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} onClick={() => dismissToast(t.id)}>
            <span className="toast-icon">{TOAST_ICON[t.kind] || 'ℹ'}</span>
            <span className="toast-text">{t.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
