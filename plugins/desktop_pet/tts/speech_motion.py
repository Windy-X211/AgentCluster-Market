# coding=utf-8
"""说话时的程序化肢体微动作（不依赖模型自带动作文件）。

背景：`assets\符玄` 这类模型 `Motions = 0`、`Expressions = 0`，靠播动画做不出
"说话时身体有反应"。而 Cubism 物理（physics3.json）把 `ParamAngleX/Y/Z`、
`ParamBodyAngleX/Y/Z`、`ParamBreath` 当**输入**，写这些参数就能让头发、衣摆自动跟着摆。

设计约束（踩过的坑）：
1. 只产出「偏移量」，由 pet_window 叠加到鼠标跟随的目标值上 —— 绝不直接写头部参数，
   否则每帧的跟随插值会把动作抹平；
2. 只发的参数在模型里不存在时，由 renderer 的 applyParam 统一吞掉，
   因此换任何 Live2D 模型都不会报错、不会乱动；
3. 幅度单位是"度"（Cubism 角度参数是 -30~30），超过 ±6° 就会显得假；
4. 随机用固定种子：同一句台词每次微动一致，便于复现与验收。
"""

from __future__ import annotations

import math
import random
from typing import Dict, List

# 默认档：头部起伏 ±6~7.5°，能明显看出"在说话"，又不至于抽搐
# （物理自带的待机摆动 ±2~3°，微动作必须比它大才看得出来）
AMP_NOD = 6.0          # 每次点头的幅度（度）
# 段内固定落点：只留很小的偏置，靠 ROCK 的往复提供"活着"的感觉。
# 原来 6.0/4.0 意味着整句台词头都压在某一侧，看起来像"歪着头说话"，
# 而且同一句每次都压同一侧 —— 用户看到的就是「一开口头就歪一边」。
AMP_SWAY_X = 2.0       # 段内左右转头的固定落点
AMP_SWAY_Z = 1.5       # 段内歪头的固定落点
AMP_STANCE = 5.0       # 换句子时的身体重心（ParamBodyAngleX）
AMP_ROCK_X = 3.4       # 句内持续轻摆（左右），叠加在固定落点上 → 头一直在动
AMP_ROCK_Z = 2.2       # 句内持续轻摆（歪头）
ROCK_S = 3.4           # 轻摆周期（秒）
AMP_BREATH = 0.35      # ParamBreath 振幅（0~1 参数）
AMP_TREMOR_X = 0.9     # 句内细摆（左右），让头不是死的
AMP_TREMOR_Z = 0.6     # 句内细摆（歪头）
TREMOR_HZ = 0.55
BREATH_PERIOD_S = 3.6

NOD_MS = 480            # 点头一次的总时长
SWAY_MS = 1200          # 段内转头到位时间
STANCE_MS = 2000        # 换站姿时间
RELAX_MS = 600          # 说完回到基准

# 换句子/换站姿的判断：句间静音够长，或已经连了 3 段
STANCE_GAP_MS = 500
STANCE_EVERY = 3


