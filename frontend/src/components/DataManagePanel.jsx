// 本地数据管理面板:数据池盘点(标的明细/文件/容量)+ 预下载 + 删除 + 缓存清理。
// 数据来自 /api/data/* 端点;表格内滚动 + 表头悬浮(sticky)与全局同方案。
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';

const SIZE_UNITS = ['B', 'KB', 'MB', 'GB'];
function fmtSize(bytes) {
  let v = Number(bytes) || 0;
  let u = 0;
  while (v >= 1024 && u < SIZE_UNITS.length - 1) { v /= 1024; u += 1; }
  return `${v.toFixed(v >= 100 || u === 0 ? 0 : 1)} ${SIZE_UNITS[u]}`;
}

const TYPE_LABEL = { CS: '股票', ETF: 'ETF', INDX: '指数' };

export default function DataManagePanel({ notify }) {
  const [overview, setOverview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [fetchOids, setFetchOids] = useState('');
  const [fetching, setFetching] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await api.dataOverview();
      setOverview(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const doFetch = async () => {
    const oids = fetchOids.split(/[,\s;]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
    if (!oids.length) {
      notify('warn', '请先输入要预下载的标的代码(如 600519.XSHG,多个用逗号分隔)。');
      return;
    }
    setFetching(true);
    setError('');
    try {
      const data = await api.dataFetch({ oids });
      if (data.ok) {
        const added = (data.added || []);
        const inPool = (data.fetched || []);
        notify('success',
          `预下载完成:${added.length} 个新标的已入池(${inPool.map((r) => `${r.oid} ${r.rows}行`).join('、') || '无新增'})。`);
        setFetchOids('');
        await load();
      } else {
        setError(data.error || '预下载失败。');
        notify('error', data.error || '预下载失败。');
      }
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setFetching(false);
    }
  };

  const doRemove = async () => {
    const oids = [...selected];
    if (!oids.length) {
      notify('warn', '请先在表格中勾选要删除的标的。');
      return;
    }
    if (!window.confirm(`确认从数据池删除 ${oids.length} 个标的?\n${oids.join('、')}\n\n(基准指数不可删;删除后对应行情/缓存一并清理,再次使用会自动重新获取)`)) {
      return;
    }
    setRemoving(true);
    setError('');
    try {
      const data = await api.dataRemoveInstruments({ oids });
      if (data.ok) {
        notify('success', `已删除 ${data.removed.length} 个标的(缓存同步清理)。`);
        setSelected(new Set());
        await load();
      } else {
        setError(data.error || '删除失败。');
        notify('error', data.error || '删除失败。');
      }
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    } finally {
      setRemoving(false);
    }
  };

  const doClearCache = async () => {
    if (!window.confirm('确认清空全部 K 线缓存(duckdb)?\n\n不影响数据池本体;下次回测会自动重建缓存。')) {
      return;
    }
    setError('');
    try {
      const data = await api.dataCacheClear();
      notify('success', `缓存已清空:${data.removed.length} 个文件,释放 ${fmtSize(data.freed_bytes)}。`);
      await load();
    } catch (e) {
      setError(e.message);
      notify('error', e.message);
    }
  };

  const toggle = (oid) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(oid)) next.delete(oid);
      else next.add(oid);
      return next;
    });
  };

  const rows = overview?.instruments ?? [];
  const counts = overview?.counts ?? {};
  const isProtected = (r) => r.type === 'INDX';

  return (
    <section className="panel datamg-panel">
      <div className="panel-head">
        <h2>本地数据管理</h2>
        <div className="panel-actions">
          <button type="button" className="btn btn-ghost" onClick={load} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </button>
        </div>
      </div>

      {error && (
        <div className="error-bar" role="alert">
          <span className="bar-icon">✗</span>
          <span>{error}</span>
        </div>
      )}

      {/* ---- 总览卡 ---- */}
      {overview && (
        <div className="dm-cards">
          <div className="metric-card">
            <div className="metric-label">数据池标的</div>
            <div className="metric-value mono">
              {counts.total} <span className="dm-sub">股{counts.CS}/ETF {counts.ETF}/指{counts.INDX}</span>
            </div>
          </div>
          <div className="metric-card">
            <div className="metric-label">数据区间</div>
            <div className="metric-value mono dm-small">
              {overview.calendar?.start ?? '—'} ~ {overview.calendar?.end ?? '—'}
            </div>
          </div>
          <div className="metric-card">
            <div className="metric-label">数据池容量</div>
            <div className="metric-value mono">{fmtSize(overview.bundle_size_bytes)}</div>
          </div>
          <div className="metric-card">
            <div className="metric-label">K 线缓存</div>
            <div className="metric-value mono">
              {fmtSize(overview.cache_size_bytes)}
              <button type="button" className="btn btn-ghost dm-clear-btn" onClick={doClearCache}>
                清空缓存
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ---- 预下载 ---- */}
      <div className="dm-fetch">
        <div className="dm-section-title">预下载(自动在线获取并加入数据池)</div>
        <div className="form-row form-row-wrap">
          <input
            className="studio-benchmark-input dm-fetch-input mono"
            value={fetchOids}
            onChange={(e) => setFetchOids(e.target.value)}
            placeholder="600519.XSHG, 159915.XSHE, 000905.XSHG(多个用逗号分隔)"
            aria-label="预下载标的"
            disabled={fetching}
          />
          <button type="button" className="btn btn-primary" onClick={doFetch} disabled={fetching}>
            {fetching ? (<><i className="spinner" aria-hidden="true" />下载中(约 30~90s)…</>) : '开始下载'}
          </button>
        </div>
        <div className="form-hint">
          任意正常上市交易的 A 股股票/ETF/指数均可预下载;已退市或无行情的标的会返回可读错误。
          回测中用到数据池外的标的时也会自动获取 —— 此处可提前下载以避免回测等待。
        </div>
      </div>

      {/* ---- 标的池表格 ---- */}
      {overview && (
        <div className="dm-table-block">
          <div className="dm-section-title dm-with-action">
            <span>数据池标的({counts.total})</span>
            <button
              type="button"
              className="btn btn-ghost dm-del-btn"
              onClick={doRemove}
              disabled={removing || !selected.size}
              title="删除勾选标的(行情+缓存一并清理;基准指数不可删)"
            >
              {removing ? '删除中…' : `删除选中(${selected.size})`}
            </button>
          </div>
          <div className="dh-scroll dm-scroll">
            <table className="dh-table mono">
              <thead>
                <tr>
                  <th className="dm-check"></th>
                  <th>标的</th>
                  <th>类型</th>
                  <th className="num">数据行数</th>
                  <th>首日</th>
                  <th>末日</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.oid} className={selected.has(r.oid) ? 'dm-selected' : ''}>
                    <td className="dm-check">
                      {isProtected(r)
                        ? <span className="dm-prot" title="基准指数不可删除">🔒</span>
                        : <input type="checkbox" checked={selected.has(r.oid)}
                                 onChange={() => toggle(r.oid)} aria-label={`选择 ${r.oid}`} />}
                    </td>
                    <td className="dh-name">{r.symbol}({r.oid})</td>
                    <td>{TYPE_LABEL[r.type] ?? r.type}</td>
                    <td className="num">{r.rows.toLocaleString('zh-CN')}</td>
                    <td>{r.first ?? '—'}</td>
                    <td>{r.last ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ---- 文件明细 ---- */}
      {overview && (
        <div className="dm-table-block">
          <div className="dm-section-title">
            bundle 文件明细({fmtSize(overview.bundle_size_bytes)})
            <span className="mono dm-path">{overview.bundle_path}</span>
          </div>
          <div className="dh-scroll dm-scroll dm-files">
            <table className="dh-table mono">
              <thead>
                <tr><th>文件</th><th className="num">大小</th></tr>
              </thead>
              <tbody>
                {overview.files.map((f) => (
                  <tr key={f.name}>
                    <td className="dh-name">{f.name}</td>
                    <td className="num">{fmtSize(f.size_bytes)}</td>
                  </tr>
                ))}
                {overview.cache_files.map((f) => (
                  <tr key={f.name} className="dm-cache-row">
                    <td className="dh-name">缓存:{f.name}</td>
                    <td className="num">{fmtSize(f.size_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
