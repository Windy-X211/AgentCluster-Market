# 在线配音 · 小米 MiMo-V2.5-TTS 接入说明

插件原来的在线配音只有 Edge-TTS（`backend=edge`）。本次新增 **MiMo** 一家，
接口与 Edge 完全同构：`backend=mimo` 即可切到 MiMo，字幕、口型、BGM、微动作、打断
全都不用改。

- 官方文档：<https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5>
- 接口：OpenAI 兼容 `POST {base_url}/chat/completions`
- 默认终点：`https://api.xiaomimimo.com/v1/chat/completions`
- 计费：官方标注"限时免费"（文档更新时间 2026-07），实际以控制台账单为准

## 1. 三步用起来

### ① 拿 key

在 MiMo 控制台创建 API Key（"首次调用 API"一节）：<https://mimo.mi.com/docs/zh-CN/quick-start>

### ② 给 key：随请求传，不落盘

**方式 A（推荐）：`api_key` 参数**

```
speak(text="这个功能我早就想试了。", backend="mimo", api_key="sk-xxxx")
```

把它写进工具参数的**预设**，就一次配好、之后每次调用自动带上。key 只活在这一个请求里：
不写 `tts_config.json`、不进 `tts_status` 快照、不写日志、不在报错文案里回显。

**方式 B：环境变量**（进程级，重启桌宠后才读到）

```
setx MIMO_API_KEY "sk-xxxx"
```

**方式 C：写进插件配置** `plugins/desktop_pet/tts_config.json`
（文件不存在就新建，只写要改的字段，其余用默认值）：

```json
{
  "online": {
    "provider": "mimo",
    "mimo": {
      "api_key": "sk-xxxx",
      "voice": "冰糖"
    }
  }
}
```

key 的查找顺序是 **调用参数 → `online.mimo.api_key` → `online.mimo.api_key_file` → 环境变量 `MIMO_API_KEY`**，
前面有就用前面的，所以参数传了就不用管配置。

`provider` 决定 `backend=auto`（以及不指定 backend 的在线调用）走谁：
`edge`（默认，免 key）或 `mimo`。写 `mimo` 就是"整体切到 MiMo"；
只传参数、不想动配置的话，保持 `provider=edge`、调用时显式 `backend=mimo` 即可。

### ③ 调用

```
speak(text="这个功能我早就想试了。", backend="mimo", voice="冰糖", api_key="sk-xxxx")
speak(text="(慵懒)再让我睡五分钟……就五分钟。", backend="mimo", voice="茉莉", api_key="sk-xxxx")
tts_status()          # 精简运行态：speaking / segments / queued / error
tts_status(detail=True)   # 全量诊断：时间轴/字幕、音频设备、口型、微动作、清单
```

状态里**不再汇报"有没有 key"**（原先的 `online.mimo.has_key` / `key_source` 已删除）：
key 允许按次携带，只查配置/环境变量得到的"有没有"必然是错的。缺 key 时
`speak` 会当场返回 `ok=false`，`error` 里写明要传 `api_key` 还是配 `MIMO_API_KEY`
（不会静默降级成别的音色，免得你以为在听 MiMo 其实是 Edge）。

## 2. 音色

| Voice ID | 性别 | 说明 |
|---|---|---|
| `冰糖` | 女 | 中文，清晰自然（默认） |
| `茉莉` | 女 | 中文，柔和细腻 |
| `苏打` | 男 | 中文，明亮偏年轻 |
| `白桦` | 男 | 中文，沉稳厚重 |
| `Mia` / `Chloe` | 女 | 英文 |
| `Milo` / `Dean` | 男 | 英文 |
| `mimo_default` | — | 集群默认音色（中国集群 = 冰糖） |

也接受拼音/小写别名（`bingtang`、`moli`、`suda`、`baihua`、`mia` …）。
不认识的名字会回落该后端默认音色，不会把错名字丢给服务端。

## 3. 风格与表演控制

MiMo 的强项在这里，分两条通道：

**自然语言指令** → 放 `role=user`（插件的 `online.mimo.style`，或每句的 `speed` 折算）

```
"style": "温柔但带着一点疲惫，语速偏慢，像深夜电台。"
```

**音频标签** → 放在要念的文本里（`role=assistant`，插件原样透传）

