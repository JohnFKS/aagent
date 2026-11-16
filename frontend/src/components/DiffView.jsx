// 策略版本变更对比:展示后端 difflib 计算出的结构化行级 diff。
// 不引第三方 diff 库,行数据由 /api/sessions/{sid}/diff 提供。
export default function DiffView({ diff, loading }) {
  if (loading) return <div className="conv-empty">正在计算版本差异…</div>;
  if (!diff) {
    return (
      <div className="conv-empty">
        在版本时间线中把某个版本「设为基准」,再点击「对比」查看策略变更。
      </div>
    );
  }
  const rows = diff.rows || [];
  const stats = diff.stats || {};
  return (
    <div className="diff-view">
      <div className="diff-head">
        <div className="diff-labels mono">
          <span className="diff-label-old">− {diff.from_label}</span>
          <span className="diff-arrow">→</span>
          <span className="diff-label-new">+ {diff.to_label}</span>
        </div>
        <div className="diff-stats mono">
          <span className="diff-stat-add">+{stats.added ?? 0}</span>
          <span className="diff-stat-remove">−{stats.removed ?? 0}</span>
        </div>
      </div>
      <div className="diff-body mono" role="table" aria-label="策略代码差异">
        {rows.length === 0 && <div className="conv-empty">两个版本代码相同,无差异。</div>}
        {rows.map((row, i) => (
          <div key={i} className={`diff-row diff-${row.type}`} role="row">
            <span className="diff-ln" title="旧版本行号">{row.left_no ?? ''}</span>
            <span className="diff-ln" title="新版本行号">{row.right_no ?? ''}</span>
            <span className="diff-sign" aria-hidden="true">
              {row.type === 'add' ? '+' : row.type === 'remove' ? '−' : ' '}
            </span>
            <code className="diff-text">{row.text || ' '}</code>
          </div>
        ))}
      </div>
      <details className="diff-unified">
        <summary>统一 diff 文本(可复制归档)</summary>
        <pre className="mono">{diff.unified}</pre>
      </details>
    </div>
  );
}
