---
name: agentcluster-control
label: 智能体集群控制
description: 通过 HTTP/WebSocket 控制 AgentCluster 智能体集群程序——创建/管理智能体、组建团队画布、发起对话、实时获取智能体输出。当用户说"在 AgentCluster 里建一个团队""创建智能体""让智能体集群做X""组建一群智能体""控制 AgentCluster""agentcluster 建团队""让 AgentCluster 研究/分析/写代码""查一下 AgentCluster 的会话/智能体""external control""外部AI控制"或提到 "AgentCluster""智能体集群""智能体团队"时加载此技能。
version: "1.0.0"
author: AgentCluster
tags: [agent, cluster, external, control, api, automation, orchestration, websocket]

dependencies:
  pip:
    - "requests>=2.28.0"
  external:
    - name: "AgentCluster 智能体集群程序"
      url: "http://127.0.0.1:8870"
      description: "必装。需在「功能拓展」管理页启用「外部AI控制」拓展，并从 AppData/extensions/external_control/token.txt 获取 token"
---

# 智能体集群控制

## 何时使用

当**外部 AI 程序**（如 DeepSeek Harness、Codex、Claude Code）需要操作本机运行的 **AgentCluster 智能体集群程序**时使用此技能，包括：

- 创建/更新/删除智能体（agents）
- 创建智能体分组（groups）
- 组建团队画布（canvases，即"智能体团队"）
- 创建会话并发消息给智能体（conversations）
- 实时流式获取智能体输出的内容
- 查询系统状态、运行中的会话

**不要用在**：
- 控制其他程序（本技能只对接 AgentCluster 的 `/api/ext/external_control/` 接口）
- 直接读写 AgentCluster 的本地数据文件（一律走 HTTP 接口）

## 前置条件

按顺序确认（缺任一则无法控制）：

1. **程序已启动**：AgentCluster 后端监听 `http://127.0.0.1:8870`（可由环境变量 `PORT` 覆盖）
2. **拓展已启用**：在「功能拓展」管理页安装并启用「外部AI控制」拓展
3. **拿到 token**：
   - 直接读取文件 `AppData/extensions/external_control/token.txt`
   - 或首次调用 `GET /api/ext/external_control/token`（文件不存在时直接生成返回）
4. **token 传入方式**（三选一）：
   - 请求头 `Authorization: Bearer <token>`
   - body 字段 `{"token": "<token>", ...}`
   - query 参数 `?token=<token>`

## 接口总览

所有路由挂载在 `/api/ext/external_control/` 前缀下：

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET  | `/info` | 服务信息 + 命令清单 | 无 |
| GET  | `/token` | 查看 token（首次自动生成） | 首次免鉴权 |
| POST | `/token/reset` | 重置 token | 需 token |
| POST | `/command` | **统一命令入口** | 需 token |
| WS   | `/ws/events/{cid}` | WebSocket 实时事件流 | `?token=` |
| GET  | `/events/{cid}` | SSE 事件流（备用） | `?token=` |

## 统一命令入口（核心）

`POST /api/ext/external_control/command`

请求体：

```json
{"cmd": "agents.create", "args": {"name": "研究员"}, "token": "<token>"}
```

成功返回：

```json
{"ok": true, "cmd": "agents.create", "data": {"id": 12, "name": "研究员", ...}}
```

失败返回：

```json
{"ok": false, "cmd": "agents.create", "error": "HTTPException: 缺少必填字段"}
```

> 未知命令返回 HTTP 400；token 缺失 401、无效 403；拓展停用 503。

## 可用命令

| 命令 | args | 说明 |
|------|------|------|
| `system.info` | — | 系统信息（智能体/画布/分组数 + 运行中会话） |
| `agents.list` | — | 列出所有智能体 |
| `agents.create` | `{name, description?, system_prompt?, model?, interface_id?, reasoning_level?, commands?, skills?, plugins?, group_id?, ...}` | 创建智能体 |
| `agents.update` | `{id, ...fields}` | 更新智能体 |
| `agents.delete` | `{id}` | 删除智能体（同时清理画布节点/连线） |
| `groups.list` | — | 列出分组 |
| `groups.create` | `{name}` | 创建分组 |
| `groups.delete` | `{id}` | 删除分组（组内智能体移回未分组） |
| `canvases.list` | — | 列出画布（团队） |
| `canvases.create` | `{name, description?, nodes?, edges?, workdir?, execution_mode?}` | 创建画布 |
| `canvases.get` | `{id}` | 画布详情 |
| `canvases.delete` | `{id}` | 删除画布 |
| `conversations.list` | — | 列出会话（剥离 messages，仅元数据 + 摘要） |
| `conversations.create` | `{name?, agent_ids?, canvas_id?, workdir?, execution_mode?}` | 创建会话 |
| `conversations.get` | `{id}` | 会话完整消息历史 |
| `conversations.delete` | `{id}` | 删除会话 |
| `conversations.send` | `{id, agent_id, content}` | **发消息，阻塞聚合返回完整回复** |
| `conversations.stop` | `{id, agent_id?}` | 停止运行 |
| `conversations.running` | `{id}` | 查询运行状态 |

