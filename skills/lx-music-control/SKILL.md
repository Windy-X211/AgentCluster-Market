---
name: lx-music-control
label: 洛雪音乐操控
description: 控制洛雪音乐桌面版（lx-music-desktop）播放/暂停/切歌/跳转/调音量/静音/收藏/查询状态/查歌词/搜索播放/播放歌单。当用户说"打开洛雪音乐""播放音乐""暂停""下一曲""上一曲""切歌""切下一首""调音量""静音""跳进""跳转进度""现在在放什么歌""当前播放""播放歌词""收藏这首歌""搜歌""播放这首歌""播放歌单"或提到"洛雪""lx-music""LX Music"时加载此技能。
version: "2.0.0"
author: AgentCluster
tags: [music, player, http, api, automation, lxmusic]

dependencies:
  external:
    - name: "洛雪音乐 lx-music-desktop"
      url: "https://github.com/lyswhut/lx-music-desktop/releases"
      description: "必装。需要 v2.7.0+ 并在「设置 → 开放 API 服务」开启 HTTP API（默认 http://127.0.0.1:23330）"
---

# 洛雪音乐操控

## 何时使用

当用户要求控制**洛雪音乐桌面版**（lx-music-desktop，v2.7.0+）时使用此技能，包括：

- 播放 / 暂停 / 恢复
- 下一曲 / 上一曲 / 切歌
- 跳转进度（跳转到指定秒）
- 调整音量 / 静音
- 收藏 / 取消收藏当前歌曲
- 查询当前播放状态（歌曲名、艺术家、进度、封面、歌词等）
- 获取当前歌曲 LRC 歌词 / 全部歌词类型
- 订阅播放状态变化（SSE）
- 搜索并播放指定歌曲
- 播放 / 打开指定歌单

**不要用在**：
- 控制其他音乐软件（网易云客户端、QQ 音乐客户端等）→ 需各自 API
- 在线音乐网站播放（网页版）→ 用 browser_control

## 前置条件

按顺序确认（缺任一则无法控制）：

1. **洛雪音乐已启动**：进程名 `lx-music`（Windows 应用 ID `cn.toside.music.desktop`）。未启动则先启动。
2. **开放 API 服务已开启**：洛雪音乐 v2.7.0+ 特性，用户需在 **设置 → 开放 API 服务** 开启，默认监听 `http://127.0.0.1:23330`。首次使用未开启时，引导用户手动开启；端口如被改，先询问用户实际端口。
3. **API 可访问**：探测 `http://127.0.0.1:23330/status`（GET），收到 JSON 即视为就绪；`ECONNREFUSED` 说明未开启或端口错。

> 下文所有接口默认端口 `23330`，替换成用户实际端口即可。

## 工作流程

### 第 1 步：确认意图与必要参数

- **播放 / 暂停 / 切歌 / 静音 / 收藏**：无参数，直接调用。
- **跳转进度**：需要目标秒数 `offset`。
- **调音量**：需要目标值 `volume`（1-100）。
- **播放歌单**：必须两项信息
  - `source`：`kw`(酷狗) / `kg`(酷我) / `tx`(QQ音乐) / `wy`(网易云) / `mg`(咪咕)
  - 歌单 `id` 或 `url`（二选一必传）
  - 用户没给就从链接提取（如 `https://music.163.com/#/playlist?id=xxxxx` → `id=xxxxx, source=wy`）
  - 都不明确就主动询问，不猜。
- **搜索并播放歌曲**：需要歌曲名（推荐同时给艺术家、专辑以匹配更准）；用 Scheme URL `music/searchPlay` 或 `music/search`。

### 第 2 步：执行控制

按操作类型选择通道：

- **开放 API（HTTP）**：状态查询、歌词、基础播放控制、音量、静音、收藏。
- **Scheme URL（`lxmusic://`）**：搜索、搜索并播放、歌单打开/播放，以及部分便捷播放控制。

Windows 下唤起协议：`cmd /c start "" "lxmusic://..."`。协议未关联时提示用户检查。

---

#### A. 开放 API 接口（`http://127.0.0.1:23330`，全部 GET）

##### A1. 查询类

| 接口 | 用途 | 备注 |
|------|------|------|
| `/status` | 当前播放状态 | 支持 `?filter=a,b,c` 精简返回字段 |
| `/lyric` | 当前歌曲 LRC 歌词 | UTF-8 文本 |
| `/lyric-all` | 所有类型歌词 | JSON：`lyric`/`tlyric`(翻译)/`rlyric`(罗马音)/`lxlyric`(Any Listen) |
| `/subscribe-player-status` | SSE 长连接订阅状态变化 | 支持 `filter`；用 `curl -N` 或 SSE 客户端 |

**`/status` 字段**（默认返回：`status,name,singer,albumName,lyricLineText,duration,progress,playbackRate`）：

