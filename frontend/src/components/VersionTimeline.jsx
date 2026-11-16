// 策略版本时间线:全程可见(来源/说明/指标/行数)、可编辑说明、可删除、可回退、可选为 diff 基准。
import { useState } from 'react';
import { formatValue } from './MetricsCards.jsx';

const SOURCE_LABEL = { initial: '初始', agent: 'Agent', user: '手动', rollback: '回退' };

export default function VersionTimeline({
  versions,
  currentId,
  compareFrom,
  onCompare,
  onRollback,
  onView,
  onEditMeta,
  onDelete,
}) {
  const [editingVid, setEditingVid] = useState('');
  const [draft, setDraft] = useState({ message: '', analysis: '' });

  if (!versions || versions.length === 0) {
    return <div className="conv-empty">暂无版本。发起一轮对话或手动编辑代码后生成 v1。</div>;
  }
  return (
    <div className="version-list">
      {versions.map((v) => {
        const isCurrent = v.id === currentId;
        const isBase = v.id === compareFrom;
        const metrics = v.metrics;
        return (
          <div
            key={v.id}
            className={`version-item ${isCurrent ? 'version-current' : ''} ${isBase ? 'version-base' : ''}`}
          >
            <div className="version-top">
              <span className="version-id mono">{v.id}</span>
              <span className={`version-source src-${v.source || 'agent'}`}>
                {SOURCE_LABEL[v.source] || v.source || '未知'}
              </span>
              {isCurrent && <span className="badge badge-pass">当前</span>}
              {isBase && <span className="badge badge-info">对比基准</span>}
              <span className="version-time mono">{String(v.created_at || '').slice(5, 16)}</span>
            </div>
            {editingVid === v.id ? (
              <div className="version-edit">
                <input
                  aria-label="版本说明"
                  placeholder="版本说明"
                  value={draft.message}
                  onChange={(e) => setDraft({ ...draft, message: e.target.value })}
                />
                <textarea
                  aria-label="版本分析"
                  placeholder="Agent 修改分析(可选)"
                  value={draft.analysis}
                  onChange={(e) => setDraft({ ...draft, analysis: e.target.value })}
                />
                <div className="version-edit-actions">
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => { onEditMeta(v.id, draft); setEditingVid(''); }}
                  >
                    保存
                  </button>
                  <button type="button" className="btn btn-ghost" onClick={() => setEditingVid('')}>
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="version-msg" title={v.message || ''}>
                  {v.message || '(无修改说明)'}
                </div>
                {v.analysis && (
                  <div className="version-analysis" title={v.analysis}>
                    {v.analysis.length > 90 ? `${v.analysis.slice(0, 90)}…` : v.analysis}
                  </div>
                )}
              </>
            )}
            <div className="version-metrics mono">
              {metrics ? (
                <>
                  夏普 {formatValue('num2', metrics.sharpe)} · 年化{' '}
                  {formatValue('percent', metrics.annualized_returns)} · 回撤{' '}
                  {formatValue('percent', metrics.max_drawdown)}
                </>
              ) : (
                <span className="conv-muted">未回测</span>
              )}
              <span className="version-lines">{v.lines ?? 0} 行</span>
            </div>
            {editingVid !== v.id && (
              <div className="version-actions">
                <button type="button" className="btn btn-ghost" onClick={() => onView(v.id)}>
                  查看代码
                </button>
                <button type="button" className="btn btn-ghost" onClick={() => onCompare(v.id)}>
                  设为基准
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={isCurrent}
                  onClick={() => onRollback(v.id)}
                  title={isCurrent ? '已经是当前版本' : '回退不删除历史,可再次前进'}
                >
                  回退到此
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => {
                    setEditingVid(v.id);
                    setDraft({ message: v.message || '', analysis: v.analysis || '' });
                  }}
                >
                  编辑说明
                </button>
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={() => onDelete(v.id)}
                  title="永久删除此版本,不可恢复"
                >
                  删除
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