### 画布节点与连线的数据结构

```json
{
  "name": "研究团队",
  "execution_mode": "serial",
  "nodes": [
    {"id": 1, "agent_id": 10, "x": 100, "y": 100},
    {"id": 2, "agent_id": 11, "x": 320, "y": 100}
  ],
  "edges": [
    {"id": 1, "from_node": 1, "to_node": 2, "type": "handoff"}
  ]
}
```

- `node.id` 为画布内节点 id（非 agent_id），`edges` 的 `from_node`/`to_node` 引用节点 id
- `edge.type`：`chat`（默认，对话消息）或 `handoff`（任务移交）
- `execution_mode`：`serial`（串行）或 `parallel`（并行）

## 典型工作流：创建智能体团队并让它干活

### 第 1 步：创建智能体

```bash
curl -X POST http://127.0.0.1:8870/api/ext/external_control/command \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"cmd":"agents.create","args":{"name":"架构师","system_prompt":"你负责系统架构设计"}}'
```

记下返回的 `data.id`（后面组建画布、发消息都要用）。

### 第 2 步：组建团队画布

把上面创建的智能体作为节点放进画布，用 `handoff` 边定义协作顺序：

```bash
curl -X POST http://127.0.0.1:8870/api/ext/external_control/command \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"cmd":"canvases.create","args":{"name":"开发团队","execution_mode":"serial","nodes":[{"id":1,"agent_id":10,"x":100,"y":100},{"id":2,"agent_id":11,"x":320,"y":100}],"edges":[{"id":1,"from_node":1,"to_node":2,"type":"handoff"}]}}'
```

### 第 3 步：创建会话并发消息

```bash
# 创建会话（绑定画布则进入集群上下文，不绑则是纯对话）
curl -X POST http://127.0.0.1:8870/api/ext/external_control/command \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"cmd":"conversations.create","args":{"name":"开发任务","canvas_id":1010}}'

# 发消息（阻塞拿到完整回复）
curl -X POST http://127.0.0.1:8870/api/ext/external_control/command \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"cmd":"conversations.send","args":{"id":5,"agent_id":10,"content":"设计一个聊天系统"}}'
```

`conversations.send` 返回：

```json
{
  "ok": true, "conv_id": 5, "agent_id": 10,
  "text": "智能体的完整回复文本",
  "events": [{"seq": 1, "data": "DATA:..."}],
  "event_count": 42
}
```

### 第 4 步（可选）：实时流式获取输出

若需边生成边展示，先 `conversations.send`（或用 `conversations.running` 确认运行中），
再用 WebSocket 订阅：

```
ws://127.0.0.1:8870/api/ext/external_control/ws/events/{cid}?token=<token>
```

每条消息为 JSON `{"seq": int, "data": "事件行"}`，结束发 `{"done": true}`。

## 配套脚本（推荐）

本技能目录提供轻量客户端 `scripts/ac_client.py`，封装 token 与 HTTP 调用：

```bash
python scripts/ac_client.py info
python scripts/ac_client.py cmd agents.create name=研究员 system_prompt=你负责研究
python scripts/ac_client.py cmd conversations.send id=5 agent_id=10 content=分析量子计算
python scripts/ac_client.py stream 5
```

- 环境变量 `AC_BASE`（默认 `http://127.0.0.1:8870`）、`AC_TOKEN`（默认自动读取 token.txt）
- `cmd` 子命令：`key=value` 形式传参，值会自动尝试转成 int/bool
- `stream` 子命令：WebSocket 实时打印智能体输出

## 注意事项 / 边界

- **鉴权**：所有 `/command` 调用必须带 token；不要把 token 泄露到公开日志
- **防重入**：同一会话同一智能体正在运行时，`conversations.send` 返回 409
- **运行解耦**：`conversations.send` 是阻塞式（聚合到 DONE）；要中断用 `conversations.stop`
- **可视化协同**：智能体输出若含 ` ```viz:type ` 代码块，会由 AgentCluster 的「可视化交互」拓展渲染；外部 AI 只需原样透传文本即可
- **端口**：默认 `8870`，如程序自定义了 `PORT` 环境变量请相应调整
- **数据落库**：所有操作写入 AgentCluster 真实数据（AppData/），创建前建议先 `agents.list` / `canvases.list` 确认不重名