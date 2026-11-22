# 多模型同屏（v0.6.0 起）

一个模型 = 一个 `pet_window.py --worker` 子进程。父进程（插件进程）只保留一张注册表，
通过 `127.0.0.1` 的 JSON 控制通道按 `model_path` 把命令路由到对应 worker。
宿主进程退出时，这些 worker 会被自动收回（三层保险，见下文「宿主退出时自动收回」）。

## 为什么是多进程，而不是单进程多视图

QtWebEngine 的硬约束：一个进程只有一个 `QApplication`、只有一个 GUI 线程，所有
`QWidget` 必须在那个线程创建；而原来的 `_qapp_main` 末尾是常驻的 `app.exec_()`，
永不返回。所以「在同一个进程里再开第二个窗口」必须再起一个 GUI 线程 —— 这正是原代码
注释里明确禁止的（二次初始化会 access watchdog violation）。

多进程方案下，每个 worker 内部跑的就是原来那套已经验证过的单模型渲染链路，无需改动
渲染逻辑；而 `close` 直接结束进程，Chromium 渲染进程、WebGL 纹理、大贴图才能整份
还给系统 —— 单进程内销毁 QWebEngineView 做不到这一点（进程仍在，显存不一定回收）。

## 每个 worker 独立的东西

| 资源 | 隔离方式 |
| --- | --- |
| Chromium profile | `_webengine_profile/pet_<sha1(key)[:10]>/`，由 `PET_PROFILE_DIR` 传入；关缓存与持久 cookie |
| 模型 HTTP 服务 | worker 内 `serve_model()`，从 18765–18779 起向上找空闲端口 |
| 帧服务 | worker 内 18780–18800 向上顺延 |
| 控制通道 | 父进程向 OS 申请空闲端口（18700+ 随机），只监听 127.0.0.1 |
| 日志 | `_workers/pet_<sha1>.log`（stdout+stderr 合并，行首带 `[tag]`） |

注册表主键 = `model3.json 路径(normcase)#pid`，天然唯一；而「能不能被 model_path 找到」靠的是
每个实例身上的 `keys` 集合（它对应过的所有模型路径）。两者分开，才能既保证不漏条目、
又让右键换过模型的窗口仍可被定位。

## 命令语义

| 命令 | 行为 |
| --- | --- |
| `show(model_path)` | 没开就新建 worker 并等加载完成；已开则只显示 + 应用 x/y/scale |
| `show()` | 省略时开插件自带的符玄；已经有 1 个模型则作用于它；≥2 个返回歧义报错 |
`list_open_models` | 每个模型的位置/缩放/可见/就绪/帧率/进程号 + `adopted`（是否重启后找回的遗留窗口），顶层 count 与 max |
| `close(model_path)` | 只关这一个：`/shutdown` → terminate → kill，进程结束即释放内存 |
| `close()` | 关全部（兼容旧用法） |
| `close_all` | **明确的全关命令**：遍历注册表逐个销毁，返回 closed 明细 / count=0 |
| `switch(model_path, from_model_path)` | 关旧 + 原槽位建新，位置/缩放/跟随沿用；省略 from 时只在单实例成立 |
| 其余单模型动作 | `model_path` 定位；只有 1 个模型时可省略；0 个返回 `count=0`；≥2 个报错 + 候选 |

## 窗口里的右键「关闭 / 全部关闭」（v0.6.2）

右键菜单里现在是两项：**「关闭」= 关掉当前这个模型**，**「全部关闭」= 所有模型一起关掉**
（等价于命令 `close_all`）。两者都是真正结束进程，不是隐藏。但与命令行 `close` 不同：
菜单动作发生在**worker 进程内部**，而注册表在父进程里。所以链路是：

1. worker 把 `{"pid": 自己的 pid, "model": 当前模型}` POST 到父进程
   （`/worker_self_close` 或 `/worker_close_all`）；
2. 父进程按 pid 认出实例（不靠 model_path，避免用户右键换过模型导致键漂移），
   走 `_destroy` → `/shutdown` → 进程退出；「全部关闭」则遍历注册表逐个收走，
   发起请求的 worker 自己也在其中；
3. 父进程若不在（独立运行 / 插件已退出 / 回调超时），worker 直接 `os._exit(0)`
   把内存还给系统，注册表里的死条目由 `_prune_dead()` 在下一次操作时清掉。

配套健壮性：

- 实例身上的 `keys` 会吸收 worker 上报的**真实模型**：用户右键「切换模型」后，新旧路径
  都能定位到这个实例；两个窗口撞成同一个模型时，两条注册表记录都保留（不会再互相顶掉）。
- 每实例独立 profile 目录名仍按创建时的模型计算，右键换模型只是复用同一个 profile 目录。
- 父进程侧的回连服务（`/worker_self_close`）只在第一次拉起 worker 时启动，端口随进程存活。

## 为什么 `list_open_models` 会漏报，现在怎么保证不漏（v0.6.3）

实例注册表原本只活在父进程内存里，于是有两种漏报：

**（A）插件进程重启后，屏幕上的窗口变成孤儿。** worker 是子进程，父进程退出并不会带走它：
窗口还在动、内存还占着，但新进程的注册表是空的 → `list_open_models` 返回 `count=0`，
`close` 也关不掉它（现场抓到过：worker `pid=15384`，`--parent-port 49481` 已无监听）。

修法：worker 启动后（控制通道一开始应答）就把自己的身份写成
`_workers/state_<pid>.json`（`pid / control_port / parent_port / model / x / y / scale / ready`），
`/shutdown` 前删掉。父进程每次操作前扫描这批文件：

- 文件里的 `pid` 还活着 → 用 `control_port` 连上去拿 `/state`；连得上就**收养**进注册表
  （条目带 `adopted: true`），并用 `/set_parent_port` 把它右键关闭的回连目标改成本进程，
  于是遗留窗口的右键「关闭 / 全部关闭」立刻恢复有效；
- `pid` 已死 → 视为过期文件直接删掉；
- 不是本进程拉起、又不归注册表管的窗口，从此都能被 `list_open_models` 列出、被
  `move / resize / set_params / close` 正常操作，`close` 走「HTTP `/shutdown` → 等 2s →
  按 pid `TerminateProcess`」这条兜底路径。

**（B）注册表主键互相覆盖。** 旧版在 worker 上报模型变化时把主键换成新模型路径；若两个窗口
被切成同一个模型，`_instances[新键] = B` 会把 A 顶掉 —— 屏幕上两个窗口、列表只有一个。

修法：主键改成含 pid 的唯一键，永不变动；模型路径只进 `keys` 集合用于路由。查找走
`_find_insts(model_file)`（按 `keys` 匹配，可能返回多个），同模型的多个窗口一律照实列出。

代价与约定：同一个模型真的开了两个窗口时，`move(model_path=...)` 这类命令无法确定操作哪
一个，会返回「匹配到 N 个窗口」的报错；此时用 `close(model_path=...)`（会把该模型的所有窗口
一起关掉）或 `close_all`。

## 宿主退出时自动收回所有 worker（v0.6.4）

worker 是子进程，宿主（插件进程）退出并不会带走它们——不管的话屏幕上会留下看不见也管不了的
残余窗口。现在三层一起兜：

| 层 | 覆盖的场景 | 机制 |
| --- | --- | --- |
| `atexit` 钩子 | 宿主正常退出、抛出未捕获异常退出 | 逐个 POST `/shutdown`，再按 pid 兜底强杀，顺手删自述文件 |
| Windows Job Object | 宿主崩溃、`os._exit`、任务管理器「结束任务」强杀 | `CreateJobObject` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，worker 一创建就 `AssignProcessToJobObject`；宿主进程一消失，Job 最后一个句柄由内核关闭 → 子树连根终止 |
| worker 看门狗 | Job 没生效（被更外层的 Job 挡住 / 非 Windows / 手工起的 worker） | 主线程每 4s 探测一次父进程回连端口，**曾经连通过**之后连续 3 次不可达（约 12s）就自行 `os._exit(0)` 并删自述文件 |

