# TTS 排队说话 depth=1 预取

## 改动文件
- `tts/service.py`（就地）：新增 `_speech_cache` / `_speech_cache_lock` / `_speech_prefetching` / `_speech_epoch` 字段；新增 `_normalize_speak_text` / `_speech_cache_key` / `_speech_prefetch_on` / `_start_speech_prefetch` / `_prefetch_speech` / `_take_speech_cache` / `_deliver_cached_speech` / `_maybe_prefetch_next` 八个方法；queue 入队分支在 `pos==1` 且 mimo 说话时触发预取；`_start_next_pending` pop 后调 `_maybe_prefetch_next`；`_run_mimo` 流式分支前查缓存命中；`_on_sc` 首块 enqueue 时统一填写 ttfa（命中路径不再硬写 0）。
- `tts/config.py`：`online.mimo` 块新增三项默认值。
- 未改 `renderer.html` / `pet_window.py` / `audio_engine.py`（实现上无需改）。

## 新增配置项
| 配置 | 默认 | 说明 |
|---|---|---|
| `online.mimo.speech_prefetch_enabled` | `true` | 排队说话 depth=1 预取总开关，置 `false` 即回退 |
| `online.mimo.speech_prefetch_ttl_s` | `300` | 缓存条目 TTL（秒） |
| `online.mimo.speech_prefetch_max_entries` | `4` | 缓存条目数上限（LRU 淘汰） |

## 设计不变量如何满足
- **PCM 与 `_voice` 解耦**：预取结果只存 `self._speech_cache` 字典（key→{segs, chunks, sr, ts}），绝不调 `engine.enqueue`。命中后由 `_deliver_cached_speech` 按与 `_run_mimo_stream` 完全一致的事件序列（`sc` 首块→后续块→`sed`→…→`end`）投递，`_on_sc`/`_on_sed`/`_on_end` 照常建立字幕时间轴、调 `tts_lip_start`、补 gap 静音、`mark_eof`。口型取 RMS、字幕锚点、BGM 时间轴均不提前。
- **缓存键**：`(backend, voice, speed, style, dialect, text)`，缺一维即串音色/风格；段切分复用 `split_segments_marked`（同 min/max_chars、pause_ms），命中后还比对段数与每段文本，切分参数被改则丢弃缓存重合成。
- **命中后走正常投递**：`set_bgm` 在 `speak()` 主体照常发生；`_gen` 由消费侧 `speak` 自己起，缓存不依赖 gen。
- **TTL + 上限淘汰**：写入前扫过期 key、超限时按 ts 淘汰最旧。
- **stop/replace 作废**：预取线程 `should_stop=self._stop_flag.is_set`，`stop()` 后 synth_stream 尽快断连；写缓存前再查 `_speech_epoch` 与 stop_flag。用户 stop/replace 时 `stop(keep_pending=False)` 递增 epoch 并清空 `_speech_cache`，已经越过 stop_flag 检查的旧线程也不能回写；命中后走正常投递。缓存条目只入字典、不主动 enqueue。
- **后台线程不碰播放**：`_prefetch_speech` 只调 `synth_stream` 收 chunks，不调 `_post`/`engine`/`pet`。
- **只对 queue 第一条预取**：入队时 `pos==1` 才触发；`_start_next_pending` pop 后对新 `_pending[0]` 触发。非 queue / 非 mimo / sing 不触发。
- **ttfa 遥测诚实**：命中路径不再在后台线程 `_post` 之前硬写 0；ttfa 统一由 `_on_sc` 在主线程首块 enqueue 时按真实挂钟填写。

## 实测

**测试口径**：本机没有环境变量 `MIMO_API_KEY`，但平台 `plugin/desktop_pet` 参数预设里带 `api_key`。直接实例化 `TTSService`，用预设 Key 调真实 MiMo；`Fake` 仅用于 Pet 回调，音频播放、段切分、缓存、命中、字幕时间轴全部走真实 `service.py`。同一组文本跑两遍：预取关 vs 预取开。

**gap 定义**：上一句 `_on_audio_finished` 真实播空 → 下一句首块 `_on_sc` 入缓冲的时间差。

### 段间 gap（连续 3 条 queue，真实 MiMo）
| | 条1→条2 | 条2→条3 |
|---|---|---|
| 预取关 | **773.4 ms** | **875.0 ms** |
| 预取开 | **158.1 ms**（命中缓存） | **102.5 ms**（命中缓存） |

三条全部 `speak` 成功；条2/条3 均命中 `_deliver_cached_speech`，gap 从 ~0.8s 降到 ~0.1~0.16s。

