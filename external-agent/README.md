# 外部 Agent 模式接入指南

把 ZCode / Claude Code / Codex 等外部 agent 平台接入 AQ-Platform:外部平台选中任意文件夹作为项目目录(`strategies/` 存策略、`reports/` 存报告),通过 MCP 工具在**它们自己的对话里**完成「策略编辑→生成→回测→优化→验证」全流程;本平台的 Web/CLI 只是后台引擎。

- 方法论(给 agent 读的 skill):[SKILL.md](./SKILL.md)
- 项目目录骨架模板:[project-template/](./project-template/)

## 通用前提(所有平台都需要)

1. **安装并启动平台 API**(安装与 bundle 构建的完整说明见主 README「快速开始」):

```bash
cd /path/to/aagent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 数据 bundle(二选一):合成数据先跑通 / 真实 A 股数据
python -m aquant.data.build_bundle --source synthetic --out ~/.aquant/bundle
pip install -e ".[akshare]"
python -m aquant.data.build_bundle --source akshare \
    --codes 000001,600519 --start 2022-01-01 --end 2024-12-31 \
    --out ~/.aquant/bundle-real

# 启动 API(前端可选;MCP 只依赖 REST 端点)
uvicorn aquant.api.main:app --port 8000
```

2. **MCP 运行方式**:stdio,`python -m aquant.mcp_server.server`——必须用**能 import aquant 的解释器**(上面的 venv 即可;下文示例统一用 venv 绝对路径 `/path/to/aagent/.venv/bin/python`,请替换为实际路径)。
3. **环境变量**(配置在 MCP server 的 env 块):
   - `AQUANT_API_BASE`:平台 API 地址,默认 `http://127.0.0.1:8000`;
   - `AQUANT_MCP_TOKEN`:平台启用 token 闸门(`AQUANT_MCP_TOKEN`/`AQUANT_MCP_TOKENS`)时必填,需 readwrite 档才能用回测/优化/验证类工具。
4. **准备项目目录**:

```bash
cp -r /path/to/aagent/external-agent/project-template ~/aquant-project
# 之后在外部平台中打开/选中 ~/aquant-project 作为工作目录
```

## Claude Code

```bash
# 注册 MCP server(-- 后是实际启动命令;MCP 配置默认 local scope,仅当前项目)
claude mcp add aquant \
    -e AQUANT_API_BASE=http://127.0.0.1:8000 \
    -- /path/to/aagent/.venv/bin/python -m aquant.mcp_server.server

# 全平台可用改为: claude mcp add -s user aquant -- ...
```

- **cwd/解释器说明**:MCP server 进程由 Claude Code 从当前项目目录启动;`python -m aquant.mcp_server.server` 不依赖 cwd,只依赖解释器能 import aquant——所以推荐 venv 绝对路径,避免「未激活 venv 就启动失败」。
- 安装 SKILL.md(Claude Code 会在匹配场景自动加载):

```bash
mkdir -p ~/.claude/skills/aquant-quant
cp /path/to/aagent/external-agent/SKILL.md ~/.claude/skills/aquant-quant/SKILL.md
```

## ZCode

1. 安装 SKILL.md(用户级技能目录;项目级放项目下 `.zcode/skills/`):

```bash
mkdir -p ~/.zcode/skills/aquant-quant
cp /path/to/aagent/external-agent/SKILL.md ~/.zcode/skills/aquant-quant/SKILL.md
```

2. 注册 MCP:把主 README「方式四:MCP Server」一节的 mcpServers 块写入 ZCode 配置(用户级 `~/.zcode/settings.json`,项目级为项目根 `.zcode/settings.json` 的 `mcpServers` 字段;以所用版本文档为准):

```json
{
  "mcpServers": {
    "aquant": {
      "command": "/path/to/aagent/.venv/bin/python",
      "args": ["-m", "aquant.mcp_server.server"],
      "env": { "AQUANT_API_BASE": "http://127.0.0.1:8000" }
    }
  }
}
```

## Codex / 其他平台

通用 MCP stdio 配置 JSON 模板(`mcpServers` 块,适用于 Claude Desktop、Cursor 等大多数客户端):

```json
{
  "mcpServers": {
    "aquant": {
      "command": "/path/to/aagent/.venv/bin/python",
      "args": ["-m", "aquant.mcp_server.server"],
      "env": {
        "AQUANT_API_BASE": "http://127.0.0.1:8000",
        "AQUANT_MCP_TOKEN": "tkn_xxx"
      }
    }
  }
}
```

Codex CLI 使用 TOML(`~/.codex/config.toml`),字段一一对应:

```toml
[mcp_servers.aquant]
command = "/path/to/aagent/.venv/bin/python"
args = ["-m", "aquant.mcp_server.server"]
env = { AQUANT_API_BASE = "http://127.0.0.1:8000", AQUANT_MCP_TOKEN = "tkn_xxx" }
```

没有独立 skill 机制的平台(Claude Desktop、Codex 等):把 SKILL.md 全文放进该平台的 system prompt / 项目指令文件(如 Codex 的 AGENTS.md),即可获得同一套七步工作流。

## 项目目录约定(project-template/)

```text
~/aquant-project/
├── strategies/README.md   # 策略文件命名与写法要点
├── reports/README.md      # 报告存放约定(文件名带日期与阶段)
└── .gitignore             # 可选:忽略 reports/*.json
```

agent 第一步会读取两个目录内的 README 并遵循;strategies/ 与 reports/ 的详细约定见各目录 README(均 <20 行)。

## 工具清单(MCP,8 个)

| 工具 | 作用 | REST 端点 |
|---|---|---|
| `platform_workflow_guide` | 七步工作流与工具契约(每次会话先调) | - |
| `platform_health` | 健康检查 | GET /api/health |
| `platform_validate_code` | Layer 1 静态校验 | POST /api/validate |
| `platform_run_backtest` | 真实回测(校验为硬门禁) | POST /api/backtest |
| `platform_get_kline` | K 线行情(bundle 真实价日线) | GET /api/kline |
| `platform_optimize` | AI 自动优化闭环 | POST /api/optimize |
| `platform_verify_layered` | 分层验证(IS→OOS + 随机基准,防过拟合) | POST /api/verify |
| `platform_arena` | 多策略竞技场(1~6 候选排名) | POST /api/arena |

## 各平台接入差异速查

| 平台 | MCP 注册 | Skill/方法论安装 |
|---|---|---|
| Claude Code | `claude mcp add aquant -- <venv-python> -m aquant.mcp_server.server` | `~/.claude/skills/aquant-quant/SKILL.md` |
| ZCode | `~/.zcode/settings.json`(或项目 `.zcode/settings.json`)的 mcpServers 块 | `~/.zcode/skills/aquant-quant/SKILL.md` |
| Codex CLI | `~/.codex/config.toml` 的 `[mcp_servers.aquant]` | AGENTS.md 引入 SKILL.md 内容 |
| Claude Desktop 等 | 客户端配置文件的 mcpServers 块(见主 README) | system prompt / 项目指令引入 SKILL.md |

> 维护者注:本文工具名/参数已与 `src/aquant/mcp_server/server.py` 的 8 工具注册逐一核对;后续 server.py 若有变更,以实际注册为准并同步更新本目录文档。