两点设计取舍：

- 看门狗只在「曾经连通过」后才启用：父进程端口本来就不可达的场景（独立运行、看门狗测试）
  不会被误杀；真实链路上 worker 一定先连上父进程的回连服务。
- 挂了 Job 意味着**宿主一退出窗口就没了**，再次启动要重新 `show`。这与「重启后自动收养
  遗留窗口」并存：收养只在实际发生残留时（例如 Job 被外层挡住、worker 是手工起的）才用得上。

## 默认决策

- 同屏上限 **6** 个，超出返回 `同屏模型已达上限`，提示先 close。
- 新实例位置**级联错位**（0.0 / -0.14 / +0.14 / -0.28 / +0.28 / -0.42，相对 0.5）。
- **第 2 个起的实例不跟随鼠标**（`track=false`），避免多窗口互相追光标打架；
  第一个实例保持跟随。`switch` 沿用被替换实例的 track 设置。

## 多模型的置顶策略（v0.7.1）

多模型同时打开时，每个 worker 都有一对窗口：Electron 透明显示窗（画模型）+ Qt 透明覆盖层
（收点击/拖动/菜单）。两对窗口都在 topmost 带里，谁在带内更靠上，谁就盖住别人。

旧实现有两条周期性抢顶层的行为，导致模型互相盖、闪烁：

| 位置 | 旧行为 | 现在 |
| --- | --- | --- |
| `_set_hwnd_topmost()` | 无条件 `SetWindowPos(HWND_TOPMOST)`，顺手把自己抬到 topmost 带最上层，每 2s 一次 | 先查 `WS_EX_TOPMOST`，**已经置顶就不动**（只在别的程序把置顶属性清掉时补） |
| `_pin_qt_topmost()` | 把 Qt 覆盖层抬到带内最顶层 | 用 `SetWindowPos(qt, 自己那个 Electron 的 HWND)` **相对插入**：只压在自己模型之上，不压到别人模型头上；先用 `GetTopWindow/GetWindow(GW_HWNDNEXT)` 比较 z 序，已在上方就跳过 |
| `main.js` `/bounds`、`/visible`、load 回调 | `setAlwaysOnTop()` + `moveTop()`；`setBounds` 每帧缩放都调，等于 60fps 抢 z 序 | 去掉 `moveTop()`，仅当 `!win.isAlwaysOnTop()` 才补置顶 |
| 补置顶定时器 | 固定 2000ms，多个 worker 同频 | 每次 tick 后随机重排 2400–3600ms，避免同帧 DWM 排位 |

只有**用户点击**某个模型时才会把它整体抬到最前（`PetWidget.mousePressEvent` →
`_raise_own_windows()`），这是有意的唯一"抢占"入口。

排查时设 `PET_ZDEBUG=1`，`_js_console.log` 里会出现 `[ZDBG]` 行（`SetWindowPos(TOP)` /
`place_above` / `raise_own`），能直接看出是哪一次调用抬的窗。

### 覆盖层本来就要压在自己的模型上（v0.7.2）

Qt 覆盖层同时负责两件事：收点击 **和** 画"双击热区"的黄色圆点 + 序号。它一旦掉到
Electron 窗下面，模型照样能点（Electron 整窗鼠标穿透），但圆点和数字会被模型整个盖住。

两个坑，都已修：

| 坑 | 说明 |
| --- | --- |
| `SetWindowPos` 第二参数方向相反 | 它的语义是「排在我**后面**（更靠下）的窗口」，传 ref 会把自己插到 ref **下面**。所以 `_place_above(qt, el)` 实际执行的是 `SetWindowPos(el, qt)`（把 el 压到 qt 下方），再 `_is_above` 复核；复核不过才退化到"抬到 topmost 顶" |
| 显示热区时要立刻复位 | 圆点只显示 3 秒，等下一次补置顶 tick（最长 3.6s）黄花菜都凉了；`show_hotspots()` 一开始就先 `_pin_qt_topmost()` |

另外两处配套：

- 圆点从半径 7.5px / 8pt 放大到 9.5px / 9pt，并加了 1.8px 深色描边 —— 在深色美术上也看得清；
- `_apply_interaction_mask()` 里"并进命中区"的热区方框从 ±7px 放大到 ±15/±13px：
  否则窗口 mask 会把圆点和序号裁掉半截，看着就像被模型挡住了。

复现/回归用的自检命令：`{"type":"__hotspots__","secs":6}`（`/cmd_sync`），一次把全部热区
标出来；配合 `PET_ZDEBUG=1` 的时间线可以确认"先被模型盖住、后被拉回上面"的全过程。
`{"type":"__geom__"}` 会一次返回渲染层的 `screen/winOrigin/stageLocal/clipLocal/model bounds`
加上 Python 侧的 `stage_rect/seeds（含 nx/ny/reg）`，是核对"热区坐标和模型实际位置对不对得上"
最快的入口；`{"type":"__dblclick__","nx":..,"ny":..}` 用来逐点验证每个热区是否点得到自己。


## Electron 显示壳的生命周期（v0.7.1）

Electron 是 worker 用 `subprocess.Popen` 起的**独立进程**，不随 worker 退出。旧版右键「关闭」
只让 worker `os._exit(0)`，于是屏幕上留下一个孤儿透明窗：清单里已没有这个模型、没人响应点击，
但模型还画在那里（"关闭了模型还在"）。现在三条路都收干净：

1. worker 退出前（`/shutdown` 的 `_bye()`、回调父进程失败的自关分支）先调
   `_stop_electron_display()`：POST `/quit` → 等 0.8s → `terminate()` → `taskkill /F /T`；
2. `tool.py` 的 `_kill_pid()` 改为优先 `taskkill /F /T`（整棵进程树），`TerminateProcess` 只兜底；
3. `main.js` 新增 `--parent-pid=`（`_start_electron_display` 传入 worker pid）+
   每 2s 用 `process.kill(pid, 0)` 探活；worker 被强杀来不及发 `/quit` 时，壳自己 `app.quit()`。

## 全屏可见（display_mode=full）下的双击热区（v0.7.1）

全屏可见时画布是整块显示器，模型自带的布景/底座（门框、树丛、地板）在 alpha 上同样不透明。
旧的热区网格只看 alpha，最远点采样会把热区均匀铺在这张"原画"上 —— 用户看到的就是
"热区分布到空白区域、没落在模型身上"。现在 `_clickMap` 两道筛：

| 筛 | 做法 |
| --- | --- |
| Stage 圈定 | `_stageBand()` 把 Stage 换算成画布归一化矩形（外扩 2%），格子只在其中取；`_alphaMask`/`_alphaAt` 同步把 Stage 之外一律当透明 |
| 前排筛选 | 每格记录最上层 drawable 的 `renderOrder`；按所有格子 order 的中位数保留"前层"格子，筛得太狠（< 8 格或 < 25%）就放宽到 25 分位，仍太少就不过滤 |

赤城（带整幅布景的 9000×9000 原画）实测：地板类格子 order=1–3、门框≈348–379、角色本体
515–1117；中位数筛完热区全部落到角色身上，21 个热区 21 个可点，空白处仍返回 `transparent`。

## 热区为什么必须按 Stage 切网格（v0.7.3）

旧版 `_build_click_map` 固定下发 `{"cols":18,"rows":26}`，渲染层把网格铺在**整块画布**上。
local 模式下画布是整块显示器（1920×1080）、Stage 只有 210×240：一格 107px，模型只压得住
1~2 列 —— 所有热区于是挤在同一条竖线上，用户看到的就是"热区停在屏幕固定区域、不在部件上"。
实测证据：`__geom__` 里 6 个 seed 的 `nx` 全是 0.3611（18 列网格的第 6 列中心），`ny` 却从
0.2115 跨到 0.7115（已经超出模型包围盒 → 有的点根本不在模型身上）。

现在的规则：

