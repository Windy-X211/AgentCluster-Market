---
name: web-image-search-download
description: 在图片站点（Unsplash / Pexels / Pixabay 等）按关键词搜索图片，自动筛掉付费与不合规结果，挑选最能体现主题的高清图，下载到工作区并做内容验证与来源登记，最后回收隐藏浏览器窗口。触发条件：用户要求"找张/找几张 XX 的照片/图片/素材"、"去 unsplash 搜 XX"、"下载配图/海报底图/游戏素材图"，或需要为文档/游戏/PPT 补充可商用图片素材。
---

# web-image-search-download Skill

把"帮我去图片站找张图"从一次性手工操作，沉淀为**可复用、可追溯、可商用**的标准流程。

> **强制约束（P0）**：执行过程中 Agent **不要反复询问用户"要不要继续"**。凡是能从页面/页面结构化快照推断出的信息，自行判断；只有"选哪一张作为主图"这类审美取向问题才交给用户拍板（且给出已下载好的候选，而非空口询问）。

---

## 0. 关键教训（先读这一段，能省掉 80% 的坑）

| 坑 | 现象 | 对策 |
|---|---|---|
| **反爬等待页** | 打开 `unsplash.com/...` 落到 `unsplash.com/.within.website?redir=...`，标题"正在检查您的浏览器"，截图全白 | **这不是失败，是异步校验**。`wait` 6~10 秒再截图/取快照即可自动放行。**不要**立刻重试或换 UA |
| **`evaluate` 返回 null** | 用 JS 抠 DOM 拿图链，结果恒为 `null`（页面有 CSP / 沙箱限制） | **放弃 JS 抠取**，改走"结构化快照 + 单元素 HTML 读取"路线（见步骤 3） |
| **`plus.unsplash.com` 是付费** | `premium_photo-*` 开头的图看着能下，实则 Unsplash+ 订阅内容 | 图源域名白名单只认 `images.unsplash.com`；`plus.unsplash.com` 一律跳过 |
| **拿到的 URL 带 `&amp;`** | 页面 HTML 里的 `&` 被转义，直接下载会 400/403 | 下载前把 `&amp;` 还原成 `&`，并把 `w=3000` 换成目标尺寸 |
| **不看图就交付** | 图片名字叫 flag，内容可能是错图/水印图/低质图 | 下载后**必须** `file_read` 逐张视觉确认（步骤 5） |
| **忘记隐藏浏览器** | 抢焦点、用户桌面被打断 | 流程收尾强制执行"隐藏浏览器"（步骤 7） |

---

## 1. 明确检索意图

从用户原话提取三个要素，缺的用默认值补齐，**不要反问**：

| 要素 | 示例 | 默认值 |
|---|---|---|
| `keyword` | 五星红旗 → `chinese flag` | 必填，需翻成站点惯用英文 |
| `count` | 3 张 | 3 |
| `usage` | 游戏海报底图 / UI 图标 / 文档配图 | `通用素材` |

> **中→英关键词映射**：图片站以英文索引为主。中文诉求先落到英文检索词，例如
> 五星红旗→`chinese flag`、长城→`great wall of china`、水墨→`chinese ink painting`、中秋→`mid autumn festival`。
> 拿不准时用"主体 + 场景"两段式（`chinese flag waving`、`chinese flag sky`）扩召回。

---

## 2. 打开搜索页

1. 新开标签页导航到站点搜索页：
   - Unsplash：`https://unsplash.com/s/photos/<keyword-with-dashes>`
   - Pexels：`https://www.pexels.com/search/<keyword>/`
   - Pixabay：`https://pixabay.com/images/search/<keyword>/`
2. **必做**：`wait` 8 秒 → `screenshot` 确认。若仍是校验页，再 `wait` 6 秒。最多重试 2 轮，仍失败才换站点。

---

## 3. 取回结果清单（核心手法）

用 `get_html` 的 **snapshot** 格式拿到全页结构化元素树（`@eN` 编号），从中解析：

- `figure` 下的 `a[href*="/photos/"]` → 详情页链接（记录，用于来源登记）
- `figure` 下的 `img` → 缩略图元素 `@eN`
- `img` 的 `alt` 文本 → 判断图片内容的重要线索（多数场景 alt 已足够筛图）

然后对**每个候选**单独调 `get_html` 的 **element** 格式、`selector=@eN`，从 `<img>` 标签里取三样东西：

| 字段 | 位置 | 用途 |
|---|---|---|
| `src` | 属性 | 下载直链（形如 `https://images.unsplash.com/photo-xxxx?fm=jpg&q=60&w=3000...`） |
| `width` / `height` | 属性 | 原图分辨率，用于判断是否高清 |
| `alt` | 属性 | 内容描述 |

> **解析要点**
> - `srcset` 里是一堆带 `w=` 的尺寸变体，**不要**用；只取 `src`，再把 `w=3000` 改写成目标宽（如 `w=2400`）、`q=60` 改成 `q=85`。
> - 域名含 `plus.unsplash.com` → **丢弃**（付费）。
> - 同一 `photo-` id 只保留一次，去重。

