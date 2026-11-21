// 策略编写指南(GuidePanel):面向"用任意大模型网页对话生成策略代码"的用户。
//
// 定位:平台的核心工作流之一 —— 用户不必在本平台配置 LLM,而是把本页提供的
// 提示词模板粘贴到任意模型网页(ChatGPT / Claude / Gemini / DeepSeek / Kimi …),
// 拿回代码后经平台校验台(Layer 1)与策略工坊回测。模板内容与平台 Layer 1 校验
// 规则(docs/strategy-code-conventions.md)逐条对齐,最大化"生成即可回测成功"的
// 命中率;模板还约束了标的池(平台 bundle 内标的),避免"标的无行情"类失败。
//
// 页面结构:
// 1) 三步工作流(复制提示词 → 模型网页生成 → 平台校验回测);
// 2) 一键复制:完整提示词模板 / 极简提示词 / 参考骨架(可送入策略工坊);
// 3) 代码契约速查(允许 / 禁止对照表);
// 4) 标的池(与默认数据 bundle 一致);
// 5) 常见回测失败与修复对照表;
// 6) 回测结果导读(收益图:策略净值 vs A股基准 / 指标卡含义);
// 7) 提交前自查清单与进阶路径(分层验证 / 竞技场 / AI 优化)。
import { useCallback, useState } from 'react';
import {
  ALLOWED_ITEMS,
  CHECKLIST_ITEMS,
  FORBIDDEN_ITEMS,
  PROMPT_FULL,
  PROMPT_MINI,
  SKELETON_CODE,
  STOCK_POOL,
  TROUBLE_ITEMS,
} from '../guideContent.js';

// 复制按钮:优先 navigator.clipboard,失败降级 textarea + execCommand(隐私模式)
function CopyButton({ text, label = '复制', notify, className = '' }) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(async () => {
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        ok = document.execCommand('copy');
        document.body.removeChild(ta);
      } catch {
        ok = false;
      }
    }
    setCopied(ok);
    notify?.(ok ? 'success' : 'error', ok ? '已复制到剪贴板。' : '复制失败:请手动选择文本复制。');
    setTimeout(() => setCopied(false), 2200);
  }, [text, notify]);

  return (
    <button
      type="button"
      className={`btn btn-ghost guide-copy-btn ${className}`}
      onClick={copy}
      title="复制到剪贴板"
    >
      {copied ? '✓ 已复制' : label}
    </button>
  );
}

// 参考骨架代码块(等宽 + 可复制 + 可送入策略工坊)
function SkeletonBlock({ onSeedStudio, notify }) {
  return (
    <div className="guide-code-block">
      <div className="guide-code-head">
        <span className="mono">demo_ma_cross.py · 双均线择时骨架</span>
        <span className="guide-code-actions">
          <CopyButton text={SKELETON_CODE} label="复制代码" notify={notify} />
          {onSeedStudio && (
            <button
              type="button"
              className="btn btn-ghost guide-copy-btn"
              onClick={() => onSeedStudio(SKELETON_CODE)}
              title="把骨架代码送入「策略工坊」编辑器"
            >
              送入策略工坊 →
            </button>
          )}
        </span>
      </div>
      <pre className="guide-code mono">{SKELETON_CODE}</pre>
    </div>
  );
}

// 提示词模板块(整块可复制)
function PromptBlock({ title, hint, text, notify, tone = '' }) {
  return (
    <div className={`guide-prompt-block ${tone}`}>
      <div className="guide-prompt-head">
        <div>
          <div className="guide-prompt-title">{title}</div>
          <div className="guide-prompt-hint">{hint}</div>
        </div>
        <CopyButton text={text} label="一键复制提示词" notify={notify} className="guide-copy-primary" />
      </div>
      <pre className="guide-prompt-body mono">{text}</pre>
    </div>
  );
}