| 项 | 做法 |
| --- | --- |
| 网格范围 | 铺在 `_stageBand()` 圈定的 Stage 范围内，不再铺整块画布 |
| 网格密度 | 只下发目标格数（默认 900），边长 = `sqrt(Stage 面积 / 格数)`，小 Stage 自然更细、大 Stage 更粗，总格数恒定 → 耗时可控 |
| 取样帧 | 每次构图先 `_captureClickFrame()` 抓当前帧；Stage 缩放/移动后旧缓冲的裁剪偏移已过期，拿旧帧取 alpha 会大面积误判成透明 |
| 质量门 | 5 点取**最小** alpha（≥40 才算实心格），细发丝/轮廓边缘不进候选 |

实测：符玄 local（Stage 700×800 / 格 25px）→ 171 实心格、前排筛完 87 格，6 个热区落在
头顶、脸、胸口、手臂、腿、脚；赤城 full → 428 实心格 / 214 格，21 个热区分散在头、脸、手、
躯干、裙摆、腿。移动 Stage 后（`setPosition` 0.2→0.62）热区跟着走，6/6 仍命中自己。

## 作者声明的交互区（HitAreas）怎么落地（v0.7.3）

`model3.json` 里的 `HitAreas` 是作者画的点击区（Cubism 标准字段，`Id` 指向 ArtMesh 或 Part）。
有就**按它布点**，没有才退回推断：

1. `hitBoxes` 把每个区解成归一化矩形；顺手返 `cx/cy`（区中心）。
2. 动作组/动作名里带着区 Id（碧蓝航线那种 `TapTouchHead` ↔ `TouchHead`）→ 优先一对一对上；
   对不上就按部位语义（区名 `Head`/`Body` → head/body）匹配。
3. 区太窄、网格一格都没压住时，用**区中心**补一格兜底（`synth`），否则该区的动作会被
   "全局最远点"甩到别的部位上 —— 也就是"作者的交互逻辑没实现"。
4. 双击时先看点在哪个区的矩形里，只在**该区**的热区里挑最近的一个（相邻区挨着时，
   纯最近邻会把头部的点击判给身体）。
5. 每个热区都记了 `reg`（所属作者区 Id），`__geom__` 里可直接看到。

Haru（官方样例，作者画了 `HitArea`=Head / `HitArea2`=Body）实测：12 个热区全部带 `reg`，
"点头"落到 `HitArea`、其余落到 `HitArea2`，12/12 命中自己。

## 覆盖层上的提示控件必须并进 setMask（v0.7.4）

Qt 覆盖层在直出模式下用 `setMask(QRegion)` 把"鼠标能点到的地方"裁到模型剪影上。
**mask 裁的是整个窗口，不只鼠标命中区**：窗口里所有子控件（`LoadingBubble` 加载气泡、
双击部位提示 `zone_hint`、部件隐藏模式的提示/确认/取消）一起被裁。

加载/切换模型的气泡贴在 Stage 顶部、模型头顶**剪影之外**，于是被裁成"只剩压在头发上的
那一小块"，用户看到的就是「切换模型的消息提醒显示不完全」。

规则：

1. `_apply_interaction_mask()` 的 `ui_rects` 必须并入当前**可见**的覆盖层子控件
   （气泡、部位提示、部件模式按钮），外扩 4px 吸收气泡 ±2px 浮动，避免每帧重建 mask。
2. mask 的 `key` 里带 `ui_rects`，控件矩形一变就重算。
3. 控件 `show()` / `hide()` 之后立刻调一次 `_apply_interaction_mask()`（气泡在
   `show_loading` / `show_progress` / 淡出 `hide()` 后调 `_resync_mask()`；`zone_hint`
   在 `_flash_zone_hint` 的显示与 `_hide()` 里调），否则要干等下一次剪影刷新。

`__bubble__` 自检（`POST /cmd_sync {"cmd":{"type":"__bubble__","text":"切换模型中"}}`）
直接回报 `bubble_rect` / `mask_bounds` / `bubble_in_mask`。符玄 local 实测：
`bubble_in_mask=true`，`mask_bounds` 顶部 141 == 气泡顶 141（修复前剪影顶在 160，
气泡上半截在命中区之外）。

## 排查

- worker 起不来：先读 `_workers/pet_<tag>.log`；再读插件根目录 `_js_console.log`（行首带实例标签）。
- 怀疑「窗口在屏幕上但列表里没有」：看 `_workers/state_<pid>.json` 是否在、`pid` 是否活着、
  里面的 `control_port` 能否连上（`Get-NetTCPConnection -LocalPort <port> -State Listen`）。
  旧版本（v0.6.2 及以前）起的 worker 不写自述文件、无法被找回，只能手动结束进程。
- 端口冲突：`action=list_servers` 按实例列出 url / dir / pid。
- 模型是否真的加载完成以 `/state.ready` 为准（实时反映，不是一次性快照），大模型冷启动
  超过 30 秒也不会被误判为失败。

## 多模型命中区必须用前排格子（v0.7.5）

一个模型 = 一个全屏 Qt 透明覆盖层，鼠标能不能点到我，只取决于 `setMask` 的命中区。
早期 local 模式用 `model_rect_norm` 包围盒、full 模式用 Stage 包围盒，两者都是**矩形**：

- local 的包围盒把模型透明边也算命中区，两个模型贴在一起时，上面那块矩形会把下面
  模型身上的点击一起吃掉 —— 表现就是"多个模型时有些模型拖不动"；
- full 的 Stage 就是整块屏幕，包围盒等于把桌面和邻居的点击全吃掉。

现在两级统一：**优先用双击热区那套前排格子（角色本体剪影）**，取不到才退回
alpha 网格 / 包围盒。

1. 格子列表与网格尺寸由 `_build_click_map` 直接写进 `state` 私有键：`_cm_cells`
   （逐格 `x/y`，画布归一化）、`_cm_cols/_cm_rows/_cm_band`（算格宽）、`_cm_seq`
   （每次重算 +1）、`_cm_stage`（发布那一刻的 Stage 位置）。
   `click_map["cells"]` 只是**格子数量**，不能拿来铺 mask。
2. mask 的 `key` 必须带 `_cm_seq` 与 Stage 位移：格子数量常常一格不变，只按数量
   当 key 会让 `setMask` 被判成"没变化"，模型一移动命中区就停在原地（松手后点不动）。
3. 拖拽过程中 Stage 在动、热区重算是 220ms 防抖后才跑，所以铺 mask 时按
   `当前 Stage - _cm_stage` 把格子整体平移，命中区全程跟着模型走。
4. 每格外扩 1px，避免格与格之间出现 1px 的穿透缝。

回归（`_workers/_diag_matrix6.py`，按命中区中心取点、每个模型连拖两次）：

| 组合 | A 拖拽 | B 拖拽 |
| --- | --- | --- |
| local + local | 2/2 moved | 2/2 moved |
| local + full | 2/2 moved | 2/2 moved |
| full + full | 2/2 moved | 2/2 moved |

## 两路抓帧并发会把 worker 带崩（v0.7.6）

full 模式拖完松手，worker 会在 ~0.4s 后**静默消失**：没有 Python traceback、
`PYTHONFAULTHANDLER=1` 也不出栈，Electron 侧只留下 `parent pid ... gone, quitting`。
Windows 事件日志给出真凶：

```
出错模块 Qt5Core_conda.dll  异常代码 0xc0000409 (fail-fast)
```

定位过程（`PET_SKIP_ALPHA_REFRESH` / `PET_SKIP_CM_REFRESH` 两个开关做二分）：

| 松手后重算什么 | 结果 |
| --- | --- |
| 只重算 alpha 剪影 | 存活 |
| 只重建双击热区 | 存活 |
| 两个都算 | 崩 |

两路都在同一个渲染器上做 canvas readPixels（截图 + 取 alpha），并发时渲染侧直接
fail-fast，连带把宿主进程带走。

规则：**同一渲染器上的抓帧必须串行**。

