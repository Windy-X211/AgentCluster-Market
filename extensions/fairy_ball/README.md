# ⚡ 精灵悬浮球（fairy_ball）

桌面宝可梦精灵悬浮球拓展：一只宝可梦小精灵常驻屏幕角落，随智能体运行状态活动。

## 功能

| 交互 | 行为 |
|------|------|
| **左键点击** | 打开主窗口（已打开则置顶），精灵头顶冒出属性特色 emoji |
| **按住拖动** | 把精灵拖到屏幕任意位置 |
| **右键菜单** | 区域截图提问 / 临时对话 / 打开设置 / 精灵样式 / 悬浮球大小 |

## 个性化（v1.4.0 · 宝可梦改版）

右键菜单 →「精灵样式 ▸」/「悬浮球大小 ▸」二级视图内切换：

- **10 只特色宝可梦**（宝可梦 Showdown 官方像素动图，`assets/` 本地内置经 `/sprite/` 端点下发；
  `image-rendering: pixelated` 复古像素风放大，GIF 自带呼吸/摇摆动画）：

  | 宝可梦 | 属性主色光晕 | 点击特效 |
  |------|------|------|
  | 皮卡丘 | 电 · 金黄 | ⚡ |
  | 伊布 | 一般 · 暖棕 | 🌟 |
  | 妙蛙种子 | 草 · 翠绿 | 🌱 |
  | 小火龙 | 火 · 橙红 | 🔥 |
  | 杰尼龟 | 水 · 天蓝 | 💧 |
  | 耿鬼 | 幽灵 · 紫 | 👻 |
  | 快龙 | 龙 · 橙金 | 🐉 |
  | 路卡利欧 | 格斗 · 蓝金 | 🥋 |
  | 水君 | 水 · 蓝紫 | 💠 |
  | 谜拟Q | 幽灵·妖精 · 暗黄 | 🎭 |

- **3 档大小**：小（0.72×）/ 中（1×·默认）/ 大（1.28×），底部锚点缩放（`wrap` 缩放层与精灵动画层分离互不干扰）
- 选择写入球窗 localStorage（`fairy.skin` / `fairy.size`），重启保留；旧版 CSS 精灵皮肤 id 自动清理回落皮卡丘
- 鼠标穿透命中半径随缩放档位实时重算（`getBoundingClientRect` 已含缩放）
- 菜单窗精灵列表以 26px 小图预览（同 `/sprite/` 端点，选中即换装）

## 精灵状态（每 3s 轮询 `GET /api/ext/fairy_ball/state`）

| 状态 | 动画 |
|------|------|
| 空闲（无运行） | GIF 原生呼吸/摇摆 + 缓慢漂浮 |
| 运行中 | 欢快弹跳 + 属性主色双层光晕，顶部显示「N 个智能体运行中」 |
| 后端异常 | 变灰、慢速漂浮、微微歪头 |

## 截图提问流程

1. 右键菜单 →「截图提问」→ 全屏变暗进入框选（精灵自动隐藏避免入镜）
2. 拖选区域 → 松手后选择去向：**主窗提问**（主窗口新建临时会话并预填截图，输入问题后发送）
   或 **小窗提问**（弹出独立小对话窗，截图已带好，直接输入问题）
3. ESC / 右键 取消

## 临时对话

独立小对话窗（自绘标题栏 + 智能体下拉 + 流式回复），不依赖主窗口。
会话记录仍走后端落库；**未发送任何消息就关窗的空会话会被自动清理**，发过消息的可在主窗口会话列表回看。

## 架构

```
Electron main.cjs（壳：透明置顶小窗 / 选区窗 / 小对话窗 / desktopCapturer 截图）
        │  加载
        ▼
GET /api/ext/fairy_ball/page|snip|quickchat   ← backend.py 下发 HTML（按 mtime 缓存，改外观无需重启）
GET /api/ext/fairy_ball/sprite/{name}.gif     ← 宝可梦精灵图（assets/ 内置，白名单校验防目录穿越）
GET /api/ext/fairy_ball/state                 ← 聚合 run_registry 全局运行态
GET /api/ext/fairy_ball/enabled               ← Electron 轮询决定球窗开关（停用拓展球自动消失）
POST /api/ext/fairy_ball/quickchat/cleanup    ← 小窗关窗清理空会话
```

- manifest `type: hybrid` + `backend_entry: backend`，启动时由
  `api.extensions.mount_extension_backends` 动态挂载到 `/api/ext/fairy_ball/` 前缀
- 页面文件 `fairy.html` / `menu.html` / `snip.html` 的 JS/CSS 内联自包含，
  宝可梦 GIF 由 `/sprite/` 端点按需下发（FileResponse 协商缓存）→
  **修改动画/样式/精灵图无需重建前端 dist；改页面无需重启后端**（mtime 缓存，
  改 backend.py 路由代码才需重启后端）
- 截图保存于系统临时目录 `~/AppData/Local/Temp/agentcluster_screenshots/`
  （已在 `/api/fs/image` 白名单内，智能体与消息气泡均可读取）
- 注意：区域截图仅支持主显示器（DPI 缩放已按 scaleFactor 换算）

## 已知边界

- 串行接力运行中，球只感知「有智能体在跑」（全局粒度），不区分会话
- Web 模式（--tray-only）无 Electron 窗口环境，球不出现