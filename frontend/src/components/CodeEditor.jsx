// 策略工坊代码编辑器:等宽 textarea + 自实现行号列(不引重型编辑器库)。
// - 行号列与 textarea 保持相同字号/行高(22px)与顶部内边距,滚动时用
//   onScroll 把 textarea 的 scrollTop 同步给行号列(行号列 overflow: hidden);
// - **高度随内容自适应**:min = 16 行可视(372px),随代码增减伸缩,
//   超过 40 行内部滚动(不再出现"短代码下面一大片空行"),去掉 resize 手柄;
// - Tab 键插入 4 空格并阻止焦点跳出;Ctrl/Cmd+Enter 触发 onCtrlEnter 快捷提交;
// - 受控组件注意:Tab 场景先直接改 DOM textarea 的 value 并定位光标,
//   再通知 React(state 与 DOM 一致,重渲染不会重置光标位置)。
import { useMemo, useRef } from 'react';

const TAB_SPACES = '    '; // 4 空格

// 高度自适应边界(px):行高 22px + 上下内边距 20px
const LINE_H = 22;
const PAD_Y = 20;
const MIN_H = LINE_H * 16 + PAD_Y; // ≈ 372px,短代码不留大片空白
const MAX_H = LINE_H * 40 + PAD_Y; // ≈ 900px,超长代码内部滚动

export default function CodeEditor({
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
  onCtrlEnter,
  autoHeight = true, // false = 由父容器决定高度(多轮工作台的 flex 满高场景)
}) {
  const gutterRef = useRef(null);
  // 行数 = 按换行切分(末尾换行也计一行,与 textarea 实际渲染一致)
  const lineCount = useMemo(() => String(value ?? '').split('\n').length, [value]);
  // 内容自适应高度(行号列随 flex stretch 自动等高)
  const editorHeight = autoHeight
    ? Math.min(MAX_H, Math.max(MIN_H, lineCount * LINE_H + PAD_Y))
    : undefined;

  // 行号列跟随 textarea 垂直滚动(水平滚动不影响行号)
  function handleScroll(e) {
    if (gutterRef.current) {
      gutterRef.current.scrollTop = e.currentTarget.scrollTop;
    }
  }

  function handleKeyDown(e) {
    // Ctrl/Cmd + Enter:快捷执行所选分析
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && onCtrlEnter) {
      e.preventDefault();
      onCtrlEnter();
      return;
    }
    // Tab:插入 4 空格(Shift+Tab / 组合键交还浏览器默认行为)
    if (e.key !== 'Tab' || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    e.preventDefault();
    const ta = e.currentTarget;
    const src = String(value ?? '');
    const start = ta.selectionStart ?? src.length;
    const end = ta.selectionEnd ?? src.length;
    const next = src.slice(0, start) + TAB_SPACES + src.slice(end);
    // 先直改 DOM 并定位光标,再同步 React state(值一致,重渲染不丢光标)
    ta.value = next;
    ta.selectionStart = ta.selectionEnd = start + TAB_SPACES.length;
    onChange(next);
  }

  return (
    <div className="studio-editor" style={{ height: editorHeight }}>
      <div className="studio-gutter mono" ref={gutterRef} aria-hidden="true">
        {Array.from({ length: lineCount }, (_, i) => (
          <div className="studio-ln" key={i}>
            {i + 1}
          </div>
        ))}
      </div>
      <textarea
        className="studio-ta"
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        aria-label={ariaLabel || '策略代码编辑器'}
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
        onScroll={handleScroll}
        onKeyDown={handleKeyDown}
      />
    </div>
  );
}