1. `_request_alpha_mask()` 进门置 `state["_alpha_busy"]=True`，回调（含异常路径）
   清掉。
2. `_refresh_hit_geometry()` 只发起 alpha 抓帧，并把 `_geom_pending` 记下；
   热区重建延后。
3. `_poll_cmds()`（10ms，主线程）在 `_alpha_busy` 落下去（或超过 1.2s 超时）后
   才消费 `_geom_pending` 去重建热区。

实测：单个 full 模型松手后 1.6s 仍存活（修复前 0.4s 就死）；三组合矩阵连拖 12 次
无崩溃。

## 拖拽惯性 = 身体/衣物摆动，不是整模型滑行（v0.7.7）

用户要的"拖拽惯性"指的是**手一拽、身体和头发衣物还甩两下**，而不是松手后整个模型
继续往前滑（那样是"拖动有延迟"，已被明确否决）。

### 被否决的第一版

`PetWidget` 里按手势速度让 Stage 继续滑行 + 摩擦衰减 + 撞边界回弹
（`INERTIA_*`、`_inertia_start/_inertia_tick`），实测衰减曲线
`660→402→214→113→59→29→0`（0.5s 滑 110px）、贴边还会回弹。
物理上没错，但语义错了 —— 已整段删除，连带 `__inertia__` 自检入口与
`__mask__` 里的 `inertia/inertia_active` 字段。

### 现在的实现（渲染层参数物理）

`renderer.html` 里一套二阶弹簧，把 Stage 位移的**速度 + 加速度**当外力，
驱动 Live2D 参数产生滞后摆动：

| 通道 | 参数 | K / C | 幅度上限 |
| --- | --- | --- | --- |
| bodyRoll | `ParamBodyAngleZ` | 22 / 5.4 | 13° |
| bodyPitch | `ParamBodyAngleX` | 22 / 5.4 | 10° |
| headRoll | `ParamAngleZ` | 34 / 7.0 | 7° |
| headTilt | `ParamAngleY` | 34 / 7.0 | 5° |
| hair | `/hair|ahoge|pigtail/i`（最多 8 个） | 9 / 2.2 | 1.6 |
| cloth | `/skirt|cloth|dress|ribbon|scarf|cape|sleeve|tail/i`（最多 8 个） | 10 / 2.4 | 1.2 |

要点：

1. `_swayFeed()` 挂在 `_applyPetLayout()` 末尾，按 Stage 位移差分出 px/s 速度并平滑；
   `_swayTick()` 挂在 `_paramTick()` 里（track/stream 之后、口型锁之前）。
2. 目标角 = `-(速度*0.0022 + 加速度*0.0006)`，夹到 ±1 再乘通道幅度；弹簧追这个目标，
   过冲一次再收回 —— 就是"甩一下再停"。
3. **松手后没有 Stage 更新**，速度按时间衰减（`0.10^dt`，约 0.90/帧），
   衰减放在算加速度之前，速度一掉就成"减速冲击"，这正是回摆的来源。
4. 叠加参数时不能无脑 `当前值 + 偏移`：有 track/tween/stream 每帧**绝对写**的参数
   当前值就是干净基线；没人写时才要减掉上一次自己叠加的量。用"上次写进去的值"和
   当前值比对来分辨这两种情况（`_sway.wrote`），否则开了鼠标跟随就会把跟随写坏。
5. 幅度按参数真实量程收敛（`min(定义上限, 量程*0.42)`），不同模型同一参数量程差很大。
6. 偏移收到 `4% 幅度` 以内直接归零，避免模型长期歪着一点点。

### 实测

`_workers/_diag_sway_accept.py`（真鼠标拖 220px × 10 帧）：

| 模型 | 通道 | 拖中峰值 | 松手位置漂移 | 2s 后偏移 |
| --- | --- | --- | --- | --- |
| 符玄 | 4 | `BodyAngleZ -6.7°`、`AngleZ -6.0°` | 0 px | ≈0（-0.07） |
| 赤城 | 8 | `BodyAngleZ -7.1°`、`AngleZ -6.4°`、`HairFront -0.66` | 0 px | ≈0 |
| 符玄 track=true | 4 | `BodyAngleZ -5.8°`、`AngleZ -5.2°` | 0 px | ≈0 |

`_workers/_diag_rigbind.py` 证明参数真绑在骨架上（写值 → 强制 update → 网格顶点和变化）：

| 参数 | 符玄 | 赤城 |
| --- | --- | --- |
| `ParamBodyAngleZ` | -26.51 | -51.03 |
| `ParamBodyAngleX` | -14.09 | +25.80 |
| `ParamAngleZ` | -22.74 | -5.60 |
| `ParamHairFront` | -6.99 | **-0.01** |

最后一行要注意：**带物理（physics3.json）的模型，头发参数每帧会被物理覆盖**，
我们叠上去的量看不见（赤城就是）。所以头发/衣物通道只对没有物理的模型有效，
身体/头部两对 Angle 参数才是主要观感来源。

### 自检入口

- `POST /cmd_sync {"cmd":{"type":"__sway__","reset":true}}` → 通道表 + 各参数当前偏移 +
  本次手势峰值（`peak`）。
- `{"type":"probeParams","params":[["ParamBodyAngleZ",-6]]}` → 写参数并强制更新一帧，
  返回网格顶点和，用来判断"这个模型的骨架到底绑没绑这个参数"。

## 加载中可拖动 + 滚轮漏给下层窗口（v0.7.8）

两个现场 bug，根因都在"Qt 覆盖层的鼠标命中区"这一个点上。

### bug①：模型还没加载出来，屏幕任意位置拖动/滚轮都会动到它

覆盖层是**整块屏幕**的工具窗。命中区靠 `setMask()` 裁到模型剪影，但剪影要等
渲染层抓完 alpha 才有；这段时间窗口没有任何 mask，**整屏都在命中区里**，
`mousePressEvent` / `wheelEvent` 又不看就绪状态，于是：

- 在桌面任意位置按住拖一下 → `move_stage_to()` 把还没画出来的 Stage 挪走；
- 随便滚一格 → `_scale_wheel()` 改掉它的缩放。

修法（两条护栏 + 一个记账）：

1. 就绪状态进 `state["ready"]`：`_show_loading()` 置 False，`_on_model_ready()` 置 True。
2. `mousePressEvent` / `wheelEvent` 开头就查 `state["ready"]`，False 直接 `ignore()`。
3. `_apply_interaction_mask()` 在未就绪时把命中区缩到**加载气泡那一圈**
   （`_bubble.geometry().adjusted(-10,-10,10,10)`，没有气泡就给 1px）。
   不能用 1px 了事 —— `setMask` 会连子控件一起裁，"加载中"气泡会被裁没
   （那正是之前修过的"消息提醒显示不完全"）。`PetWidget.__init__` 也先把命中区
   收到 1px，避免窗口刚建好、剪影还没抓到时吃掉整屏点击。

实测（`_workers/_diag_loading_input.py`，赤城冷加载）：

| 时刻 | stage_rect |
| --- | --- |
| 加载中（ready=False） | `[813, 312, 294, 336]` |
| 加载中在空白点拖 260px + 滚 3 格 | `[813, 312, 294, 336]`（**没变**） |
| 加载完成（补窗宽之后） | `[789, 312, 343, 336]`（宽度 +49、x −24 = 居中补宽，非输入导致） |

加载中的 `mask_bounds` = `[886, 308, 148, 74]`（气泡 128×54 + 10px 外扩），`rects=1`。

### bug②：模型上方滚轮，下面的窗口也跟着滚

命中区里有"洞"就会漏：滚轮走到窗口外 → Windows 把这一格发给下面的窗口 → 下层
聊天/浏览器跟着滚动。

根因是**格子只按平移映射，没算缩放**：`_apply_interaction_mask()` 里格子是发布
那一刻的 Stage 里量出来的，之前只用 `dx/dy` 做整体平移。滚轮缩放会同时改 Stage
尺寸，模型长出来了、命中区还停在旧尺寸，光标正好落进"新旧之间的缝"里。