```
speak(text="(东北话)哎呀妈呀，这天儿也忒冷了吧！", backend="mimo")
speak(text="（紧张，深呼吸）呼……冷静，不就是一个面试吗……（小声）领带歪没歪？", backend="mimo")
speak(text="(唱歌)原谅我这一生不羁放纵爱自由……", backend="mimo", voice="冰糖")
```

> 这些标签只在 `backend=mimo` 下有效。`edge` / `local` 会把它当普通文字念出来，
> 换后端时记得把标签去掉。

**方言** → `speak(dialect="东北话")`。官方没有独立的 `dialect` API 参数，方言属于
"音频标签控制"，所以插件把它作为风格标签合并到目标文本开头（如 `(唱歌 四川话)`），
可与 `sing`、`style` 同用。工具枚举：`普通话`（默认）/ `东北话` / `四川话` / `河南话` / `粤语`。

**停顿** → MiMo 不把空格当停顿。插件只在**发给 MiMo 的请求文本**里做替换：换行→`。`，
中文句内空格→`，`（中英混排的 `打开 B 站` 不会误插逗号，英文单词间空格保留）。
字幕时间轴仍用调用方原文，所以歌词字幕不会多出标点。

## 4. 唱歌（`sing=true`）

官方文档对唱歌有硬约束，插件按这套规则实现（不是自己猜的）：

| 文档要求 | 插件怎么处理 |
|---|---|
| 只有 `mimo-v2.5-tts` 支持唱歌，`-voicedesign` / `-voiceclone` 不支持 | 配错模型直接报错，不退化成"把歌词念出来" |
| `(唱歌)` 必须在**目标文本最开头**，等价写法 `唱歌` / `sing` / `singing` | `sing=true` 时自动补到最开头；开头已有风格标签就合并成 `(慵懒 唱歌)`，不叠两个括号 |
| 标签要放在 `role=assistant` 的 content 里 | 目标文本本来就放 assistant，天然满足 |
| 歌词建议中文 | 中文效果更稳（英文也能唱，只是不如中文稳） |
| 半角 `()`、全角 `（）`、方括号 `[]` 都认 | 三种都解析，不会因为写成全角就漏判 |

```
speak(text="原谅我这一生不羁放纵爱自由，也会怕有一天会跌倒……", backend="mimo", sing=true, voice="冰糖")
speak(text="月亮代表我的心", backend="mimo", sing=true, api_key="sk-xxxx")
```

### 唱歌和平时说话的两处行为差异

