# MCP 服务端（mcp_server）拓展

把 AgentCluster 的能力以 **MCP（Model Context Protocol）** 暴露给外部 AI 客户端。

## 能力

| 工具 | 说明 |
|------|------|
| `system_info` | 系统信息（智能体/画布/分组数量、运行中会话） |
| `list_capabilities` | 可用命令 / 技能 / 插件清单 |
| `list_agents` / `get_agent` | 查看智能体 |
| `list_groups` | 查看智能体分组 |
| `list_canvases` / `get_canvas` | 查看画布（团队） |
| `list_conversations` / `get_conversation` | 查看会话（列表剥离消息正文） |
| `create_conversation` | 新建会话（可绑定画布） |
| `send_message` | 向智能体发消息并阻塞至运行结束，返回最终文本与事件 |
| `stop_agent` | 停止运行 |
| `list_running` | 查询运行状态 |
| `create_agent` / `update_agent` / `delete_agent` | 写操作，默认关闭，需在配置页开启 `allow_write` |

## 传输方式

1. **Streamable HTTP**：`http://127.0.0.1:8870/api/ext/mcp_server/mcp`
   请求头 `Authorization: Bearer <token>`（token 在配置页查看，可重置）。
2. **stdio**：外部客户端以子进程方式启动 `mcp_stdio.py`，无需 token。
   ```json
   {
     "mcpServers": {
       "agentcluster": {
         "command": "python",
         "args": ["<安装目录>/AppData/extensions/mcp_server/mcp_stdio.py"]
       }
     }
   }
   ```

配置页（拓展管理 → MCP 服务端 → 配置）会直接给出可复制的 JSON 片段。