修法：格子按 Stage 的**平移 + 缩放**一起搬：

```
屏幕x = 当前Stage原点 + (格子屏幕x - 发布时Stage原点) * (当前Stage宽 / 发布时Stage宽)
```

格宽同样乘缩放比；mask key 里加上 `sx/sy`，缩放一变就强制重算。
另外 `wheelEvent` 命中不到时用 `model_rect_norm` 兜底（点落在模型包围盒内仍算
在模型上），并把光标那一小圈 12×12 并进命中区 —— 否则下一格又被判成"在外面"。
命中就 `e.accept()`，不吃掉的事件才是漏给下层的那个。

实测（`_workers/_diag_wheel.py`：一个 Qt 探测窗压在桌宠下面记自己的滚轮事件）：

| 阶段 | 探测窗收到滚轮 | 光标在命中区 | stage |
| --- | --- | --- | --- |
| 修复前 ①1 格 | 0 | 第1格后 **False** | 315→352 |
| 修复前 ②5 格 | **3**（漏了） | 全程 False | 只涨到 442（应 621） |
| 修复后 ①1 格 | 0 | True | 315→352 |
| 修复后 ②5 格 | **0** | 全程 True | 352→621（×1.12⁵，吃满 5 格） |

顺带修好一个没人报但确实存在的老问题：**缩放之后模型点不动/拖不动**
（`_workers/_diag_zoom_drag.py`）：放大 3 格后拖动 `(589,178)→(679,198)` ✅、
缩小 3 格后拖动 `(737,268)→(827,288)` ✅，模型外空白处滚轮不动 ✅。

多模型拖拽矩阵（`_diag_matrix6.py`）重跑仍 12/12 `moved=True`，无崩溃。

## 摆动幅度改为跟拖动速度挂钩（v0.7.9）

现场反馈："慢慢拖动一点点就摆幅巨大，非常不真实"。v0.7.7 那版把
**"松手后速度归零"当成加速度冲击**去驱动二阶弹簧，问题是这个加速度是逐帧差分
算出来的（`a = Δv/dt`）：松手那一帧 Δv 就等于整个末速度、dt 只有 16ms，
`a` 一下冲到几千，被 `clamp ±3000` 后**慢拖和快拖都打满**——
轻轻挪 100px，峰值就已经是满幅的一半。

真实惯性只跟**末速度**有关，"停得多突然"影响很小。改法：

1. 删掉加速度冲击项，目标角只由速度算：
   `ratio = (|v| - VDEAD) / (VREF - VDEAD)`，夹 ±1，再 `^1.25` 让中低速更轻；
   `_SWAY_VREF = 1500` px/s（到这个速度打满），`_SWAY_VDEAD = 45` px/s（更慢完全不摆）。
2. 松手之后由 `_swayTick` 把速度按 `0.10^dt` 衰减（≈0.3s 归零），弹簧追这条衰减
   曲线 —— 快拖时目标高、弹簧过冲大，本身就是"甩两下"。
3. 弹簧配硬一档才追得上这 0.3s 的脉冲（body `K 22→40 / C 5.4→7.6`、
   head `34→55 / 9.4`、hair `9→16 / 3.4`、cloth `10→17 / 3.6`），
   阻尼比仍在 0.55~0.60，过冲一次就回摆。
   *（中间试过把衰减放到 0.55/s：快拖能到 8.4°，但要 10s 才收干净、松手后模型
   一直歪着，弃用。）*

实测（`_workers/_diag_sway_vel.py`，符玄，`ParamBodyAngleZ` 峰值，cap=13°）：

| 拖动速度 | 改前峰值 | 改后峰值 | 末值(松手 2.6s) |
| --- | --- | --- | --- |
| 25 px/s（极慢挪） | — | **0.00°** | 0 |
| 190 px/s（慢拖） | 3.63° | **0.41°** | 0 |
| 750 px/s（中速） | 5.42° | **3.65°** | 0 |
| 1900~2100 px/s（快甩） | 6.50° | **7.92°** | 0 |

改前 1 : 1.5 : 1.8（速度几乎没影响，慢拖=满幅一半）；改后 1 : 9 : 19，
且模型位移严格等于手拖的距离（120→120 / 300→300，惯性不碰位置）。

`__sway__` 自检新增 `vratio`（当前速度对应的幅度比例）、`vref`、`vdead`。

## 命中区必须并高精度 alpha 剪影，且只并"角色附近"（v0.8.0）

现象：**模型上看得到的地方，点下去却穿透到下面的窗口**（"鼠标有时还是会穿透"）。

取证：新写 `_workers/_diag_leak.py`，把渲染层逐点 alpha（新增 `__alpha__` 自检命令，
批量调 `alphaAt`）和 `__mask__` 的 `pts_in_mask` 做对照 —— 符玄 local 静置时
**可见像素 902 个、命中 439 个、漏 463 个，漏点率 51.3%**；ASCII 网格里漏点集中在
**模型左右边缘一圈**和**下半身裙摆/腿**。OS 级 `WindowFromPoint`（`_diag_hit.py`）
同样只有 264 个点真的落在覆盖层。

根因：命中区优先用 `state["_cm_cells"]`（渲染层"前排格子"，按格内平均 alpha 过滤）。
符玄 350×400 的 Stage 只有 18 个格子（每格约 30px），边缘和裙摆这种细条**整格被判成
透明**，那一块就整块不接点击 —— 而更细的 `alpha_mask`（96×100，每格 20×10.8px）
却被 cells 分支挡在外面。

改法：

1. `PetWidget._region_from_alpha(am, pw, ph)`：把 alpha 位图按行 run-length 转成
   region，并且**与 cells 用同一套 Stage 映射**（`屏幕x = 当前Stage原点 +
   (网格画布x - 抓帧时Stage原点) * 缩放比`）—— 抓帧时的 Stage 原点记在
   `alpha_mask["stage"]` 里。
2. `_apply_interaction_mask` 末尾把 alpha region 并入；**并且先裁到角色附近**
   （cells region 包围盒 `±26px` 求交）。整幅并入会踩坑：全屏可见模式下 alpha 网格
   里还有背景/布景，整块屏幕都成了命中区，**邻居模型的点击会被这个窗口抢走**
   （多模型矩阵当场 0/12）。
3. alpha 抓帧分辨率 `96×100 → 200×160`（每格 9.6×6.75px），`renderer.html` 的
   `_alphaMask` 上限放宽到 `cols/rows ≤ 240`；cells 的 mask key 增补
   `alpha_mask_ver`（alpha 一更新必须重算）。

实测：

| 指标 | 改前 | 改后 |
| --- | --- | --- |
| 静置漏点率（符玄 local） | 51.3%（463/902） | **0.0%（0/897）** |
| 放大 2 格后漏点率 | — | 0.1%（1/1385，最外缘 1 个像素级点） |
| `mask_rects` 矩形数 | 18 | 47~53 |
| OS `WindowFromPoint` 命中点 | 264 | **619/620** |

回归（**注意：本机真实光标会被外部程序抢占，`SetCursorPos` 读回固定在别处**，
所以改用 `SendMessage` 直接投递 `WM_LBUTTONDOWN/MOUSEMOVE/UP`，脚本
`_diag_dragmsg.py` / `_diag_zoomdrag_msg.py` / `_diag_two.py`）：

- 单模型：press 后 `dragging=True`，拖动 `(550,170)→(630,200)` ✅；滚轮放大
  `350→439` ✅；空白处滚轮不缩放 ✅。
- 缩放后拖动：放大后 `(610,176)→(690,206)` ✅、缩小后 `(711,230)→(791,260)` ✅。
- 双模型（符玄 local + 赤城）：两者各自拖动 6 步逐步跟随 ✅，
  交叉检查 `A吃掉B中心=False / B吃掉A中心=False`（互不抢点击）。