1. **唱歌整曲一次合成（默认不拆句），说话才是流式。** 说话走流式：服务端边算边下发
   pcm16 块，插件收到一块就入队一块，40 字首块实测 1.3 秒出声。唱歌**不能用**流式：
   实测 `stream=true`+`pcm16` 只回 1 块、首块时间等于总耗时，等于没流式。
   而唱歌是"整段推理"，实测 **0.29~0.37 秒/字**，所以整曲一次要等（49 字约 15 秒出首声）。

   为什么不再按句拆：MiMo 每次请求都是独立推理，看不到上一句唱到哪、停在哪个音高。
   实测同一段 4 句 49 字歌词：整曲一次内部帧间音高变化中位仅 **0.09 半音**（p95 0.75），
   说明引擎/采样率没问题；而拆 3 段时第 3 段整段低了 **5.50 半音**、两个接缝跳变
   **+4.42 / −16.33 半音**；拆 4 段是 −0.42 / +4.20 / −2.88；两行一段是 +3.46。
   结论：**段数 = 起唱次数 = 跑调机会数**，所以默认 `sing_split_chars=0`。

   | 配置（`tts_config.json` → `online.mimo`） | 默认 | 作用 |
   |---|---|---|
   | `sing_split_chars` | **0** | 0 = 整曲一次（旋律连贯、首声最慢）；>0 才按歌词行拆段 |
   | `sing_first_chars` | 20 | 拆段时才用：首段再压到这个字数以内 |
   | `sing_ms_per_char` | 300 | 唱歌进度气泡的估算速率（毫秒/字），只影响显示，不参与合成 |
   | `sing_wait_bgm` | `true` | 等待期播背景音（唱歌一开口就淡出，不叠在旋律上） |
   | `sing_wait_scene` | `ambient` | 等待期用哪个内置 BGM 场景（piano/ambient/cafe/night/nature/bright） |
   | `sing_wait_volume` | 0.55 | 等待期 BGM 音量（低于正常配音 0.85，别盖住"清嗓子"那句） |
   | `sing_wait_voice` | `true` | 是否播"清嗓子"配音（读 `_tts/sing_wait/<音色>.wav` 缓存） |
   | `sing_wait_line` | 见配置 | 等待台词，只用于烘焙缓存（`tools/bake_sing_wait.py`），不参与请求 |
   | `stream` | `true` | 说话是否流式（唱歌恒定非流式） |
   | `parallel` | 2 | 说话流式时"同时进行中的段数"，后段提前发，把服务端排队藏在播放里 |

   代价是每段会各自"起唱"，跨段旋律不保证连续；要旋律完整连贯就设 `sing_split_chars=0`，
   用"慢"换"连着唱"。

   **十几秒的等待要有反馈：气泡 + 背景音 + 一句"清嗓子"。** MiMo 唱歌不返回任何中间进度
   （流式也只回 1 块），所以插件自己造三层反馈：

   1. 桌宠头顶气泡：开始合成时投 `prg` 事件，主线程 300ms QTimer 按
      "已过时间 ÷ `sing_ms_per_char`"折算百分比，写到 `LoadingBubble`（纯文字 + 底部
      进度条，和"模型加载中"共用气泡但进度模式不画省略号）：`清嗓酝酿中 28%` →
      `69%` → … 第一段音频入队时收起。实测 50 字：4.4s 显示 27%、10.3s 显示 67%、
      13.3s 出第一声并收起。
   2. 等待背景音：`waitp` 事件让主线程 `set_bgm(等待场景, mode="always")`。用 `always`
      而不是默认 `speech` 是必须的——人声没到时设备会被判"欠载"而 suspend，音乐跟着停；
      `always` 下"只有音乐"也算有料可播（`_start_if_possible` 为此放行、`starved` 判定
      也把这种情形排除），实测 6 秒纯音乐 `bgm_played` 与挂钟 1:1，0 次 suspend。
   3. 等待配音：`_tts/sing_wait/<音色>.wav`（默认 4.83s："嗯……我先清一下嗓子，稍等一会儿，
      马上就唱给你听。"）。**为什么烘焙成文件而不是实时合成**：实时合成要么再排一次服务端
      队列、要么用本地引擎冷启动几十秒，两者都会拖慢真正的唱歌请求；烘焙文件读盘即播，
      且直接垫在人声采样 0 位置——实测歌段 `start=4829ms`，正好接在 4.83s 的等待语音之后。
      换音色后用 `tools/bake_sing_wait.py --voice 茉莉 --force` 重做一次；没有缓存时
      自动退化成"只放背景音"，不会报错。
      歌一开口（第一段 `seg`）就 `fade_out_bgm(600ms)` 收掉背景音、气泡同步收起，
      不在旋律上叠音乐；`stop()` / 出错也会收。

   估算值只决定"看起来的百分比"，不会写进状态或字幕；`_prog_timer` 是主线程
   定时器，worker 只投事件（Qt 定时器不能跨线程启停）。
2. **`speed` 被忽略。** 旋律自带节奏，再塞"语速快约 30%"只会跟演唱打架。
   `style`（自然语言风格）仍然生效，可以写"深情一些""轻快一点"。

字幕依旧是按字数估算（MiMo 不返回逐字时间戳），每句歌词会均分到自己那段的时长上。
说话流式时首块到货还没有真实时长，先按"字数 × `stream_est_ms_per_char`(260ms)"估一个，
这段收完再按真实样本数修正 `end_ms` 与逐字 marks。

也可以不用 `sing` 参数，直接写 `(唱歌)歌词` —— 效果一样；两种写法下 `(唱歌)` 标签都只用于
合成请求，**不会出现在字幕里**。

### 把自然语言控制开放给智能体：`speak(style=...)`

MiMo 的风格控制是"自然语言指令"，插件把它做成 `speak` 的一个参数，智能体可以按次直接写：

