# AQ-Platform Web 前端

"A股回测正确性平台"的网页端(docs/design.md §6 "Agent 网页平台"),React 18 + Vite 单页应用。界面为中文量化终端风格:深色底、等宽数字、红涨绿跌(A 股习惯)。左栏为对话式策略编辑(需求 → `/api/chat` 一条管线:生成 → Layer 1 校验 → 回测 → 评审 → 报告),右栏为校验台(`/api/validate` 仅校验、`/api/backtest` 直接回测)。技术约束:不引入 UI 组件库/图表库/markdown 大依赖,样式为单文件手写 CSS(`src/styles.css`,主题变量集中在 `:root`),报告 Markdown 用自写轻量渲染器(`src/markdown.jsx`)。

## 开发

```bash
cd frontend
pnpm install        # 安装依赖(react 18 / vite 5 / @vitejs/plugin-react)
pnpm run dev        # 启动开发服务器(默认 http://localhost:5173)
```

开发模式下 `vite.config.js` 已把 `/api` 代理到 `http://localhost:8000`,请先启动后端 FastAPI(如 `python -m aquant.api --port 8000`,以仓库实际启动方式为准);后端不在时页面仍可加载,顶栏状态灯转灰、请求以中文错误条 + toast 提示。

## 构建与产物

```bash
pnpm build          # 产物输出到 frontend/dist/
pnpm exec vite preview   # 本地预览 build 产物(默认 http://localhost:4173,/api 同样代理)
```

产物路径为 `frontend/dist/`(含 `index.html` 与 `assets/`),为纯静态文件,可由任意静态服务器托管;部署时把 `/api` 反向代理到 FastAPI 服务即可(前端代码统一使用相对路径 `/api/**`)。

## 目录结构

```
frontend/
├── index.html              # 入口 HTML(中文标题/favicon)
├── vite.config.js          # React 插件 + /api 代理(dev 与 preview)+ outDir: dist
├── src/
│   ├── main.jsx            # React 18 createRoot 挂载
│   ├── App.jsx             # 根组件:顶栏状态轮询、两栏布局、全局 toast、跨栏代码投递
│   ├── api.js              # 统一 fetch 封装(120s 超时、中文错误、422 verdict 透传)
│   ├── samples.js          # 预置示例(需求示例 / 含 .shift(-1) 的坏策略代码)
│   ├── markdown.jsx        # 自写轻量 Markdown 渲染器(标题/表格/列表/粗体/行内代码)
│   ├── styles.css          # 全部样式(深色终端主题,A 股红涨绿跌)
│   └── components/
│       ├── Header.jsx       # 顶栏:平台名 / 版本号 / 后端状态灯
│       ├── ChatPanel.jsx    # 左栏:需求输入 + 生成并回测 + 结果区 + 报告弹层
│       ├── StepProgress.jsx # 管线五步进度条(edit→validate→backtest→review→report)
│       ├── MetricsCards.jsx # 指标卡片(百分比两位小数,收益正红负绿)
│       ├── FindingsList.jsx # 校验发现列表(✗红/⚠黄/ℹ灰)
│       ├── CodeBlock.jsx    # 可折叠代码块(行号 + 轻量 Python 着色)
│       └── ValidatePanel.jsx# 右栏:代码粘贴 + 仅校验 / 校验并回测
└── dist/                   # build 产物
```
