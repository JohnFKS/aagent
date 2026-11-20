import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';

const PRESETS = [
  { key: 'openai', label: 'OpenAI', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', auth_scheme: 'bearer' },
  { key: 'deepseek', label: 'DeepSeek', base_url: 'https://api.deepseek.com', model: 'deepseek-chat', auth_scheme: 'bearer' },
  { key: 'moonshot', label: 'Moonshot', base_url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k', auth_scheme: 'bearer' },
  { key: 'qwen', label: '通义千问', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus', auth_scheme: 'bearer' },
  { key: 'zhipu', label: '智谱', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash', auth_scheme: 'bearer' },
  { key: 'custom', label: '自定义', base_url: '', model: '', auth_scheme: 'bearer' },
];

const EMPTY = {
  provider: 'custom', base_url: '', api_key: '', model: '',
  auth_scheme: 'bearer', timeout: 120, max_retries: 2,
};

export default function SettingsPanel({ open, onClose, notify }) {
  const [form, setForm] = useState(EMPTY);
  const [current, setCurrent] = useState(null);
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState('');

  const load = useCallback(async () => {
    try {
      const data = await api.llmSettings();
      setCurrent(data);
      setForm({
        provider: data.provider || 'custom',
        base_url: data.base_url || '',
        api_key: '',
        model: data.model || '',
        auth_scheme: data.auth_scheme || 'bearer',
        timeout: Number(data.timeout || 120),
        max_retries: Number(data.max_retries || 2),
      });
    } catch (err) {
      notify('error', `读取 LLM 设置失败:${err.message}`);
    }
  }, [notify]);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const set = (key, value) => setForm((f) => ({ ...f, [key]: value }));

  const applyPreset = (preset) => {
    setForm((f) => ({
      ...f,
      provider: preset.key,
      base_url: preset.base_url || f.base_url,
      model: preset.model || f.model,
      auth_scheme: preset.auth_scheme,
    }));
  };

  const payload = () => ({
    provider: form.provider,
    base_url: form.base_url.trim(),
    api_key: form.api_key.trim() || undefined,
    model: form.model.trim(),
    auth_scheme: form.auth_scheme,
    timeout: Number(form.timeout),
    max_retries: Number(form.max_retries),
  });

  const onTest = async () => {
    setBusy('test');
    try {
      const res = await api.testLlmSettings(payload());
      notify(res.ok ? 'success' : 'warn', res.ok ? `连接成功:${res.reply}` : `连接失败:${res.detail}`);
    } catch (err) {
      notify('error', `连接失败:${err.message}`);
    } finally { setBusy(''); }
  };

  const onSave = async () => {
    setBusy('save');
    try {
      const res = await api.saveLlmSettings(payload());
      setCurrent(res);
      setForm((f) => ({ ...f, api_key: '' }));
      notify('success', '大模型设置已保存,后续对话与优化立即生效。');
    } catch (err) {
      notify('error', `保存失败:${err.message}`);
    } finally { setBusy(''); }
  };

  const onReset = async () => {
    setBusy('reset');
    try {
      const res = await api.resetLlmSettings();
      setCurrent(res);
      setForm({ ...EMPTY });
      notify('success', '已恢复为内置确定性模型。');
    } catch (err) {
      notify('error', `重置失败:${err.message}`);
    } finally { setBusy(''); }
  };

  if (!open) return null;

  return (
    <div className="settings-overlay" onMouseDown={onClose}>
      <aside className="settings-drawer" onMouseDown={(e) => e.stopPropagation()} aria-label="大模型 API 设置">
        <div className="settings-head">
          <div>
            <div className="settings-eyebrow">工作台配置</div>
            <h2>大模型 API 设置</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭设置">×</button>
        </div>

        <div className={`settings-status ${current?.configured ? 'is-on' : ''}`}>
          <span className="settings-status-dot" />
          <div>
            <strong>{current?.configured ? '真实大模型已启用' : '当前使用内置确定性模型'}</strong>
            <div className="settings-status-sub">
              {current?.configured
                ? `${current.provider || 'custom'} · ${current.model} · ${current.api_key_hint}`
                : '配置后将用于策略生成、评审与 AI 优化'}
            </div>
          </div>
        </div>

        <div className="settings-section">
          <label className="settings-label">服务商预设</label>
          <div className="preset-grid">
            {PRESETS.map((p) => (
              <button
                key={p.key}
                type="button"
                className={`preset ${form.provider === p.key ? 'preset-active' : ''}`}
                onClick={() => applyPreset(p)}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        <div className="settings-form">
          <label className="settings-field">
            <span>Base URL</span>
            <input value={form.base_url} placeholder="https://api.example.com/v1" onChange={(e) => set('base_url', e.target.value)} />
          </label>
          <label className="settings-field">
            <span>模型名称</span>
            <input value={form.model} placeholder="gpt-4o-mini" onChange={(e) => set('model', e.target.value)} />
          </label>
          <label className="settings-field">
            <span>API Key</span>
            <div className="key-input">
              <input
                type={showKey ? 'text' : 'password'}
                value={form.api_key}
                placeholder={current?.has_api_key ? `已保存 ${current.api_key_hint}` : 'sk-...'}
                onChange={(e) => set('api_key', e.target.value)}
                autoComplete="off"
              />
              <button type="button" className="key-toggle" onClick={() => setShowKey((v) => !v)}>
                {showKey ? '隐藏' : '显示'}
              </button>
            </div>
          </label>
          <div className="settings-row">
            <label className="settings-field">
              <span>认证方式</span>
              <select value={form.auth_scheme} onChange={(e) => set('auth_scheme', e.target.value)}>
                <option value="bearer">Bearer Token</option>
                <option value="api-key">api-key Header</option>
              </select>
            </label>
            <label className="settings-field">
              <span>超时（秒）</span>
              <input type="number" min="10" max="600" value={form.timeout} onChange={(e) => set('timeout', e.target.value)} />
            </label>
          </div>
        </div>

        <div className="settings-note">
          密钥仅保存在服务端本地文件（权限 0600），接口不会回传明文。切换后立即生效，无需重启。
        </div>

        <div className="settings-actions">
          <button className="btn btn-ghost" type="button" onClick={onTest} disabled={!!busy}>
            {busy === 'test' ? '测试中…' : '测试连接'}
          </button>
          <button className="btn btn-danger-ghost" type="button" onClick={onReset} disabled={!!busy}>
            恢复默认
          </button>
          <button className="btn btn-primary" type="button" onClick={onSave} disabled={!!busy}>
            {busy === 'save' ? '保存中…' : '保存设置'}
          </button>
        </div>
      </aside>
    </div>
  );
}