```
speak(text="今天加班到十点，真的有点累了。", backend="mimo", style="温柔但疲惫，尾音放轻，语速偏慢")
speak(text="报告出来了，业绩翻倍！", backend="mimo", style="兴奋、语速偏快、尾音上扬")
speak(text="天青色等烟雨，而我在等你", backend="mimo", sing=True, style="温柔，略带气声")
```

- `style` 写进 `role=user` 的 content（和 `online.mimo.style` 同一个位置），**不会**出现在字幕，
  也不会被念出来（TTS 只念 assistant 里的正文）。
- **按次优先级**：`speak(style=...)` 在前，配置里的 `online.mimo.style` 在后，两句都带上；
  完全相同则去重。两者都不写时，user 消息里只剩语速指令（说话）或空（唱歌）。
- 支持"角色 / 场景 / 指导"三段式导演模式（详见 MiMo 文档），也支持文内 `(风格)`、`[音频标签]`。
- 不支持后期处理词（混响、EQ、压缩），也别写相互矛盾的特征（"稚嫩童声 + CEO 气场"）。
- 只对 `backend=mimo`（或 auto 落到 MiMo）生效；local / edge 会忽略这个参数。

## 5. 语速

MiMo **没有官方 `rate` 参数**。插件的 `speed` 会折成一句中文指令塞进 user 消息，
例如 `speed=1.3` → `"语速比正常快约 30%，吐字保持清晰。"`，属于**近似控制**，
不保证精确倍数。要"抖音式精确变速"请用 `backend=edge`（云端 rate 百分比）；
`online.mimo.speed_hint=false` 可关掉这段指令。

## 6. 配置项（`tts_config.json` → `online.mimo`）

| 键 | 默认 | 说明 |
|---|---|---|
| `base_url` | `https://api.xiaomimimo.com/v1` | 接口终点，换集群/代理网关时改这里 |
| `model` | `mimo-v2.5-tts` | 另有 `-voicedesign`（文本设计音色）、`-voiceclone`（音频复刻，`voice` 传 `data:audio/...;base64,...`） |
| `voice` | `冰糖` | 预置音色 ID；`backend=mimo` 不传 `voice` 时用它 |
| `api_key` | 空 | 配置里的兜底 key；**调用参数 `api_key` 优先于它**，一般不用写 |
| `api_key_env` | `MIMO_API_KEY` | 从哪个环境变量读 key |
| `api_key_file` | 空 | 也可以指向一个只存 key 的文本文件 |
| `style` | 空 | 默认自然语言风格指令（写进 user 消息）；`speak(style=...)` 可按次覆盖/追加 |
| `voice_design` | 空 | `model=-voicedesign` 时的音色描述兜底 |
| `speed_hint` | `true` | 是否把 `speed` 折成语速指令 |
| `trim_silence` | `true` | 裁掉回包首尾静音（提前首声、句间不多一拍） |
| `parallel` | `2` | 说话流式时同时进行中的段数（最多 4）——后段提前发，服务端排队被藏在播放里；唱歌拆段时是并发预取数 |
| `stream` | `true` | 说话走流式（`stream=true` + `pcm16`，首块即出声）；唱歌恒定非流式 |
| `stream_est_ms_per_char` | `260` | 流式首块到货时字幕的预估毫秒/字（收完按真实样本数修正） |
| `timeout_s` | `30` | 单句请求超时 |
| `sample_rate` | `48000` | 送给播放引擎的采样率（回包 24k，内部会重采样） |
| `format` | `wav` | 回包格式；流式固定 `pcm16`（`wav` 是整包 RIFF，没法边拼边播） |
| `optimize_text_preview` | `false` | 仅 `voicedesign`：让服务端先润色播报文本 |
| `proxy` | 空 | 给 MiMo 单独配代理（默认继承 `online.proxy`） |

`online.provider`、`online.proxy`、`online.api_key_env` 仍放在 `online` 这一层。

## 7. 实现要点（改代码时看这里）