function InfoTable({ headers, rows }) {
  return (
    <div className="guide-table-wrap">
      <table className="guide-table">
        <thead>
          <tr>{headers.map((h) => <th key={h}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((cell, j) => <td key={j} className={j === 0 ? 'guide-cell-key' : ''}>{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function GuidePanel({ notify, onSeedStudio }) {
  return (
    <div className="guide-layout">
      {/* ---- 1. 页头:指南定位 ---- */}
      <section className="panel guide-hero">
        <div className="panel-head">
          <h2>量化策略编写指南</h2>
        </div>
        <p className="guide-lede">
          用<strong>任意大模型网页对话</strong>(ChatGPT / Claude / Gemini / DeepSeek / Kimi / 文心一言…)生成
          RQAlpha 策略代码,回到本平台<strong>校验 → 回测</strong>。本页的提示词模板已逐条对齐平台
          四层校验规则(未来函数 / 外部数据 / 墙钟时间禁令、保守参数强制),照模板生成的代码
          <strong>默认配置下可直接回测成功</strong>;万一失败,按「常见失败与修复」表贴回模型自纠即可。
        </p>
        <div className="guide-flow">
          <div className="guide-flow-step">
            <span className="guide-flow-num mono">1</span>
            <div>
              <div className="guide-flow-title">复制提示词</div>
              <div className="guide-flow-desc">下方模板一键复制,替换策略需求占位</div>
            </div>
          </div>
          <span className="guide-flow-arrow" aria-hidden="true">→</span>
          <div className="guide-flow-step">
            <span className="guide-flow-num mono">2</span>
            <div>
              <div className="guide-flow-title">模型网页生成</div>
              <div className="guide-flow-desc">粘贴到任意模型对话,拿到唯一 python 代码块</div>
            </div>
          </div>
          <span className="guide-flow-arrow" aria-hidden="true">→</span>
          <div className="guide-flow-step">
            <span className="guide-flow-num mono">3</span>
            <div>
              <div className="guide-flow-title">回平台回测</div>
              <div className="guide-flow-desc">校验台过 Layer 1 → 策略工坊回测,看收益图与基准对照</div>
            </div>
          </div>
        </div>
      </section>

      {/* ---- 2. 提示词模板(核心交付物) ---- */}
      <section className="panel">
        <div className="panel-head">
          <h2>① 提示词模板(复制到任意模型网页)</h2>
        </div>
        <div className="guide-note">
          模板 = 平台代码契约的全量快照。<strong>只需替换【我的策略需求】一段</strong>,其余内容请勿删改 ——
          每一条都对应一道平台校验闸门,删掉哪条,生成的代码就可能在哪条上翻车。
        </div>
        <PromptBlock
          title="完整提示词模板(推荐)"
          hint="包含硬性规则、标的池、参考骨架与输出格式约束;生成的代码命中率最高。"
          text={PROMPT_FULL}
          notify={notify}
          tone="guide-prompt-main"
        />
        <PromptBlock
          title="极简提示词(一段话版)"
          hint="快速对话用;约束同完整版但更精简,适合移动端或轻量模型。"
          text={PROMPT_MINI}
          notify={notify}
        />
        <div className="guide-note">
          模板用法示例:「把【我的策略需求】替换为『用 5 日均线和 60 日均线做宁德时代择时,金叉买死叉卖,
          仓位 80%』→ 复制全部 → 粘贴到模型网页发送 → 复制它输出的 python 代码块 → 回平台」。
        </div>
      </section>

      {/* ---- 3. 拿到代码之后 ---- */}
      <section className="panel">
        <div className="panel-head">
          <h2>② 拿到代码之后:校验 → 回测</h2>
        </div>
        <ol className="guide-steps-list">
          <li>
            <strong>校验台</strong>(「策略对话」页右栏):粘贴模型输出的代码 → 「仅校验」看 Layer 1 结论;
            有 ✗ 就把发现贴回模型对话修复,直到全绿。
          </li>
          <li>
            <strong>策略工坊</strong>(手动模式):代码粘入编辑器 → 选日期(默认 2023-01-03 ~ 2024-06-28)→
            选 A股基准(任意 bundle 内标的:上证指数 / 沪深300 / 池内个股)→ 执行分析 →
            看<strong>收益图</strong>(策略净值曲线 + 基准对照线 + 回撤阴影带 + 买卖标记 + 水下副图)与指标卡。
          </li>
          <li>
            <strong>进阶</strong>:勾选「分层验证」防过拟合(样本内/样本外对照);多份候选代码可去「策略竞技场」同场排名;
            编辑器右键式调参可交给 AI 自动优化。
          </li>
        </ol>
        <SkeletonBlock onSeedStudio={onSeedStudio} notify={notify} />
        <div className="guide-note">
          平台也内置 LLM 对话生成(「策略对话」页,需在「大模型设置」里配置 API);不配置时用确定性模板兜底。
          本指南的意义在于:<strong>不配置 LLM 也能借助外部模型网页完成全流程</strong>。
        </div>
      </section>

      {/* ---- 4. 代码契约速查 ---- */}
      <section className="panel">
        <div className="panel-head">
          <h2>③ 代码契约速查(为什么模板长这样)</h2>
        </div>
        <div className="guide-cols">
          <div>
            <div className="guide-table-title guide-ok-title">✓ 允许使用</div>
            <InfoTable
              headers={['能力', '用法']}
              rows={ALLOWED_ITEMS.map((it) => [it.title, it.detail])}
            />
          </div>
          <div>
            <div className="guide-table-title guide-bad-title">✗ 一律禁止</div>
            <InfoTable
              headers={['禁区', '规则']}
              rows={FORBIDDEN_ITEMS.map((it) => [it.title, it.detail])}
            />
          </div>
        </div>
        <div className="guide-note">
          完整规范见仓库 <span className="mono">docs/strategy-code-conventions.md</span>;校验规则实现在
          <span className="mono"> aquant.validation.ast_rules</span>(AST 静态扫描,拒绝即 422)。
        </div>
      </section>

      {/* ---- 5. 标的池 ---- */}
      <section className="panel">
        <div className="panel-head">
          <h2>④ 标的池(平台默认数据 bundle 内可回测)</h2>
        </div>
        <div className="guide-pool">
          {STOCK_POOL.map((s) => (
            <span key={s.oid} className="guide-pool-item" title={`${s.name}(${s.note})`}>
              <span className="mono">{s.oid}</span>
              <span className="guide-pool-name">{s.name}</span>
              <span className="guide-pool-note">{s.note}</span>
            </span>
          ))}
        </div>
        <div className="guide-note">
          数据区间 <span className="mono">2022-01-04 ~ 2024-12-31</span>;均线类策略的预热期会自动用到
          回测起始日之前的 bundle 数据(history_bars 可取到),无需为此把起始日提前。
          想扩充标的池:重建 bundle(<span className="mono">python -m aquant.data.build_bundle --codes …</span>)。
        </div>
      </section>

      {/* ---- 6. 常见失败与修复 ---- */}
      <section className="panel">
        <div className="panel-head">
          <h2>⑤ 常见回测失败与修复</h2>
        </div>
        <InfoTable
          headers={['现象(平台提示)', '怎么修']}
          rows={TROUBLE_ITEMS.map((t) => [t.symptom, t.fix])}
        />
        <div className="guide-note">
          修复套路:<strong>把平台的报错原文贴回模型对话</strong>,加上一句「按上述硬性规则修复」,通常一轮就能自愈。
        </div>
      </section>

      {/* ---- 7. 回测结果导读 ---- */}
      <section className="panel">
        <div className="panel-head">
          <h2>⑥ 回测结果导读(收益图与指标)</h2>
        </div>
        <div className="guide-legend-demo">
          <span className="guide-legend-item"><i className="legend-swatch" aria-hidden="true" />策略净值(蓝,首日 = 1.0)</span>
          <span className="guide-legend-item"><i className="legend-line legend-line-bm" aria-hidden="true" />A股基准(金:上证指数 / 沪深300)</span>
        </div>
        <InfoTable
          headers={['图 / 指标', '怎么读']}
          rows={[
            ['收益图 · 策略净值', '首日归一为 1.0;曲线上方 = 盈利。虚线 1.0 基线 = 保本线。'],
            ['收益图 · A股基准', '同期"买入持有"基准净值(真实指数行情);两条线的差距 = 超额收益。'],
            ['指标卡 · 总收益 / 年化', '区间累计与年化收益;A 股习惯红涨绿跌。'],
            ['指标卡 · 基准收益', '基准同期累计收益,直接与总收益比大小。'],
            ['指标卡 · 夏普 / 最大回撤', '风险调整后收益 / 最深亏损;夏普 ≥1、回撤可控是常用及格线。'],
            ['指标卡 · 佣金 / 印花税', '交易成本合计;成本占比过高说明换手过频(模板仓位法可压低换手)。'],
          ]}
        />
        <div className="guide-note">
          平台默认强制 0.2% 滑点、万八佣金 + 千一印花税、T+1、涨跌停与成交量限制 ——
          收益图与指标已经是<strong>贴近实盘的保守口径</strong>,不是理想化数字。
        </div>
      </section>

      {/* ---- 8. 自查清单 ---- */}
      <section className="panel">
        <div className="panel-head">
          <h2>⑦ 提交前自查清单</h2>
        </div>
        <ul className="guide-checklist">
          {CHECKLIST_ITEMS.map((item) => (
            <li key={item}>
              <span className="guide-check-mark" aria-hidden="true">✓</span>
              {item}
            </li>
          ))}
        </ul>
        <div className="guide-note">
          回测通过 ≠ 可以实盘:先跑「分层验证」(样本外不衰减 + 随机交易基准对照),再对照模拟盘清单逐步推进。
        </div>
      </section>
    </div>
  );
}