---

## 4. 选图策略

按 `usage` 决定优先级，每类挑 1 张，凑到 `count` 张为止：

| 用途 | 优先特征 |
|---|---|
| 海报 / 氛围图 | 大场景、留白足、有景深或剪影、横构图 |
| UI / 图标 / 背景纹理 | 主体特写、纹理清晰、可裁成方形 |
| 文档配图 / 场景图 | 主体明确、环境信息完整、无路人脸特写 |
| 游戏素材 | 分辨率 ≥ 3000px、无文字水印、主体居中度高 |

同时记下**被淘汰的候选及原因**（付费 / 分辨率低 / 主体不符 / 疑似 AI 生成痕迹），写进交付清单——用户下次想换图能直接看这份记录。

---

## 5. 下载 + 验证

1. 建目录（默认 `./flags`、`./assets/images` 或按主题命名），用 PowerShell 批量下载：

```powershell
New-Item -ItemType Directory -Force -Path ".\assets\images" | Out-Null
$imgs = @{
  "chinese-flag-closeup.jpg" = "https://images.unsplash.com/photo-xxxx?fm=jpg&q=85&w=2400&fit=max"
}
foreach ($k in $imgs.Keys) {
  try {
    Invoke-WebRequest -Uri $imgs[$k] -OutFile ".\assets\images\$k" -TimeoutSec 60 -UserAgent "Mozilla/5.0"
    Write-Output "OK $k"
  } catch { Write-Output "FAIL $k : $_" }
}
Get-ChildItem .\assets\images | Select-Object Name, Length
```

2. **逐张 `file_read` 视觉确认**：是不是目标主体？有没有水印/叠加字？构图能否直接用？
3. 文件体积落在 200KB ~ 2MB 视为正常；`< 20KB` 基本是失败页（多半是 403 HTML），需重新下载。

---

## 6. 交付清单（必须给全）

输出四块内容，缺一不可：

1. **候选图预览**：每个候选给一个 `viz:image` 块，`url` 用相对路径（`./assets/images/xxx.jpg`），`caption` 写"内容 + 原图分辨率"。
2. **来源与授权表**：文件名 → 详情页链接 → 授权类型。Unsplash License / Pexels License / Pixabay License 均可免费商用；**明确标注哪些是付费图未下载**。
3. **参数说明**：下载尺寸（最长边 / 质量）、是否已裁切。
4. **让用户拍板**：给一个 `viz:choice` 多选块，选项为"选项N：特征（适用场景）"。

> 若用户后续要指定尺寸（1920×1080、竖版 1080×1920、方形 1024），直接改 URL 的 `w`/`h`/`fit` 参数重下，不要重新搜图。

---

## 7. 收尾：隐藏浏览器（P0，不可跳过）

任务完成、图片落盘后，**必须**把自动化浏览器窗口藏起来，避免打断用户桌面：

```powershell
Add-Type -Namespace Win -Name Api -MemberDefinition '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
$names = @('msedge','chrome','firefox','brave','opera','360se','360ChromeX','QQBrowser','SogouExplorer')
foreach ($n in $names) {
  Get-Process -Name $n -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    ForEach-Object { [Win.Api]::ShowWindow($_.MainWindowHandle, 0) }   # 0 = SW_HIDE
}
```

- `ShowWindow(handle, 0)` 即 `SW_HIDE`，进程不死、会话保留，随时可 `1`（SW_SHOWNORMAL）唤回。
- 若返回 `NO_VISIBLE_BROWSER_WINDOW`，说明浏览器已在后台/无头运行，**不要**报错，如实说明即可。
- 隐藏动作放在**最后一次文件操作之后**，确保下载不被中断。

---

## 8. 边界与合规

- 只下载**明确标注免费商用**的图（Unsplash License / Pexels License / Pixabay License）。
- 遇到 Unsplash+ / iStock / Getty / Shutterstock 等付费水印图：**不下载**，但要在清单里点名，并给出免费替代建议。
- 不要绕过登录墙、不要抓取需要授权的高清原图 API。
- 国旗、货币、人脸、商标等敏感主体：交付时提示使用场景的合规风险。

---

## 9. 复用检查表

执行完逐条打勾，任一条不满足则视为未完成：

- [ ] 关键词已翻成站点惯用英文
- [ ] 反爬校验页已 `wait` 放行，截图确认非白屏
- [ ] 用 snapshot + element 取到 `src`/`width`/`height`/`alt`（未依赖 `evaluate`）
- [ ] `plus.unsplash.com` 候选已剔除
- [ ] URL 中 `&amp;` 已还原、尺寸参数已改写
- [ ] 每张图 `file_read` 视觉验证通过
- [ ] 交付含"预览 + 来源授权表 + 参数 + 选择块"四要素
- [ ] 浏览器窗口已 `SW_HIDE`