| 环节 | 文件 |
|---|---|
| HTTP / 解码 / 音色归一 | `tts/engine_mimo.py`（只用标准库 + numpy，没有新增第三方依赖） |
| 逐句调度、并发预取、事件投递 | `tts/service.py` → `_run_mimo()`（非流式，唱歌走这条） |
| 说话流式投递 | `tts/service.py` → `_run_mimo_stream()` + `_on_sc()` / `_on_sed()` / `_tl_append()` / `_tl_refresh()` |
| 流式 HTTP（SSE + pcm16） | `tts/engine_mimo.py` → `synth_stream()` |
| backend 归一（auto/online→provider） | `tts/service.py` → `_normalize_backend()` |
| 音色清单与默认值 | `tts/service.py` → `MIMO_VOICES` / `_voice_for("mimo")` |
| 配置默认值 | `tts/config.py` → `online.mimo` / `voice.mimo` |
| 工具层白名单 | `tool.py` → `_cmd_speak()` / `_cmd_tts_config()` |
| 调参说明 | `tool.yaml` → `backend` / `voice` / `speed` / `text` / `sing` / `style` |
| 试听脚本 | `tools/tts_probe.py --backend mimo` |
| 唱歌：标签归一 / 标签剥离 | `tts/engine_mimo.py` → `_lead_tag()` / `is_singing()` / `ensure_sing_tag()` / `strip_sing_tag()` |
| 唱歌：剥停顿符号 | `tts/segmenter_marks.py` → `strip_pause_marks()` |
| 唱歌：拆句与首段压短 | `tts/service.py` → `_sing_segments()` / `_run_mimo()` 的 `singing` 分支 |
| 播放：峰值余量 | `tts/engine_mimo.py` → `_limit_peak()`（默认压到 88% 满量程） |
| 唱歌进度气泡 | `tts/service.py` → `_run_mimo()` 投 `prg` + `_on_prog()` / `_prog_tick()` / `_prog_hide()`；`pet_window.py` → `PetWidget.tts_progress()` / `LoadingBubble.show_progress()` |
| 唱歌等待垫场 | `tts/service.py` → `_run_mimo()` 投 `waitp`、`_on_waitp()` / `_load_wait_pcm()` / `_wait_line_path()` / `_stop_wait_bgm()`；`tts/audio_engine.py` → `fade_out_bgm()` + `_start_if_possible` 的 `music_only` + `_on_tick` 的 `keep_music`；`tools/bake_sing_wait.py` 烘焙 `_tts/sing_wait/<音色>.wav` |

关键实现约定：

1. **目标文本必须放 `role=assistant`**，放 user 里只会被当成指令（官方明确要求）。
2. **没有逐字时间戳**：`synth_segment` 的 `words` 返回 `None`，字幕按"字数 + 标点权重"
   均分（`tts/marks.py`），和 Edge 缺 WordBoundary 时的行为一致。
3. **按句请求 + 按序投递**：并发只用来提前算后面的句子，投递顺序必须严格按分段下标，
   否则字幕和声音会错位。
4. **静音在切句处补**：与 Edge 相同，段间隔音由 `segmenter_marks` 决定（符号先剥掉）。
5. **不静默降级**：没 key / 401 / 网络不通都可能直接报错到字幕，错误文案里带修复方向。
6. **key 走调用参数**：`speak(..., api_key=...)` → `service.speak(api_key=...)` →
   `_run_mimo(api_key=...)` → `engine_mimo.synth_segment(api_key=...)` → `Authorization: Bearer`。
   全程只在内存里传递一次，不落盘、不进状态快照、不写日志；配置与环境变量只做兜底。
7. **唱歌先剥符号再拆句**：`sing` 一旦成立（参数或文本开头的 `(唱歌)`），`_run_mimo` 先
   `strip_pause_marks` + `strip_sing_tag` 拿到"干净歌词"（`~`/`|`/`||`/`|||` 与 `(唱歌)`
   都不进请求、不进字幕），再按 `sing_split_chars` / `sing_first_chars` 拆段；模型不是
   `mimo-v2.5-tts` 时在 `_build_payload` 里抛 `MimoTTSError`（报错文案带修法），
   不让它悄悄念歌词。
8. **输出峰值留余量**：MiMo 唱歌回包峰值实测恒为 32767，与 BGM 相加必然削波（听感是刺耳
   噪声），所以解码后统一压到 `online.mimo.headroom`（默认 0.88）以内，只衰减不放大。