- `status`：`playing` / `paused` / `error` / `stoped`（**注意 stoped 是官方拼写**）
- `name` 歌曲名 / `singer` 艺术家 / `albumName` 专辑名
- `duration` 总时长（秒）/ `progress` 播放进度（秒）/ `playbackRate` 播放速率
- `picUrl` 封面 URL（HTTP 或 data URL）
- `lyricLineText` 当前歌词行 / `lyricLineAllText` 当前歌词+翻译/罗马音
- `lyric` / `tlyric` / `rlyric` / `lxlyric`：完整 LRC 文本（需显式 `filter` 加入）
- `collect` 是否已收藏 / `volume` 音量 / `mute` 是否静音

示例（精简返回）：`GET /status?filter=name,singer,status,progress`

##### A2. 播放器控制类

| 操作 | 接口 | 参数 | 示例 |
|------|------|------|------|
| 播放 | `/play` | 无 | `GET /play` |
| 暂停 | `/pause` | 无 | `GET /pause` |
| 下一曲 | `/skip-next` | 无 | `GET /skip-next` |
| 上一曲 | `/skip-prev` | 无 | `GET /skip-prev` |
| 跳转进度 | `/seek` | **`offset`**（秒，必填） | `GET /seek?offset=120` |
| 调音量 | `/volume` | **`volume`（1-100，必填）** | `GET /volume?volume=50` |
| 静音开关 | `/mute` | **`mute`**（`true`/`false`） | `GET /mute?mute=true` |
| 收藏当前曲 | `/collect` | 无 | `GET /collect` |
| 取消收藏 | `/uncollect` | 无 | `GET /uncollect` |

> 常见错误：`/seek` 传 `time=` 会不生效；`/volume` 传 0 也可能被拒绝（范围 1-100）。

---

#### B. Scheme URL（`lxmusic://...`）

两种传参方式：
- **data 方式**：复杂参数用 URL 编码后的 JSON，`lxmusic://<action>?data=<encoded-json>`
- **URL 方式**（v1.18.0+）：简单参数直接拼路径

平台 source 可用值：`kw` / `kg` / `tx` / `wy` / `mg`
音质可用值：`128k` / `320k` / `flac` / `flac24bit`

##### B1. 歌单类

| 描述 | URL | 参数 |
|------|-----|------|
| 打开歌单 | `songlist/open` | `source` 必传；`id` 或 `url` 二选一必传 |
| 播放歌单 | `songlist/play` | 同上 + `index`（从第几首开始，0 起，选传） |

data 方式示例：
```
lxmusic://songlist/open?data=%7B%22source%22:%22kw%22,%22id%22:%223373919903%22%7D
lxmusic://songlist/play?data={"source":"wy","id":"123456","index":0}   # 记得 URL 编码 data
```

URL 方式示例：
```
lxmusic://songlist/open/kw/3373919903
lxmusic://songlist/play/wy/123456
```

##### B2. 搜索与搜索并播放

| 描述 | URL | 参数 |
|------|-----|------|
| 搜索歌曲（不播放） | `music/search` | `keywords` 必传；`source` 选传 |
| 搜索并播放 | `music/searchPlay` | `name` 必传；`singer`/`albumName`/`interval`(xx:xx)/`playLater`(bool) 选传 |

示例：
```
lxmusic://music/search?data={"keywords":"突然的自我"}
lxmusic://music/searchPlay?data={"name":"突然的自我","singer":"伍佰","playLater":false}
lxmusic://music/search/kw/%E7%AA%81%E7%84%B6%E7%9A%84%E8%87%AA%E6%88%91
lxmusic://music/searchPlay/%E7%AA%81%E7%84%B6%E7%9A%84%E8%87%AA%E6%88%91-%E4%BC%8D%E4%BD%B0
```

##### B3. 播放歌曲（需要完整元信息，一般用 searchPlay 替代）

`music/play` 参数：`name`、`singer`、`source`、`songmid` 必传，其余可选，见官方文档。通常不直接用，除非已有完整元信息。

##### B4. 便捷播放控制（Scheme URL，URL 方式，无参数）

| 描述 | URL |
|------|-----|
| 播放 | `lxmusic://player/play` |
| 暂停 | `lxmusic://player/pause` |
| 切换播放/暂停 | `lxmusic://player/togglePlay` |
| 下一曲 | `lxmusic://player/skipNext` |
| 上一曲 | `lxmusic://player/skipPrev` |
| 收藏当前曲 | `lxmusic://player/collect` |
| 取消收藏 | `lxmusic://player/uncollect` |
| 不喜欢当前曲 | `lxmusic://player/dislike` |

Scheme URL 方式在洛雪音乐未开 API 但已关联协议时也能用；两种通道结果等价，优先用 HTTP API（可拿到返回结果便于确认）。

---

### 第 3 步：查询并汇报状态

