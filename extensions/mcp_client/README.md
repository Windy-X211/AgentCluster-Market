# MCP 客户端（mcp_client）拓展

连接外部 **MCP（Model Context Protocol）服务器**，把对方提供的工具注入本程序智能体的
function calling 列表。

## 工作原理

1. 在「功能拓展 → MCP 客户端 → 配置」中添加 MCP 服务器（stdio / Streamable HTTP / SSE）。
2. 拓展后端在专用事件循环线程里维持长连接，连接成功后拉取工具清单并缓存。
3. 缓存的工具以 `mcp__<服务器>__<工具名>` 命名注入所有智能体（可在服务器配置里用
   `agents` 字段限定智能体 id，`all` 表示全部）。
4. 智能体调用这些工具时，拓展把参数转发给外部 MCP 服务器并返回结果。

## 服务器配置字段

| 字段 | 说明 |
|------|------|
| `id` | 唯一标识（留空自动生成） |
| `name` | 显示名 |
| `enabled` | 是否启用 |
| `transport` | `stdio` / `http` / `sse` |
| `command` / `args` / `cwd` / `env` | stdio 传输参数 |
| `url` / `headers` | http / sse 传输参数 |
| `timeout` | 单次调用超时秒数（默认 120） |
| `agents` | `all` 或智能体 id 数组，限定工具可见范围 |

配置保存在本目录 `servers.json`，修改后点击「保存并重连」即可生效，无需重启后端。

## 快捷导入（推荐）

配置页「📋 粘贴 JSON 配置」可直接粘贴标准 `mcpServers` 片段（Claude Desktop / Cursor / Codex / DeepSeek Harness 同款格式），解析后自动识别传输方式并保存：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:\\work"]
    },
    "agentcluster": {
      "type": "http",
      "url": "http://127.0.0.1:8870/api/ext/mcp_server/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

规则：含 `command` → stdio；含 `url` → Streamable HTTP（`type: "sse"` 或 URL 含 `/sse` → SSE）；`headers` / `env` / `timeout` / `agents` 原样保留。也支持粘贴单个服务器对象或数组。

## 示例

```json
{
  "options": { "max_tools": 48, "call_timeout": 120 },
  "servers": [
    {
      "id": "filesystem",
      "name": "本地文件系统",
      "enabled": true,
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:\\work"],
      "timeout": 120,
      "agents": "all"
    },
    {
      "id": "agentcluster",
      "name": "本程序 MCP 服务端",
      "enabled": true,
      "transport": "http",
      "url": "http://127.0.0.1:8870/api/ext/mcp_server/mcp",
      "headers": { "Authorization": "Bearer <token>" },
      "timeout": 120,
      "agents": "all"
    }
  ]
}
```

> 与「MCP 服务端」拓展配合可实现自举验证：客户端连自己的服务端，智能体就能
> 通过 MCP 工具操作本程序的智能体、画布与会话。