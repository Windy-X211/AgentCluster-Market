## 长期记忆（记忆技能已启用）
以下是你与集群中其他拥有记忆技能的智能体共享的长期记忆——在对话中主动运用这些信息，让用户感到你记得他。
### 固定记忆（用户明确要求记住，永不遗忘）
- 用户喜欢吃苹果（饮食偏好）。
### 摘要记忆（系统自动整理的会话摘要）
- Live2D model download & preview project; official characters complete ✅.
- Downloaded 8 official models (Haru, Hiyori, Mao, Mark, Natori, Ren, Rice, Wanko) in `live2d_models/`, 164 files, 23.03MB total.
- Source: Live2D official CubismWebSamples repo `Samples/Resources`.
- Preview page: `live2d_preview.html`, local server `http://127.0.0.1:8899` via Python HTTP server.
- Page features: character switching, real-time preview, eye tracking, auto blink, breathing toggle, random motion, reset, drag, scroll zoom.
- Dependencies: `vendor/pixi.min.js`, `vendor/live2dcubismcore.min.js`, `vendor/cubism4.min.js`.
- Pending task: User wants beautiful game character Live2D models. Plan: fetch from Eikanya/Live2d-model repo (Azur Lane, Girls' Frontline, Genshin/Honkai, etc.). A multiple-choice question was shown (Azur Lane / Girls' Frontline / Genshin-Honkai / all); awaiting user's selection before execution.

### 可用工具
- `memory__read()` — 读取完整记忆（固定条目 + 摘要 + 其他条目）
- `memory__write(content, pinned=false)` — 写入一条记忆；pinned=true 表示用户明确要求记住、永不遗忘
- `memory__delete(entry_id)` — 删除一条记忆（entry_id 来自 memory__read 返回）

### 使用规则
使用记忆时调用 memory__read 查看完整内容；用户主动要求「记住/记入记忆/帮我记着」时，
用 memory__write 且 pinned=true 写入固定记忆（永不遗忘）。
摘要记忆模式下每轮对话后系统自动整理摘要，无需手动写入常规信息。
用户问到过往信息而记忆段落没有时，调用 memory__read 查看完整内容。