控制动作完成后调 `GET /status?filter=name,singer,albumName,status,progress,duration,picUrl,lyricLineText,collect,volume,mute` 拉最新状态，向用户汇报。

---

## 输出格式

### 状态汇报表

```
🎵 当前播放信息

| 项目 | 信息 |
|------|------|
| 歌曲名 | 小半 |
| 艺术家 | 陈粒 |
| 专辑 | 小梦大半 |
| 状态 | ⏸️ 暂停 |
| 时长 | 4分57秒 |
| 进度 | 3分54秒（约 79%） |
| 音量 | 60%（未静音） |
| 收藏 | ✅ 已收藏 |
| 当前歌词 | 宠溺的 厌倦的 |
```

- `status` → 图标：`playing` ▶️ 播放中 / `paused` ⏸️ 暂停 / `error` ⚠️ 出错 / `stoped` ⏹️ 停止
- 时间格式：`X分Y秒`；进度百分比 = progress/duration
- 若封面 URL 有效且用户想看到图，可附一行"封面：`<picUrl>`"

### 控制反馈

成功执行动作后**一句话确认 + 状态表**：
- 「✅ 已切到下一曲《江湖之间》并开始播放」
- 「✅ 音量已调整到 50%」
- 「✅ 已收藏《小半》」
- 「✅ 已跳转到 2:00」

---

## 典型工作流示例

1. **"播放这首歌"**：
   - 若用户给了歌名：`lxmusic://music/searchPlay?data=<encoded>{"name":"xxx","singer":"yyy"}</...>` → 等 1-2 秒 → `GET /status` 汇报
   - 若用户在指当前歌单曲库：直接 `/play` 恢复播放

2. **"把音量调到 30"**：`GET /volume?volume=30` → 简短确认，不必拉全状态。

3. **"现在放什么？"**：`GET /status`（可用 filter 精简）→ 状态表。

4. **"给我这首歌的歌词"**：`GET /lyric`（LRC 原文）或 `GET /lyric-all`（含翻译/罗马音）。若只关心"当前这一行"用 `/status?filter=lyricLineText`。

5. **"播放这个歌单"**（用户给了网易云链接）：解析 `id` 与 `source=wy` → 用 data 方式拼 `songlist/play` → 唤起协议 → `/status` 汇报。

6. **"跳到 1 分 30 秒"**：`GET /seek?offset=90` → 确认。

7. **"静音"** / **"取消静音"**：`GET /mute?mute=true` / `GET /mute?mute=false`。

8. **"实时监控播放变化"**：用 SSE `GET /subscribe-player-status?filter=status,name,lyricLineText`，客户端以 `curl -N` 或 SSE 库订阅，事件形如：
   ```
   event: name
   data: "交换余生"
   ```

---

## 安全与边界

- **仅访问本地**：HTTP 请求只允许 `127.0.0.1` / `localhost`，绝不访问公网。
- **不修改配置文件**：不直接改洛雪 settings.json；开放 API 的开启由用户在设置界面完成。
- **不写入敏感信息**：不记录账号、Cookie、Token。
- **歌单来源与 ID 必须由用户提供**：不猜测；用户说不清时列出 `kw/kg/tx/wy/mg` 让其选。
- **音量适度**：除非用户明说要大音量，默认不超 80；跳进 offset 不超过 duration。
- **协议调用谨慎**：`start lxmusic://...` 是唤起系统协议，避免高频循环调用；单次一个动作即可。
- **失败处理**：`ECONNREFUSED` / 超时 → 提示用户检查洛雪音乐是否启动、开放 API 是否开启；不要反复重试轰炸（最多 1-2 次）。

---

## 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| 连接被拒绝 (ECONNREFUSED) | 开放 API 未开启 / 端口不对 | 引导用户在「设置 → 开放 API 服务」开启，默认 23330 |
| 请求超时 | 洛雪音乐未启动 / 假死 | 检查进程，必要时重启 |
| 403 Forbidden | 该接口被禁用 | 改用 Scheme URL，或告知用户该功能受限 |
| `/seek` 无效果 | 参数名错（用了 `time` 而非 `offset`） | 改 `?offset=<秒>` |
| `/volume` 报错或无效 | 超出 1-100 | 用合法范围 |
| 协议无法唤起 | `lxmusic://` 未关联 | 让用户在洛雪设置中确认协议关联，或改用 HTTP API |
| 搜索并播放没反应 | 参数未 URL 编码 / `name` 缺失 | 检查 `data=` 后是编码后的 JSON，且 `name` 必传 |
| status 里 `stoped` 看着像拼错 | 官方 API 就是这个拼写 | 别改，识别为"停止"状态 |

---

## 交付后

每次操控完成后主动一句：
- "还要继续操作吗？（下一曲 / 切歌 / 调音量 / 换歌单 / 看歌词 都可以直接说）"