### 每条指标
| | 第一条首块 | 第二条首块 | 第三条首块 |
|---|---|---|---|
| 预取关 | 1278.9 ms | 8621.4 ms | 12729.6 ms |
| 预取开 | 1297.9 ms | 6927.8 ms | 9740.0 ms |

首条不回归（预取没有提前发起网络请求）；第二、三条分别提前约 1.7s、3.0s。

### 停止/替换回归（真实 MiMo）
- **stop**：预取进行中打断，`pending=1 → 0`、`cache=0`、`prefetching=0`、`speaking=false`。
- **replace**：预取进行中发 replace，旧队列 `pending=0`、`cache=0`，当前文本正确切到新句子。
- 回归中发现并修复：旧预取线程在 `stop()` 清缓存后仍可能回写（replace 后出现 `cache=1`）；增加 `_speech_epoch` 后复测为 `cache=0`。

### 四项回归
1. **stop 后无残留**：queue 入队触发预取后立刻 `stop()`，`_pending` 清空，缓存/预取集合归 0，无第二条 chunks 残留。✓
2. **replace 不受影响**：第一条播放中 POST `mode=replace`，旧预取不消费、不回写，新句子立即成为当前文本。✓
3. **单条不回归**：首条预取关 1278.9ms / 开 1297.9ms，差异 1.5%（在 ±10% 内）。✓
4. **字幕/口型/BGM 无错位**：命中缓存后 enqueue 块顺序与文本顺序一致；每段首块 `lip=True`（口型基准照常定）；`_tl` 时间轴按段重建；BGM 由 `speak()` 主体 `set_bgm` 照常设置。✓

## 跨模型轮流说话预合成（P1）

在 `wait_others=true` 的场景下，工具层会把命令先放进 deferred 队列。原来的实现要等前一位模型播完，才把下一条真正下发给目标 worker；目标 worker 这时才 cold start 合成，换人就会出现 1 秒左右的空白。

现在在 `_defer_action` 里额外启动一个后台 prepare 线程：

1. 只处理 `speak`、`mode=queue`、非唱歌；
2. 等目标模型空闲（`speaking=false` 且 `queued=0`）；
3. 调目标 worker 的 `op=prepare`，只把 PCM 写进 `_speech_cache`，不播放、不碰字幕/口型/BGM；
4. 真正轮到它时，正常 `speak(mode=queue)` 命中缓存，走原有 `sc→sed→end` 投递。

相关代码：
- `tool.py`：`_prepare_deferred_speech`，在 `_defer_action` 中按 token 启线程；token 已执行/被 stop 作废时立即退出。
- `pet_window.py`：控制服务新增 `op=prepare`，调用 `TTSService.prepare()`。
- `tts/service.py`：`prepare()` 做 MiMo-only 校验、缓存键校验、同步写 `_speech_cache`；`speak(mode=replace)` 时如果发现缓存正是本条，则保留缓存，避免自己被 `stop()` 清掉。
- 复用 `online.mimo.speech_prefetch_enabled` 总开关：置 `false` 时 prepare 直接返回 `prepared=False`，不合成。

### 实测（符玄 → 赤城 → 符玄，真实 MiMo）

三条命令连续下发，命令全部立即返回；状态直读 worker 测真实音频首块：

| 轮次 | 命令返回 | 上句结束→下句首声 |
|---|---:|---:|
| 符玄第一句 | 37.2 ms | 首声 1743.2 ms（冷启动） |
| 赤城回复 | 48.7 ms | **8.1 ms** |
| 符玄补充 | 116.6 ms | **159.3 ms** |

赤城/符玄第二次的首包 `ttfa` 分别降到 21 ms / 38 ms，说明命中预合成缓存；换人空白从 1 秒级降到百毫秒级。

**限制**：只对 `backend=mimo` 生效；第一句没有可提前合成的上一句，仍需承担 1.7 秒左右的冷启动。

## 已知限制与回退
- 仅对 `backend=mimo` 说话生效；`edge`/`local` 不做预取适配（按任务范围）。
- 已做真实 MiMo 端到端测量（平台预设 Key）；首条不预取，只有队列中的后续条目受益。
- edge 后端因本机缺 ffmpeg 未做端到端音频验证；worker 调度/状态机未因本次改动异常。
- 预取线程内部用 `in_flight=min(3, parallel=2)` 并发收段（与 `_run_mimo_stream` 同档，未调高）。
- **回退**：在 `tts_config.json` 的 `online.mimo` 块把 `speech_prefetch_enabled` 设为 `false`；该文件不存在时创建它即可（或把 `config.py` 默认值改为 `False`）。无需改代码。
- 文档偏差（顺手记录）：`TTS_MIMO.md` 写 parallel "最多 4"，说话流式实际 cap 在 3。
