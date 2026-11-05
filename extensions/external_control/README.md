# 外部AI控制 拓展

让 DeepSeek Harness、Codex、Claude Code 等外部 AI 程序通过 HTTP/WebSocket 连接本程序，发出结构化命令控制智能体团队与对话。

## 安装与启用

1. 在「功能拓展」管理页找到「外部AI控制」，点击安装并启用
2. 查看 token：打开 `AppData/extensions/external_control/token.txt`（首次启用后端时自动生成）
3. 把 token 配置给外部 AI 客户端

## 接口总览

所有路由挂载在 `/api/ext/external_control/` 前缀下。

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET  | `/info` | 服务信息 + 命令清单 | 无 |
| GET  | `/token` | 查看当前 token | 需 token |
| POST | `/token/reset` | 重置 token | 需 token |
| POST | `/command` | **统一命令入口** | 需 token |
| WS   | `/ws/events/{cid}` | WebSocket 实时事件流 | ?token= |
| GET  | `/events/{cid}` | SSE 事件流（备用） | ?token= |

### 鉴权方式（三选一）

- 请求头：`Authorization: Bearer <token>`
- body 字段：`{"token": "<token>", ...}`
- query 参数：`?token=<token>`

## 统一命令入口

`POST /api/ext/external_control/command`

```json
{
  "cmd": "agents.create",
  "args": {"name": "研究员", "system_prompt": "你是一名研究员"},
  "token": "<token>"
}
```

返回：

```json
{"ok": true, "cmd": "agents.create", "data": {"id": 12, "name": "研究员", ...}}
```

失败：

```json
{"ok": false, "cmd": "agents.create", "error": "ValueError: ..."}
```

## 可用命令

| 命令 | args | 说明 |
|------|------|------|
| `system.info` | — | 系统信息 |
| `agents.list` | — | 列出所有智能体 |
| `agents.create` | `{name, system_prompt?, model?, ...}` | 创建智能体 |
| `agents.update` | `{id, ...fields}` | 更新智能体 |
| `agents.delete` | `{id}` | 删除智能体 |
| `groups.list` | — | 列出分组 |
| `groups.create` | `{name}` | 创建分组 |
| `groups.delete` | `{id}` | 删除分组 |
| `canvases.list` | — | 列出画布（团队） |
| `canvases.create` | `{name, nodes?, edges?, workdir?, execution_mode?}` | 创建画布 |
| `canvases.get` | `{id}` | 画布详情 |
| `canvases.delete` | `{id}` | 删除画布 |
| `conversations.list` | — | 列出会话（剥离 messages） |
| `conversations.create` | `{name?, agent_ids?, canvas_id?, workdir?}` | 创建会话 |
| `conversations.get` | `{id}` | 会话完整消息 |
| `conversations.delete` | `{id}` | 删除会话 |
| `conversations.send` | `{id, agent_id, content}` | **发消息，阻塞聚合返回完整回复** |
| `conversations.stop` | `{id, agent_id?}` | 停止运行 |
| `conversations.running` | `{id}` | 查询运行状态 |

### `conversations.send` 返回

```json
{
  "ok": true,
  "conv_id": 5,
  "agent_id": 3,
  "text": "智能体的完整回复文本",
  "events": [{"seq": 1, "data": "DATA:..."}, ...],
  "event_count": 42
}
```

需要实时流式输出时，用 WebSocket 订阅事件流（见下）。

## 实时事件流

### WebSocket

```
ws://127.0.0.1:8870/api/ext/external_control/ws/events/{cid}?token=<token>&agent_id=<可选>
```

每条消息为 JSON：`{"seq": 1, "data": "事件行"}`，结束发 `{"done": true}`。

### SSE（备用）

```
GET /api/ext/external_control/events/{cid}?token=<token>&agent_id=<可选>
```

每行 `data: <事件行>\n\n`，结束 `data: DONE\n\n`。

## 外部 AI 接入示例

### curl

```bash
# 查看命令清单
curl http://127.0.0.1:8870/api/ext/external_control/info

# 创建智能体
curl -X POST http://127.0.0.1:8870/api/ext/external_control/command \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"agents.create","args":{"name":"研究员","system_prompt":"你是一名研究员"}}'

# 创建画布团队（含两个智能体节点 + 一条 handoff 边）
curl -X POST http://127.0.0.1:8870/api/ext/external_control/command \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"canvases.create","args":{"name":"研究团队","nodes":[{"id":1,"agent_id":10,"x":100,"y":100},{"id":2,"agent_id":11,"x":300,"y":100}],"edges":[{"id":1,"from_node":1,"to_node":2,"type":"handoff"}]}}'

# 创建会话并发消息
curl -X POST http://127.0.0.1:8870/api/ext/external_control/command \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"conversations.create","args":{"name":"测试"}}'
# 假设返回 data.id=5
curl -X POST http://127.0.0.1:8870/api/ext/external_control/command \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"conversations.send","args":{"id":5,"agent_id":10,"content":"研究一下量子计算"}}'
```

### Python（requests + websockets）

