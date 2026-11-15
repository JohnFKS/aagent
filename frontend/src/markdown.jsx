// 轻量 Markdown 渲染器(自写,无第三方依赖 —— 技术约束禁入大型 md 库)。
// 仅覆盖平台报告实际用到的语法:
//   标题(#/##/###…)、表格(| a | b | 含 --- 分隔行)、无序/有序列表、
//   分隔线(---)、段落、行内 **粗体** 与 `行内代码`;
// 未识别的语法一律按普通段落展示,渲染输出全部经 React 文本转义,无 XSS 注入面。

import { useMemo } from 'react';

const H_RE = /^(#{1,6})\s+(.*)$/;
const UL_RE = /^[-*]\s+(.*)$/;
const OL_RE = /^(\d+)[.、)]\s*(.*)$/;
const HR_RE = /^\s*(-{3,}|\*{3,})\s*$/;

// 行内语法拆分:**粗体** 与 `行内代码`
const INLINE_RE = /(\*\*[^*]+\*\*|`[^`]+`)/g;

// 行内渲染:把一段文本拆成 粗体 / 行内代码 / 普通文本 三类 React 节点
export function renderInline(text) {
  const parts = String(text ?? '').split(INLINE_RE);
  return parts.map((part, i) => {
    if (!part) return null;
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={i}>{part.slice(1, -1)}</code>;
    }
    return <span key={i}>{part}</span>;
  });
}

// 是否为表格行(以 | 开头)
function isTableLine(line) {
  return line.trim().startsWith('|');
}

// 表头分隔行:| --- | :---: | 之类,只含空白/|/:/- 且至少两个 -
function isSepLine(line) {
  if (!isTableLine(line)) return false;
  return /^[\s|:-]+$/.test(line) && (line.match(/-/g) || []).length >= 2;
}

// 把一行 "| a | b |" 拆成单元格数组
function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

// 逐行解析为块数组
function parseBlocks(src) {
  const lines = String(src ?? '').replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    if (HR_RE.test(line)) {
      blocks.push({ type: 'hr' });
      i += 1;
      continue;
    }
    const h = line.match(H_RE);
    if (h) {
      blocks.push({ type: 'heading', level: h[1].length, text: h[2] });
      i += 1;
      continue;
    }
    if (isTableLine(line)) {
      const rows = [];
      while (i < lines.length && isTableLine(lines[i])) {
        rows.push(lines[i]);
        i += 1;
      }
      const hasSep = rows.length >= 2 && isSepLine(rows[1]);
      blocks.push({
        type: 'table',
        header: hasSep ? splitRow(rows[0]) : null,
        rows: (hasSep ? rows.slice(2) : rows).map(splitRow),
      });
      continue;
    }
    if (UL_RE.test(line)) {
      const items = [];
      while (i < lines.length && UL_RE.test(lines[i])) {
        items.push(lines[i].match(UL_RE)[1]);
        i += 1;
      }
      blocks.push({ type: 'ul', items });
      continue;
    }
    if (OL_RE.test(line)) {
      const items = [];
      while (i < lines.length && OL_RE.test(lines[i])) {
        items.push(lines[i].match(OL_RE)[2]);
        i += 1;
      }
      blocks.push({ type: 'ol', items });
      continue;
    }
    // 普通段落:合并连续的普通行(遇到标题/表格/列表/分隔线即止)
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !H_RE.test(lines[i]) &&
      !isTableLine(lines[i]) &&
      !HR_RE.test(lines[i]) &&
      !UL_RE.test(lines[i])
    ) {
      para.push(lines[i].trim());
      i += 1;
    }
    if (para.length) blocks.push({ type: 'para', text: para.join('\n') });
  }
  return blocks;
}

// 单个块 → React 节点
function renderBlock(b, idx) {
  switch (b.type) {
    case 'heading': {
      const Tag = `h${Math.min(b.level, 6)}`;
      return <Tag key={idx}>{renderInline(b.text)}</Tag>;
    }
    case 'hr':
      return <hr key={idx} />;
    case 'para':
      return (
        <p key={idx}>
          {b.text.split('\n').map((ln, j) => (
            <span key={j}>
              {j > 0 && <br />}
              {renderInline(ln)}
            </span>
          ))}
        </p>
      );
    case 'ul':
      return (
        <ul key={idx}>
          {b.items.map((it, j) => (
            <li key={j}>{renderInline(it)}</li>
          ))}
        </ul>
      );
    case 'ol':
      return (
        <ol key={idx}>
          {b.items.map((it, j) => (
            <li key={j}>{renderInline(it)}</li>
          ))}
        </ol>
      );
    case 'table':
      return (
        <div className="md-table-wrap" key={idx}>
          <table>
            {b.header && (
              <thead>
                <tr>
                  {b.header.map((c, j) => (
                    <th key={j}>{renderInline(c)}</th>
                  ))}
                </tr>
              </thead>
            )}
            <tbody>
              {b.rows.map((row, j) => (
                <tr key={j}>
                  {row.map((c, k) => (
                    <td key={k}>{renderInline(c)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    default:
      return null;
  }
}

export default function Markdown({ text }) {
  const blocks = useMemo(() => parseBlocks(text), [text]);
  return <div className="md">{blocks.map((b, i) => renderBlock(b, i))}</div>;
}