class SpeechMotion(object):
    def __init__(self, seed: int = 20261114, gain: float = 1.0,
                 nod_every_ms: int = 2200):
        self._rng = random.Random(seed)
        # 幅度总开关：1.0 = 默认档；配置 motion_gain 可整体放大/收敛
        try:
            self._gain = max(0.2, min(3.0, float(gain)))
        except Exception:
            self._gain = 1.0
        # 句内自动点头的间隔（只在说话时生效），带随机抖动避免机械
        try:
            self._nod_every_ms = max(800, int(nod_every_ms))
        except Exception:
            self._nod_every_ms = 2200
        self._next_nod = -1.0
        self._t0 = 0.0
        self._active = False
        self._seg_i = -1
        self._nod_at = -1.0
        self._sway = (0.0, 0.0)          # (X, Z) 段内目标
        self._sway_from = (0.0, 0.0)
        self._sway_since = None
        self._stance = 0.0
        self._stance_from = 0.0
        self._stance_since = None
        self._relax_at = -1.0
        self._pause_until = -1.0
        self._utt = 0                    # 第几次开口（跨句累积，决定左右交替相位）
        self._out: Dict[str, float] = {}

    # ---------------------------------------------------------------- 生命周期
    def start(self, now: float) -> None:
        self._t0 = now
        self._active = True
        self._seg_i = -1
        # 每条台词 _seg_i 都从 0 重新数。只用 _seg_i 判断左右的话，单段台词永远
        # 走同一个方向 → 每次开口都往同一边歪头。这里跨台词累积一个序号参与相位。
        self._utt += 1
        self._nod_at = -1.0
        self._sway = (0.0, 0.0)
        self._sway_from = (0.0, 0.0)
        self._stance = 0.0
        self._stance_from = 0.0
        self._relax_at = -1.0
        self._pause_until = -1.0
        self._next_nod = now + self._nod_every_ms / 1000.0 * 0.9
        self._rng = random.Random(20261114)

    def stop(self, now: float) -> None:
        """说完：不立刻清零，给 RELAX_MS 缓出（突然复位会像"定格"）。"""
        if not self._active:
            return
        self._relax_at = now
        self._active = False
        self._pause_until = -1.0

    def on_segment(self, index: int, gap_after_ms: int = 0, now: float = 0.0) -> None:
        """每段开始调用一次：段落起点点头；句子层面换一次站姿。"""
        self._seg_i = int(index)
        self._nod_at = now
        phase = self._seg_i + self._utt          # 段序 + 台词序：跨句也能左右交替
        rng = random.Random(20261114 + phase * 7919)
        # 幅度带最小绝对值的随机：全随机到 0.1° 就跟没动一样。
        # 方向按相位交替，避免每句都往同一边偏（固定种子下否则整段配音都在左侧）。
        sgn_x = 1.0 if (phase % 2 == 0) else -1.0
        sgn_z = -1.0 if (phase % 2 == 0) else 1.0
        sx = sgn_x * rng.uniform(0.55, 1.0) * AMP_SWAY_X * self._gain
        sz = sgn_z * rng.uniform(0.55, 1.0) * AMP_SWAY_Z * self._gain
        self._sway_from = self._sway_value(now)
        self._sway = (sx, sz)
        self._sway_since = now
        # 下一处句内点头：段落起点已经点了，这里从"半拍后"开始轮流点
        self._next_nod = now + self._nod_every_ms / 1000.0 * 0.9
        # 只在"句子边界"换站姿：开头先站定一个重心，之后句间静音够长或每 3 段一次。
        # 只在 gap 触发的话，三句话的短配音从头到尾身体不动，会显得只有头在动。
        if (int(gap_after_ms) >= STANCE_GAP_MS or self._seg_i == 0
                or (self._seg_i > 0 and self._seg_i % STANCE_EVERY == 0)):
            self._stance_from = self._stance_value(now)
            st_sign = 1.0 if (phase % 2 == 0) else -1.0
            self._stance = st_sign * rng.uniform(0.6, 1.0) * AMP_STANCE * self._gain
            self._stance_since = now

    # ---------------------------------------------------------------- 取值
    def _sway_value(self, now: float) -> tuple:
        if self._sway_since is None:
            return self._sway
        k = _ease((now - self._sway_since) * 1000.0 / SWAY_MS)
        return (self._sway_from[0] + (self._sway[0] - self._sway_from[0]) * k,
                self._sway_from[1] + (self._sway[1] - self._sway_from[1]) * k)

    def _stance_value(self, now: float) -> float:
        if self._stance_since is None:
            return self._stance
        k = _ease((now - self._stance_since) * 1000.0 / STANCE_MS)
        return self._stance_from + (self._stance - self._stance_from) * k

    def tick(self, now: float) -> Dict[str, float]:
        """返回这一帧该叠加到跟随目标上的偏移量（单位：参数原生单位）。"""
        out = self._out
        out.clear()

        # 台词里指定了内置动画：那段时间让位，避免"点头动画 + 微动作转头上"打架
        if self._active and now < self._pause_until:
            return out

        # L0 呼吸：说话期间一直有，收尾时随其它动作一起淡出
        if self._active or self._relax_at >= 0:
            out["ParamBreath"] = round(
                0.5 + AMP_BREATH * math.sin(2 * math.pi * (now - self._t0) / BREATH_PERIOD_S), 3)

        if not self._active:
            # 缓出：所有动作按 RELAX_MS 线性回零
            if self._relax_at >= 0:
                k = max(0.0, 1.0 - (now - self._relax_at) * 1000.0 / RELAX_MS)
                sway = self._sway
                out["ParamAngleX"] = round(sway[0] * k, 2)
                out["ParamAngleZ"] = round(sway[1] * k, 2)
                # 没在点头时必须显式发 0：setTrackTarget 是"整批替换目标"，
                # 停发某个参数它就会冻结在上一次的值（Y 会一直低着）。
                out["ParamAngleY"] = 0
                out["ParamBodyAngleX"] = round(self._stance * k, 2)
                out["ParamBodyAngleZ"] = 0
                # 呼吸也要淡回 0（ParamBreath 的中性值是 0，停在这里会一直微鼓）
                out["ParamBreath"] = round(
                    (0.5 + AMP_BREATH * math.sin(
                        2 * math.pi * (now - self._t0) / BREATH_PERIOD_S)) * k, 3)
                if k <= 0.0:
                    self._relax_at = -1.0
            return out

        # L1 句内自动点头：每隔 nod_every 秒点一次（抖动 ±，避免节拍器感）。
        # 段落起点那次点头由 on_segment 触发，这里只补句内剩下的部分。
        if self._next_nod >= 0 and now >= self._next_nod and self._nod_at < 0:
            self._nod_at = now
            self._next_nod = now + (self._nod_every_ms / 1000.0
                                    * self._rng.uniform(0.75, 1.35))
        # L1 单次点头：sin 单峰，起落都平滑；没点头时显式发 0（否则会冻结在上一帧）
        out["ParamAngleY"] = 0
        if self._nod_at >= 0:
            p = (now - self._nod_at) * 1000.0 / NOD_MS
            if p >= 1.0:
                self._nod_at = -1.0
            else:
                out["ParamAngleY"] = round(
                    -AMP_NOD * self._gain * math.sin(math.pi * max(0.0, p)), 2)

        # L1 段内转头/歪头（缓到目标）+ 句内持续轻摆 + 细摆。
        # 只有"固定落点"时，1.2s 到位后头就不动了 —— 短句看起来像只有物理在动，
        # 所以叠一层慢摆（周期 ROCK_S），保证说话期间头始终有可感知的往复。
        sway = self._sway_value(now)
        rp = 2 * math.pi * (now - self._t0) / ROCK_S
        ph = 2 * math.pi * TREMOR_HZ * (now - self._t0)
        out["ParamAngleX"] = round(
            sway[0] + self._gain * (AMP_ROCK_X * math.sin(rp)
                                    + AMP_TREMOR_X * math.sin(ph)), 2)
        out["ParamAngleZ"] = round(
            sway[1] + self._gain * (AMP_ROCK_Z * math.sin(rp + 1.6)
                                    + AMP_TREMOR_Z * math.sin(ph + 1.1)), 2)
        # L2 句子层面的重心（缓变）+ 随身体轻摆
        out["ParamBodyAngleX"] = round(self._stance_value(now), 2)
        out["ParamBodyAngleZ"] = round(
            self._gain * 1.4 * math.sin(rp + 0.4), 2)
        return out

    def describe(self) -> Dict[str, object]:
        return {"active": self._active, "seg": self._seg_i,
                "sway": [round(v, 2) for v in self._sway], "nod": self._nod_at >= 0,
                "gain": self._gain}

    def pause(self, now: float, dur_ms: int = 950) -> None:
        """让位给台词里点名的内置动画（约一个动作的时长），期间不输出偏移量。"""
        self._pause_until = max(self._pause_until, now + max(0, dur_ms) / 1000.0)

    def is_idle(self) -> bool:
        """说话结束且缓出也做完了 → 调用方可以停掉 50ms 心跳。"""
        return (not self._active) and self._relax_at < 0


def _ease(p: float) -> float:
    """0→1 的平滑曲线（easeInOutQuad），到位不生硬。"""
    p = max(0.0, min(1.0, p))
    return p * p * (3 - 2 * p)