- 多模型矩阵脚本 `_diag_matrix6.py` 的 `moved=False` 是假阴性：它靠真实光标
  拖动，而本机光标被抢；同参数换成消息投递即正常。

## 切模型补宽改成缓动外拉（v0.8.1）

现象：换到包围盒更宽的模型时，画面**先只露出中间一条，随后整块一下子弹出来**。

根因：`pending_model_width` 走的是 `_set_stage_rect(...)` 一次性改宽 —— 渲染层
下一帧 fit 到的就是最终宽度，中间没有任何过渡；宽高比 `_sync_stage_to_model()`
那一处同样是直接赋值。

改法：

1. 新增 `_wid_anim` + `_wid_timer` + `_animate_stage_width(target_w, dur=0.30)`：0.3s
   easeInOutCubic，每帧以"此刻的中心"为基准改宽度（中心固定、宽度逼近），
   动画中途再切一次模型会以当前宽度为起点续拉，不会跳回起点。
2. `_poll_cmds` 的 `pending_model_width` 与 `_sync_stage_to_model()` 都改调它。
3. 滚轮缩放时显式 `_wid_anim["active"] = False`：缩放动画自己管几何，
   两套动画同时改宽度会横向抖一下。

实测（Ctrl+→ 连切 8 个模型，45ms 轮询 `/state.stage_rect[2]`）：

| 切换 | 宽度序列 |
| --- | --- |
| Rice | 385 → 393 → 403 → 420 → 462 → 537 → 592 → 623 → 643 → **651** |
| Wanko | 651 → 637 → 615 → 587 → 577 → 562 → 520 → 487 → 459 → **442** |

## 换装：服装参数通道 + 右键菜单 + 中键（v0.8.1）

适用面：模型把衣装做成**参数**（cdi3 里 `Clothes1..5`），配套 `Exp3/*.exp3.json`
是作者写好的切换动作（裙子切换、脱鞋、体型1/2、胸口衣服、尾巴开关）。
符玄/赤城这类没有 `Clothes*` 参数的模型扫不到通道，**右键菜单里不会出现「换装」**。

关键坑：这些参数的范围是 `0..1`，但作者写的是 **Add 语义**——
`Value=-1` 表示"切到 0"、`+1` 表示"切到 1"。直接 `setParameterValueById(id, -1)`
会被 Cubism 裁到 0，**等于没换**（实测 `getParamValues` 读回 0、剪影 0 变化）。
所以 `scan_outfits()` 把 exp3 值归到 `[0,1]` 当目标态，并在出现负值时补一个
`1.0` 的另一端，通道才真的能来回切：

| 通道 | 状态（值） |
| --- | --- |
| 长短裙切换 | 默认（0）/ 开启（裙子切换）（1） |
| 大小变 | 默认（0）/ 体型1（0.4）/ 体型2（1） |
| 胸口衣服 | 默认（0）/ 胸口衣服（1） |
| 鞋子 | 默认（0）/ 脱鞋（1） |
| 尾巴 | 默认（0）/ 开启（尾巴开关）（1） |

实现：

1. `scan_outfits(model_file)`（模块级，按 model3.json mtime 缓存）：解析 cdi3 参数名
   → 通道标签（去掉「1.」序号），扫模型目录 2 层内的 `*.exp3.json` → 通道状态。
2. `state["outfits"] / outfit_sel / outfit_cursor`，由 `_on_model_ready` 里的
   `_refresh_outfits()` 刷新（换模型必重扫，否则菜单还挂着上一套衣服）。
3. 渲染层 `applyOutfit` + `_outfitLock`：参数**每帧在 `_paramTick` 末尾强压**
   （`_applyOutfitLock()` 在动作播放的 `return` 前也调一次）。只写一次的话，
   播个动作就被 motion/expression 写回默认，表现是"换完装一跳动作就穿回去"。
4. 右键「换装」→ 通道子菜单（当前态打 ✓）→「下一个换装 / 全部恢复默认」；
   **中键单击模型 = 轮到一个通道、切到它的下一个状态**，并弹提示
   「换装：长短裙切换 → 开启（裙子切换）」。
5. `_after_outfit_change()`：换装改了剪影，必须作废 `_pixBuf` 抓帧缓存
   （新增 `invalidateHitFrame` 命令）+ 清 `_cm_cells/_cm_model`（`_reusable_since_capture`
   只比模型 URL，换装不换 URL，它看不出剪影变了），再 `_schedule_hit_refresh()`。

实测（DeepSeek「大肥鱼」，`--track 0`）：

- 菜单 dump 里出现 5 个通道、状态与勾选正确；`__outfits__` 的 lock 逐次累积。
- 中键连点两次：`Clothes1=1` → `Clothes2=0.4`，提示按通道轮转 ✅。
- 剪影（固定缩放下重抓帧，90×90 网格）：`Clothes1` 0→148 / 1→**115**；
  `Clothes5` 0→148 / 1→**156**（尾巴冒出来）✅。
- 热区跟着换装重算：日志 `clickmap` 66 格 → **32 格**（剪影缩小后命中区同步收窄）✅。

## 全局快捷键 Ctrl+方向键（v0.8.1）

桌宠是"置顶不抢焦点"的覆盖层，`QShortcut`/`keyPressEvent` 收不到任何键 —— 只能
`WH_KEYBOARD_LL` 钩子（`_GlobalHotkeys`，模块级，独立线程 + `GetMessage` 循环）。

| 组合 | 行为 |
| --- | --- |
| Ctrl+← / Ctrl+→ | 在 `model_root` 的模型列表里上一个 / 下一个（`_cycle_model`） |
| Ctrl+↑ / Ctrl+↓ | 在内置动画 + 模型动作的摊平列表里上一个 / 下一个（`_cycle_action`） |

要点：

1. **不吞键**：回调一律 `CallNextHookEx`，编辑器里的 Ctrl+← 照样按词移动光标，
   插件只是"顺便"收到一次。
2. **归属判断在钩子线程里做，绝不碰 Qt 对象**：可见性读 `_WSTATE["visible"]`
   快照、光标用 Win32 `GetCursorPos`、几何读 `state["stage_rect"]`。
   多实例时只认"光标落在自己 Stage ±40px"的那一个；`_visible_instance_count() <= 1`
   时不再挑位置（只有一个桌宠就不该要求先把鼠标放上去）。实例数 3s 缓存。
3. 钩子只 `put_nowait` 进 `_cmd_queue`，真正干活在 `_poll_cmds` 的 `__hotkey__` 分支：
   `_cycle_model` 有 `state["ready"]` 检查 + 2s 节流（重载渲染页 ~2s，连按会堆队列），
   `_cycle_action` 用 `state["anim_cursor"]` 记住位置。
4. 系统/Alt/Shift 同时按下时不响应（让给别的快捷键）。

实测（模拟 `keybd_event`）：

| 操作 | 结果 |
| --- | --- |
| Ctrl+→ ×1（光标在模型上） | 大肥鱼 → **Haru**，日志「快捷键切模型 → …」 |
| Ctrl+← ×2 | Hiyori → Haru → **大肥鱼** |
| Ctrl+↓ ×3 / Ctrl+↑ ×1 | 动作 2/6 摇头 → 3/6 歪头 → 4/6 摇摆 → **3/6 歪头** |
| 光标移到 (60,60)（远离模型） | 单实例下 Ctrl+↓ 仍生效 ✅ |

## 快捷键「装好了但按了没反应」（v0.8.2）

现象：日志明确写着「全局快捷键已就绪」，实际按 Ctrl+方向键毫无反应，也不弹提示。
钩子本身没坏，是**它在三个地方把按键丢掉而都不吭声**：

1. **小键盘方向键的 vkCode 不是 0x25..0x28。** 主键盘是 `VK_LEFT/UP/RIGHT/DOWN`，
   小键盘（NumLock 开着）给的是 `VK_NUMPAD4/6/8/2 = 0x64/0x66/0x68/0x62`。
   笔记本与紧凑键盘常常只走小键盘这一套 —— 原来只认前者，这类键盘必然全哑。