```python
import requests, json

BASE = "http://127.0.0.1:8870/api/ext/external_control"
TOKEN = open("AppData/extensions/external_control/token.txt").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def cmd(name, **args):
    r = requests.post(f"{BASE}/command", headers=HEADERS,
                      json={"cmd": name, "args": args})
    return r.json()

# 创建智能体团队
a1 = cmd("agents.create", name="架构师", system_prompt="你负责架构设计")["data"]
a2 = cmd("agents.create", name="编码员", system_prompt="你负责写代码")["data"]
canvas = cmd("canvases.create", name="开发团队",
             nodes=[{"id":1,"agent_id":a1["id"],"x":100,"y":100},
                    {"id":2,"agent_id":a2["id"],"x":300,"y":100}],
             edges=[{"id":1,"from_node":1,"to_node":2,"type":"handoff"}])["data"]
conv = cmd("conversations.create", name="开发任务", canvas_id=canvas["id"])["data"]

# 发消息（阻塞拿完整回复）
res = cmd("conversations.send", id=conv["id"], agent_id=a1["id"],
          content="设计一个聊天系统")
print(res["data"]["text"])

# 实时流式订阅
import asyncio, websockets
async def stream():
    async with websockets.connect(
        f"ws://127.0.0.1:8870/api/ext/external_control/ws/events/{conv['id']}?token={TOKEN}"
    ) as ws:
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("done"): break
            print(msg["data"], end="", flush=True)
```

### DeepSeek Harness / Codex / Claude Code 配置

这些工具通常支持「自定义工具/MCP server/HTTP 工具调用」。把本拓展的 `/command` 端点注册为一个工具：

- **工具名**：`agentcluster_control`
- **描述**：控制 AgentCluster 智能体集群程序：创建智能体、组建团队画布、发起对话、获取回复
- **端点**：`POST http://127.0.0.1:8870/api/ext/external_control/command`
- **鉴权头**：`Authorization: Bearer <token>`
- **入参**：`{cmd: string, args: object}`，cmd 取值见上表
- **出参**：`{ok: bool, data?: any, error?: string}`

让外部 AI 自行决定调用哪个 cmd：例如用户对 Codex 说"在 AgentCluster 里建一个研究团队并让它分析 X"，Codex 即可依次调用 `agents.create` × N → `canvases.create` → `conversations.create` → `conversations.send`，并通过 WebSocket 流式获取智能体输出回显给用户。

## 可视化交互

本拓展为 `backend-service` 类，不直接提供 viz 渲染器。但外部 AI 控制本程序创建的智能体，其输出若含 ` ```viz:type ` 代码块，会由 `visual_interaction` 拓展正常渲染（两个拓展协同工作）。

## 配套技能（给外部 AI 用）

本拓展内置一份配套技能 `agentcluster-control`，位于：

```
AppData/extensions/external_control/skills/agentcluster-control/
  SKILL.md                      ← 技能说明书（Agent Skills 开放标准格式）
  scripts/ac_client.py          ← 轻量命令行客户端（封装 token 与 HTTP 调用）
```

这份技能是**给外部 AI 用的说明书**：外部 AI（Codex / Claude Code / DeepSeek Harness）把整个
`skills/agentcluster-control/` 目录拷入自己的技能目录后，就会自动获得"如何控制 AgentCluster"的知识
（命令清单、工作流、WebSocket 流式用法），无需人工解释接口。

### 把技能装到外部 AI

| 外部 AI | 技能目录 | 安装方式 |
|---------|----------|----------|
| Claude Code | `~/.claude/skills/` 或项目 `.claude/skills/` | 拷贝 `agentcluster-control/` 目录 |
| Codex / 其他兼容 Agent Skills 的工具 | 其约定的 skills 目录 | 拷贝 `agentcluster-control/` 目录 |
| 自定义 harness | 读取 `SKILL.md` 内容注入 system prompt | 直接把 SKILL.md 正文拼进提示词 |

### 用配套脚本快速操作

```bash
# 查看命令清单
python skills/agentcluster-control/scripts/ac_client.py info

# 创建智能体（key=value，值自动转 int/bool/JSON）
python skills/agentcluster-control/scripts/ac_client.py cmd agents.create name=研究员 system_prompt=你负责研究

# 发消息拿完整回复
python skills/agentcluster-control/scripts/ac_client.py cmd conversations.send id=5 agent_id=10 content=分析量子计算

# WebSocket 实时打印智能体输出
python skills/agentcluster-control/scripts/ac_client.py stream 5
```

环境变量：`AC_BASE`（默认 `http://127.0.0.1:8870`）、`AC_TOKEN`（默认自动读取 token.txt）。

> 依赖：`pip install requests`（`stream` 子命令额外需要 `pip install websockets`）。

## 安全提示

- token 首次自动生成，足够随机（32 字节 urlsafe）
- 默认仅本机访问（后端监听 127.0.0.1）；如需远端访问请自行处理端口转发与防火墙
- 重置 token：`POST /api/ext/external_control/token/reset`（需旧 token）
- 拓展停用后所有路由返回 503