9. **播放侧三条硬约束**（`tts/audio_engine.py`，改这块时别退回去）：
   - 每个 tick 要把设备缓冲填到水位（上限 4 块 ≈ 40ms），不能只写 10ms：Windows 定时器有
     1~15ms 抖动、主线程还要跑 WebEngine 帧回传，实测主线程占 35% 时"只写 10ms"会让设备
     只播 8.99s/10s（听感就是一顿一顿 + 滋滋响）。
   - 段首/段尾各 4ms 淡入淡出（`_declick`）：MiMo 会裁掉首尾静音，两段非零样本硬接就有"啪"
     的爆音。
   - 欠载要挂起设备时给 250ms 宽限：否则每个"下一段还在合成"的段间等待都走一趟
     suspend/resume，接缝处必定爆一下。**段接缝爆音 + 欠载，是用户说的"磁噪音"的主要来源。**
10. **软限幅在混音末端**：`_soft_limit`（阈值 0.90，`tanh` 压到 ±1 内）替掉硬 `clip`，
    人声 + BGM 相加超限时不再削出方波。
11. **唱歌的分段边界：先按"行"，再在行内按标点/字数**。`_run_mimo` 在原文上切好行
    （`strip_pause_marks` 会把换行抹成空格，所以必须在剥符号之前切），传给
    `_sing_segments(..., lines=...)`；`_sing_segments` 对每行独立分段，行与行之间至少
    留一个中停。**不要在跨行的地方按字数硬切** —— 实测整曲一次 49 字 13.73s，按 28 字
    打包的旧策略会切出 20/13/16，"炊烟袅袅|升起"这种半句就是那么来的。
    实测跑调规律（`_tts\sing_compare`，歌词 4 句 49 字）：整曲一次内部帧间音高变化中位
    0.09 半音（平滑），而分段版每个接缝都是一次重新起调，旧策略第 3 段整段低了 5.5 半音、
    接缝跳变 -16.3 半音。**段数 = 起唱次数 = 跑调机会数**，要旋律连贯只能用
    `sing_split_chars=0` 退回整曲一次（代价：首声 15.5s/49 字，100 字约 29s）。

12. **说话流式的四条约定**（`_run_mimo_stream` + `_on_sc`/`_on_sed`）：
    - 流式只用于说话：`_run_mimo` 在 `singing=False 且 st["stream"]=True` 时提前转
      `_run_mimo_stream`；唱歌恒走整包 `synth_segment`（实测唱歌流式只回 1 块）。
    - 块按段序投递：worker 线程只把块塞进每段自己的 `queue.Queue`，由投递线程统一
      `_post("sc", meta, pcm)`，字幕时间轴才不会因为"谁先算完"错位。
    - `enqueue(declick=False, lip=False)`：每块都做 4ms 淡入淡出会在 320ms 块边界挖出
      空隙（听感"喘气"）；口型响度基准只按每段首块定，否则嘴型跟着抖。淡入由首块自己做
      （`fade=96` 样本 @24k），流结束若末样本 |x|>40 再补一段 4ms 降音，防硬切爆音。
    - 后段预取：当前段一开始就把后面 `parallel`（默认 2）段并发发出去，块先收进内存。
      实测首块延迟在 0.73~7.66 秒之间随机抖动（服务端排队，不是限速），串行时碰上 7 秒
      排队就会在上一段播完时断腔；预取把这段等待藏在播放里。
13. **字幕时长两段式**：流式首块到货时还没有真实时长，`_tl_append(text, start, est, 0)`
    先按 `字数 × stream_est_ms_per_char(260ms)` 估；每块累加真实样本数（`_on_sc` 把
    `end_ms` 往上顶，避免"剩余时间"变负）；段结束 `_on_sed` 用 `_tl_refresh` 按真实
    长度重算 `end_ms` 与逐字 marks，并补 `gap_ms` 静音。
14. **唱歌进度气泡（`prg` 事件）**：MiMo 唱歌不返回任何中间进度，`_run_mimo` 在开始
    第一段合成前投 `prg{chars, total_chars, ms_per_char, t0, text="清嗓酝酿中"}`；
    `_on_prog` 在**主线程**（`_drain` 里）起 `_prog_timer`（300ms），`_prog_tick` 用
    `(done_chars + min(chars, 已过毫秒/ms_per_char)) / total_chars` 算百分比，上限 0.99，
    调 `pet.tts_progress("清嗓酝酿中", ratio)`；第一段 `seg` 到达、`end`、`err` 都调
    `_prog_hide()` 收起。`stop()` 只清 `_prog`（HTTP 线程里不能停 Qt 定时器），由下一次
    tick 收尾。气泡复用 `LoadingBubble`：`_dots=False` 时不画省略号，`_ratio` 非空时
    在底部画一条圆角进度条（150×58，进度模式下已显示就不再重新淡入，避免每 300ms 闪一下）。