2. **`_WSTATE["visible"]` 只在命令队列的 show/hide/toggle 分支里置位。** 但显示
   有太多入口（`--show`/启动参数、右键菜单、tool.py、快捷键），漏掉任意一个，
   快照就一直是 False，`_cursor_on_me` 在第一关直接 return False，按键静默消失。
   改法：`_poll_cmds` 每轮用 `pet.isVisible()` 校正快照（GUI 线程，成本可忽略），
   `/state` 也顺手写回 —— 谁改了窗口状态都不影响判断。
3. **`GetAsyncKeyState(VK_CONTROL)` 不一定可靠**（注入键、某些键鼠驱动、IME 抢占）。
   钩子改为自己维护 Ctrl/Shift/Alt 的 down/up 状态，与 `GetAsyncKeyState` **取或**，
   漏判概率降到可以忽略。

另外把归属判断从 bool 改成 `(hit, reason)`，并加了两个诊断入口，以后"按了没反应"
不用再猜：

- `/state.hotkey_last` → `{"vk":39,"name":"right","hit":"only-instance","seq":4,...}`
- `_js_console.log` → `[HOTKEY] vk=0x27 name=right ctrl=1 alt=0 shift=0 hit=only-instance`

`hit` 的取值直接告诉你卡在哪：

| hit | 含义 |
| --- | --- |
| `window-hidden` | 桌宠窗口没显示（或被判定为隐藏） |
| `only-instance` | 只有这一个桌宠，不看鼠标位置，直接归我 ✅ |
| `cursor-in-stage` | 多实例，光标正好在我的 Stage 上 ✅ |
| `cursor-outside(n=2)` | 多实例，光标不在我这块 → 本次按键归别的桌宠 |
| `error:...` | 归属判断里抛了异常 |

`_visible_instance_count()` 同时补了**僵尸文件过滤**（`OpenProcess` 查 pid 是否
还活着）：死掉的 worker 留下的 `state_*.json` 被当成"另一个可见实例"时，单实例
用户会被误判成多实例，于是必须把鼠标放到模型上才响应。

实测（同一进程内模拟按键，`/state.hotkey_last` + 日志双证）：

| 操作 | 结果 |
| --- | --- |
| Ctrl+↓（主键盘 0x28）×2 | 动作 2/6 摇头 → 3/6 歪头 ✅ |
| Ctrl+→（小键盘 0x66） | 大肥鱼 → Haru ✅（原来这条路完全不通） |
| Ctrl+→（主键盘 0x27） | Haru → Haru_greeter ✅ |
| 先 `hide` 再按 Ctrl+↓ | `hit=window-hidden`，不动作 ✅ |
| 再 `show` 后按 Ctrl+↓ | `hit=only-instance`，动作 4/11 摇摆 ✅ |



## 模型服务端口池会饿死（v0.8.3）

现象：起第二个桌宠时直接报「无法绑定端口（18765-18780 均被占用）」，模型加载不出来。
根因是**端口池只有 15 个**（`18765..18779`），而单个 worker 进程会把用过的模型
HTTP 服务全留着（老代码里上限就是这 15 个）。一个跑了很久的实例（实测 pid 12320
的符玄，从 11/20 起没退过）把 18765-18779 全占了，后来的进程一个端口都拿不到。

改法：

- 池扩到 80 个：`_MODEL_HTTP_PORTS = (18765, 18845)`；
- 每进程常驻数收到 4：`_MODEL_HTTP_KEEP = 4`（当前模型 + 最近切过的 3 个），
  超出就 `_evict_one()` 回收最旧的（同时 `shutdown()` + `server_close()`，端口立刻释放）；
- 绑定失败不再直接放弃：循环「回收一个 → 再绑」，直到绑上或无可回收；
- `serve_forever(poll_interval=0.1)`：0.5s 的默认轮询会让退出时端口释放延迟，
  从另一个进程看就是"明明退了还占着"。

实测（6 个不同碧蓝模型并行、全部起在屏外）：

| 模型 | control-port | ready | stage 宽度 |
| --- | --- | --- | --- |
| jinluhao_3 | 51311 | True | 763 |
| chaijun_6 | 51312 | True | 767 |
| shi_3 | 51313 | True | 531 |
| aijier_4 | 51314 | True | 621 |
| anninvwang_2 | 51315 | True | 695 |
| gaoxiong_7 | 51310 | False（275 参数，仍在加载） | 385 |

端口分配：老实例仍占 18765-18779（旧代码），6 个新 worker 分到 18780-18785，
**零冲突**。全部退出后 18780-18844 归零。

## 让桌宠被遮挡/离开屏幕时也继续画（v0.8.3）

Chromium 会把"被别的窗口完全盖住"和"整体在显示器之外"的窗口判成 occluded，
然后停掉 rAF 与合成 —— 桌宠表现为**模型定格、再点也没反应**。Electron 壳启动时
补了四个开关（`_start_electron_display` 的 args）：

```
--disable-features=CalculateNativeWinOcclusion
--disable-backgrounding-occluded-windows
--disable-renderer-backgrounding
--disable-background-timer-throttling
```

注意 `backgroundThrottling: false` 只关定时器节流，**关不掉 rAF 暂停**，所以
`main.js` 里已有的那个设置不够，必须在命令行走 Chromium 的 feature 开关。

## 测试桌宠时不要占用用户桌面（v0.8.3）

两种方案都试过：

| 方案 | 结果 |
| --- | --- |
| `CreateDesktopW` + `STARTUPINFO.lpDesktop` 建隐藏桌面，在它上面跑 worker | ❌ 稳定失败：`ImportError: DLL load failed while importing _ctypes`（错误 1114，`pet_window.py` 第 21 行） |
| 正常启动，再把 worker 与其 Electron 子进程的**所有窗口挪到虚拟屏左侧之外** | ✅ 用户桌面看不到任何东西，worker/控制通道/模型全部正常 |

隐藏桌面失败前做过四变量隔离探针（`_workers\_desk_probe.py`：默认桌面×隐藏桌面、
有无句柄重定向，跑 `python -c "import ctypes"`）——**四种组合全成功**，说明
CreateProcess/隐藏桌面/句柄重定向本身没问题，是本机的 import 时序/DLL 环境导致，
不值得继续攻。

所以现在的无头测试入口是 `_workers\_desk_park.py`：

```
python _workers\_desk_park.py "<模型目录>" <control_port> <tag> --x 0.5 --y 0.55 --scale 0.55 --track 0
```

它用 Toolhelp32 构建**进程树**（Electron 壳是独立子进程，窗口不挂在 worker 名下，
只按 worker 的 pid 找窗口会全部漏掉），前 90 秒每 200ms、之后每 2 秒把树内所有
可见顶层窗口挪到 `SM_XVIRTUALSCREEN - width - 400`。

**唯一局限**：窗口落在所有显示器之外后 Chromium 不再合成这一帧，renderer 的
`gl.readPixels` 读回全 0 —— `getAlphaMask` / `getClickMap` 这类**像素型**命令
在屏外不可用（加了 `CalculateNativeWinOcclusion` 开关也没救回来，实测不透明格
一直是 0，而 worker 日志里 `drawables=767`、`lost=false`，说明模型确实在画）。
屏外测试只能用非像素维度校验：

- `/state`：宽度/位置/动作序号/当前 outfit 值
- `getParams` / `getParamValues`：参数是否真的被写入
- 需要剪影像素时必须把窗口移回屏内（会短暂出现在用户桌面）


## 中键改成"点哪换哪"（v0.8.4）

原来的中键是"每点一次轮到下一个通道、切到它的下一个状态"：点哪儿都一样，
用户根本预判不到这一下会脱裙子还是切体型 —— 尤其模型有 5 个以上通道时。

现在改成**按鼠标所指部位**：位置 → 6 个部位档位 → 该部位对应的通道组 → 组内轮转。

