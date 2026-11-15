// 可折叠的策略代码块:等宽字体 + 行号 + 轻量 Python 语法着色(自写分词,无第三方依赖)。
// 分词按优先级:注释 / 三引号字符串 / 普通字符串 / 装饰器 / 关键字 / 数字,其余为普通文本。
// 所有输出经 React 文本节点渲染(自动转义),无 HTML 注入面。

import { useMemo, useState } from 'react';

// 单个正则内按 alternation 顺序决定优先级;三引号在前避免被普通引号截断
const TOKEN_RE =
  /(#[^\n]*|"""[\s\S]*?"""|'''[\s\S]*?'''|f?"(?:[^"\\\n]|\\.)*"|f?'(?:[^'\\\n]|\\.)*'|@[A-Za-z_][\w.]*|\b(?:def|return|if|elif|else|for|while|import|from|as|and|or|not|in|is|None|True|False|class|try|except|finally|with|lambda|pass|break|continue|global|nonlocal|assert|yield|raise|del)\b|\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b)/g;

// 关键字集合(与正则保持一致)
const KEYWORDS = new Set(
  'def return if elif else for while import from as and or not in is None True False class try except finally with lambda pass break continue global nonlocal assert yield raise del'.split(
    ' ',
  ),
);

function classify(token) {
  if (token.startsWith('#')) return 'tok-comment';
  if (token.startsWith('"""') || token.startsWith("'''")) return 'tok-string';
  if (/^f?["']/.test(token)) return 'tok-string';
  if (token.startsWith('@')) return 'tok-decorator';
  if (KEYWORDS.has(token)) return 'tok-keyword';
  if (/^\d/.test(token)) return 'tok-number';
  return 'tok-plain';
}

// 全文分词:[{text, cls}, ...]
function tokenize(src) {
  const tokens = [];
  let last = 0;
  const re = new RegExp(TOKEN_RE.source, 'g'); // 每次调用用全新 lastIndex
  let m;
  while ((m = re.exec(src)) !== null) {
    if (m.index > last) tokens.push({ text: src.slice(last, m.index), cls: 'tok-plain' });
    tokens.push({ text: m[0], cls: classify(m[0]) });
    last = m.index + m[0].length;
    if (m[0].length === 0) re.lastIndex += 1; // 防御:空匹配死循环
  }
  if (last < src.length) tokens.push({ text: src.slice(last), cls: 'tok-plain' });
  return tokens;
}

// 按 \n 把分词结果切成行,便于渲染行号
function toLines(tokens) {
  const lines = [[]];
  for (const t of tokens) {
    const segs = t.text.split('\n');
    segs.forEach((seg, idx) => {
      if (idx > 0) lines.push([]);
      if (seg) lines[lines.length - 1].push({ text: seg, cls: t.cls });
    });
  }
  return lines;
}

export default function CodeBlock({ code, title = '生成策略代码', defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  const lines = useMemo(() => toLines(tokenize(String(code || ''))), [code]);

  if (!code) return null;

  return (
    <section className="code-block">
      <button type="button" className="code-toggle" onClick={() => setOpen((o) => !o)}>
        <i className={`caret${open ? ' caret-open' : ''}`} aria-hidden="true">
          ▸
        </i>
        <span>{title}</span>
        <span className="mono code-meta">{lines.length} 行</span>
        <span className="code-lang">Python · RQAlpha</span>
      </button>
      {open && (
        <pre className="code-view">
          <code>
            {lines.map((line, i) => (
              // 每行:行号 + 源码(white-space: pre 保留缩进)
              <div className="code-line" key={i}>
                <span className="code-ln mono">{i + 1}</span>
                <span className="code-src">
                  {line.length === 0
                    ? '\u00a0'
                    : line.map((t, j) => (
                        <span key={j} className={t.cls}>
                          {t.text}
                        </span>
                      ))}
                </span>
              </div>
            ))}
          </code>
        </pre>
      )}
    </section>
  );
}