15. **唱歌等待垫场（`waitp` 事件 + 引擎三条改动）**：`_run_mimo` 在唱歌开始前先投
    `waitp{bgm_on, scene, volume, voice_on, voice}`，`_on_waitp`（主线程）：
    - `set_bgm(scene, volume=sing_wait_volume, mode="always", preroll_ms=0, tail_ms=0)`；
    - `_load_wait_pcm(voice)` 读 `_tts/sing_wait/<音色>.wav`，`enqueue(..., lead_ms=0, lip=True)`
      直接垫在采样 0 位置（不进 `_tl`、没有字幕），歌段自然排在它后面；
    - 歌开口：`_on_seg` 里 `if self._singing: _stop_wait_bgm()` → `fade_out_bgm(600)`，
      淡出播完才真正 `_bgm=None`；`stop()` / `_on_err` / `_on_end` 同样收掉。
    引擎侧必须同步的三处（不然"只有音乐"时根本没声）：
    - `_start_if_possible`：`mode="always"` 的 BGM 自己算可播内容，没到 prebuffer 也要起播；
    - `_on_tick` 欠载判定：`voice_empty` 与 `starved` 拆开——`always` 音乐在响时不算欠载，
      否则 8ms 一轮 suspend/resume，音乐一顿一顿；收工判断仍用 `voice_empty`；
    - `fade_out_bgm(ms)`：新增的线性淡出，替代硬切 `set_bgm(None)`。
    调用方自己传了 `bgm` 时不覆盖（`self._user_bgm`），也不淡出，尊重原设置。

## 8. 自检（不需要起桌宠）

```bat
cd /d E:\appdata\AgentCluster\plugins\desktop_pet
_tts_env\Scripts\python.exe -m tts.engine_mimo --status
_tts_env\Scripts\python.exe -m tts.engine_mimo --api-key sk-xxxx --text "你好，我是桌宠。" --out mimo_test.wav
```

`--status` 只打印配置（key 只报"有没有"、来自 config/file/env 哪一份）；第二个命令用
`--api-key` 临时带 key 真发一次请求并落盘 wav，不需要动任何文件。

带字幕/播放/报告的完整体检：

```bat
_tts_env\Scripts\python.exe tools\tts_probe.py --backend mimo --api-key sk-xxxx --text "今天天气不错。" --out _tts\probe_mimo
```

唱歌单独验一次（长歌词会拆句合成，输出目录里会有 wav 与字幕快照）：

```bat
_tts_env\Scripts\python.exe -m tts.engine_mimo --api-key sk-xxxx --sing --text "原谅我这一生不羁放纵爱自由" --out mimo_sing.wav
_tts_env\Scripts\python.exe tools\tts_probe.py --backend mimo --sing --api-key sk-xxxx --text "月亮代表我的心" --out _tts\probe_sing --frames 2
```

## 9. 常见报错

| 现象 | 原因 / 处理 |
|---|---|
| `MiMo 鉴权失败(401)` | key 没传或写错。参数预设里的 `api_key` 是否为空/带空格；走环境变量则要**重启桌宠进程**才读到 |
| `MiMo 接口不存在(404)` | `base_url` 多了/少了路径（默认要到 `/v1`），或 `model` 拼错 |
| `MiMo 限流(429)` | 请求太密。把 `online.mimo.parallel` 降到 1 |
| `MiMo 网络不可达` | 需要能访问 `api.xiaomimimo.com`，必要时配 `online.mimo.proxy` |
| `MiMo 请求超时` | 网络慢或句子太长；可调 `timeout_s`，或把 `local.max_chars` 调小一点 |
| 字幕比声音快/慢 | MiMo 没有逐字时间戳，字幕是均分估算；要精准请用 `backend=edge` |
| 语音里念出了 `(开心)` | 用了 `backend=edge/local`。这些标签只对 MiMo 有效，且要放在句首 |