| 档位 | 纵向比例（相对窗口） | 靠边判手 |
| --- | --- | --- |
| 头部 head | 0.00–0.24 | |
| 上身 chest | 0.24–0.46 | |
| 腰臀 waist | 0.46–0.66 | |
| 腿 leg | 0.66–0.88 | |
| 脚 foot | 0.88–1.00 | |
| 手 hand | 0.20–0.80 | `nx < 0.20` 或 `> 0.80` |

通道归到哪个档位靠名字匹配（顺序有意义）：`手套/手/袖/腕/指`→手，
`鞋/靴/脚/趾`→脚，`裤袜/丝袜/袜/裤/腿`→腿，`裙/腰/尾/披风/围巾`→腰臀，
`帽/头/发/耳/眼镜/面具`→头，`毛衣/外套/内衣/泳装/衣/胸/领`→上身。
"手套"必须排在"衣"前面，"裤袜"必须排在"袜"前面，否则会被更宽的词先吃掉。

**这里踩了一个坑**：第一版用 `state["model_rect_norm"]`（模型可见包围盒）
算相对位置，实测判不出来 —— 该值来自 renderer 的 `model.x - w/2` / cw，
对带 fit 变换的模型直接给出**画布外**的坐标（大肥鱼：`x=3179`，而 `cw=1920`），
`rx` 永远 > 1，于是每次都退回全局轮转。改用窗口归一化坐标：`applyFit` 是
"按高度贴满舞台"的，模型上下沿基本等于舞台上下沿，纵向比例直接可当部位比例。

判出的部位**没有**对应通道时（例如大肥鱼没有帽子和手套），提示一句
「头部没有可换的服装」，不动手；完全判不出部位（点在模型外/拿不到坐标）
才退回原来的全局轮转。诊断入口：`{"type":"cycleOutfit","nx":..,"ny":..,"sync":true}`
会回 `{ok, zone, sel}`，屏外测试也能验证部位判定。

实测（大肥鱼，5 通道：长短裙/大小变/胸口衣服/鞋子/尾巴）：

| 点击位置 | 判定 | 结果 |
| --- | --- | --- |
| ny=0.12 | head | `ok=False`（没有帽子类通道）✅ |
| ny=0.35 | chest | 胸口衣服 0→1 ✅ |
| ny=0.55 | waist | 长短裙 1→0 ✅ |
| ny=0.75 | leg | `ok=False`（没有腿部通道）✅ |
| ny=0.93 | foot | 鞋子 0→1 ✅ |
| nx=0.06 / 0.94 | hand | `ok=False`（没有手套通道）✅ |
| 不给坐标 | — | 退回全局轮转，轮到长短裙 ✅ |

同部位有多个通道时组内轮转（腰部连点 4 次：长短裙 0→1→1→0、尾巴 1→1→0→0，
两条腰通道交替被轮到、各自切下一态）；单通道二态连点（胸口衣服 1→0→1）。


## 碧蓝这类模型也能换装：参数型通道（v0.8.5）

碧蓝航线（Azur Lane）的模型没有 exp3 换装动作，也没有 `Clothes1..5` 这种自解释
参数名 —— 作者把换装拆成一堆**互不相干的独立开关**：`Touch_drag16` 是"裙脱"、
`Touch_drag17` 是"裤袜穿脱"、`Touch_drag9` 是"内衣切换"、`Circulation_Head_16`
是"左白手套切换"…… 中文名只写在 cdi3 里。所以原来只会扫 `Clothes*`+exp3
的实现，在这些模型上一个通道都扫不出来（264 个 model3、0 个 exp3）。

**做法：一个参数 = 一个通道，档位按参数的真实值域枚举。**

为什么不像 exp3 那样把同部位的几条参数合成一个通道：它们不是同一个开关的多个档位，
而是互相独立的布尔/整数量（有的还带物理摆动的孪生参数）。合成一个就得猜"脱裙子
要不要同时设'裙消失'和'裙子穿脱物理1'"，猜错就是穿模。拆开让用户逐条点、点错能
马上改回来；"同部位一起去点"的体验交给中键的部位轮转（腰/腿/脚/手各自成组）。

识别规则（cdi3 的 `Parameters[].Name`）：

| 类别 | 词 |
| --- | --- |
| 服装词（必须有） | 裙 裤袜 丝袜 袜 鞋 靴 内衣 内裤 手套 毛衣 外套 兜帽 帽 披风 围巾 泳装 羽织 胸罩 肩带 戒指 耳环 |
| 切换词（必须有） | 穿脱 脱 穿 切换 消失 有无 选1 开关 取下 放下 戴上 |
| 排除词（有就跳过） | 物理 姿势 动作 表情 动画 层级 变化 尺寸 SIZE 位置 角度 背景 颜色 透明度 旋转 摇 缩放 Offset 手部 |

排除词是必须的：`裙子穿脱物理1/2`（kz1/kz2）是摆动触发参数、`左手套动画` 是手臂动画、
`手套X/Y/Z/SIZE` 是形变、`背景切换开关` 是背景、`姿势切换` 是姿势 —— 全都带
"服装词 + 动作词"，不排除就会在菜单里塞满点了没用的项。

**值域分两步**：文件里查不到 min/max，所以先按名字猜（`N选1` → N 档，其余 2 档），
模型就绪后异步发一次 `getParams`，用真实值域修正 —— 两端都是整数且跨度 ≤12 的
逐档枚举（`0..1`→2 档、`0..2`→3 档、`1..5`→5 档、`0..6`→7 档），连续量取 5 等分
（`0..1.5`→`0 / 0.375 / 0.75 / 1.125 / 1.5`）。注意 `getParams` 回的 `name` 是
**参数 Id**，不是 cdi3 中文名，两者别搞混。

### 顺带修掉的一个真 bug：换装锁压不住被动作驱动的参数

第一版实测发现 `Circulation_Head_16`（左白手套切换）菜单选了、锁表也写进去了，
但 `getParamValues` 读回来**永远 0** —— 这类参数在 `internalModel.update` 的
motion 阶段被写回去，而换装锁挂在 `app.ticker` 回调里，时机会被覆盖（`Param205`
这种没人动的参数就正常）。修法：给当前模型挂 `afterMotionUpdate`
（`_ensureOutfitHook()`，每帧确认模型没换过），它是"动作算完、写模型参数之前"
的最晚时机，锁在那里压值就锁得住了。

实测（gaoxiong_7，屏外实例）：

| 通道 | 真实值域 | 档数 |
| --- | --- | --- |
| 左/右白手套切换 | 0..2 | 3 |
| 手套2选1 | -2..2 | 5 |
| 回港鞋子开关 | -1..1 | 3 |
| 毛衣穿脱 | 0..3 | 4 |
| 裙脱 | 0..1.5（连续） | 5 等分 |
| 裤袜穿脱 / 裤袜 消失 | 0..6 | 7 |
| 裙消失 / 右鞋 脱 / 左鞋 脱 | 0..1 | 2 |
| 内衣切换 | 1..5 | 5 |

点部位 → 真实参数值（`cycleOutfit` 之后用 `getParamValues` 核对）：

| 点 | 命中通道 | 参数真值 |
| --- | --- | --- |
| 腰 ny=0.55 | 裙脱 | `Touch_drag16` → 0.375 ✅ |
| 腿 ny=0.75 | 裤袜 穿脱 | `Touch_drag17` → 1 ✅ |
| 脚 ny=0.93 | 回港鞋子开关 | `Param205` → 0（-1..1 的中间档）✅ |
| 胸 ny=0.35 | 毛衣穿脱 | `Touch_drag14` → 1 ✅ |
| 手 nx=0.06 | 左白手套切换 | `Circulation_Head_16` → 1 ✅（修复前恒为 0） |

其它模型识别到的通道数：`jinluhao_3` 3 个（内衣穿/帽子消失/交互内衣）、
`chaijun_6` 2 个（左/右袜切换）、`shi_3` 4 个（手套2选1 + 左右白手套 + 裙子切换）。

