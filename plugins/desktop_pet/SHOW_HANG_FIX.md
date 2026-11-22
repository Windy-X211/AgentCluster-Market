# 桌宠 show 一直不出现／命令长时间挂起 —— 原因与修复

版本：0.6.8（修复自 0.6.7）
涉及文件：`pet_window.py`（模型加载地址 + 允许 file:// 的 Chromium 开关）、`tool.py`（进程级重试兜底）、`renderer.html`（加载重试与不误报 ready）

## 1. 现象

重启程序后智能体调用 `show`，桌宠窗口创建了（`/state` 里 `visible:true`、`alive:true`），但画面全透明，命令一直"运行中"，最后报模型加载超时。

`GET /state` 的典型失败值：

```
ready: false
error: "Aborted."
frames: 0
```

## 2. 定位过程（关键证据）

1. 页面控制台固定序列：`[Live2D Diagnose]` → 立刻 `Live2D load failed: Aborted.`；失败会话全程没有 `[CSM][I] Live2D Cubism Core version` 行，也就是模型解析根本没开始。
2. `Aborted.` 只可能来自 XHR 的 `abort` 事件：`pixi-live2d-display-0.4.0` 的 `XHRLoader` 里
   `xhr.onabort = () => new LoaderError("Aborted.", e, 0, true)`。网络级失败会是 `Network error.`。
3. 给 `XMLHttpRequest.prototype.abort` 打桩后计数为 0 —— 库自己没调用 abort，是**浏览器/网络层**把请求取消了。
4. 同一页面里用 **同步 XHR** 请求同一批资源（`符玄.model3.json` / `.moc3` / `texture_00.png`）全部 200、耗时 7/45/61ms，说明 HTTP 传输层、端口、CORS 都正常。
5. 在**失败会话**的页面里做异步 XHR 探针（text / json / arraybuffer，包含别的端口）：

   ```
   [probe-async] abort text st=0 ms=79
   [probe-async] abort json st=0 ms=31
   [probe-async] abort arraybuffer st=0 ms=41
   ```

   而**成功会话**里同样的探针是 `load text st=200 ms=104`。结论：某些会话里该页面的**异步网络加载链路整体失效**，与具体 URL、CORS、模型内容无关。
6. 排除项（都已实测无效）：CORS 头、`Access-Control-Allow-Private-Network` + `OPTIONS` 预检、`localhost` 换 `127.0.0.1`、页面 visibility/focus（失败与成功都报 `visible/focus=true`）、`webglcontextlost`（0 次）、`unhandledrejection`（0 次）。

## 3. 修复

模型资源不再经 `http://127.0.0.1:18765/<token>/` 走 Chromium 的异步网络加载，而是**直接给渲染页 `file://` 地址**：

- `pet_window.py` 新增 `model_arg_for_serve()`：默认返回 `QUrl.fromLocalFile(<模型>.model3.json)`，即
  `file:///E:/appdata/AgentCluster/plugins/desktop_pet/assets/符玄/符玄.model3.json`。
- `file://` 的请求走浏览器进程的 file loader 直接读盘，**不经过那条坏掉的异步网络链路**；渲染页本身就是 `file://`，靠 `LocalContentCanAccessFileUrls=True` 与新增的 `--allow-file-access-from-files` 开关允许互读。
- `local_dir_of_model_url()` / `local_model_json_of_url()` 现在同时认 `file://` 与 `http://` 两种模型地址（模型切换、贴图/命中区、动作配音查文件都靠它）。
- `switch_model()` 收到 http 地址时会自动换算成本地 `file://` 地址，切模型不需要改调用方。
- 原来的本地 HTTP 服务保留未动（`/state.http_url` 仍然有值），需要回到老方案时设环境变量 `PET_MODEL_SCHEME=http`。
- `tool.py` 的进程级重试（`SHOW_RETRY_MAX=3`、间隔 1.2s、换新 worker 重开）保留为兜底；`renderer.html` 的页内重试（2 次）与 20s 加载看门狗也保留，但**只有在模型真的没加载成功时才报错**，不再假报 ready。

## 4. 实测对比（同一台机器、同一模型、每轮等 13–14s 后读 `/state`）

| 方案 | 样本 | 成功 |
| --- | --- | --- |
| `http://127.0.0.1`（原方案） | 4 | 0 |
| `http://localhost` | 3 | 1 |
| **`file://`（新默认）** | 10 | **9** |

端到端（走真实 `tool.run(action="show")`）：

```
{"ok": true, "action": "show", "model_ready": true, "visible": true,
 "count": 1, "model_name": "符玄", "_elapsed_s": 6.3}
```

修复前同一条路径：4 次尝试、92 秒、`ok:false`。
画面也做了抓屏核对（700×800 窗口区域）：符玄完整、贴图正常。

## 5. 如果以后又出现"不出现"

1. 先看 `/state`：`error` 为空但 `ready:false` 是还在加载；`error:"Aborted."` 说明异步加载链路又坏了。
2. 试 `set PET_MODEL_SCHEME=http` 再启动，能出就是 file:// 路径被环境挡住（例如模型放在没有读权限的盘）。
3. 看 `_workers\<tag>.log`（worker 的 stdout/stderr）与 `_workers\_js_console.log`（页面 console，含 `[Live2D Diagnose]` / `[load] 自动重试` / `[watchdog]`）。
4. 确认桌面没有被全屏遮罩（休息提醒之类）盖住 —— 那会让"看不见"和"没加载出来"混在一起。
