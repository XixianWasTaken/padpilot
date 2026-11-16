"""Lightbar animations and rumble pattern playback (pure functions of time)."""
from __future__ import annotations

import colorsys
import math
from typing import List, Optional, Sequence, Tuple

RGB = Tuple[int, int, int]


def scale(c: Sequence[float], k: float) -> RGB:
    return tuple(max(0, min(255, int(round(v * k)))) for v in c)  # type: ignore[return-value]


def mix(a: Sequence[float], b: Sequence[float], t: float) -> RGB:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def light_at(spec: dict, t: float) -> RGB:
    """Color of light spec at time t (seconds since the state began)."""
    kind = spec.get("kind", "solid")
    color = spec.get("color", [0, 0, 0])
    if kind == "breathe":
        period = max(0.2, float(spec.get("period", 3.0)))
        lo = float(spec.get("min", 0.1))
        # Start bright so a state change is visible immediately.
        k = lo + (1 - lo) * (0.5 + 0.5 * math.cos(2 * math.pi * t / period))
        return scale(color, k)
    if kind == "blink":
        period = max(0.1, float(spec.get("period", 1.0)))
        return scale(color, 1.0 if (t % period) < period / 2 else 0.08)
    if kind == "fade":  # slow cross-fade between color and color2
        period = max(0.2, float(spec.get("period", 3.0)))
        k = 0.5 - 0.5 * math.cos(2 * math.pi * t / period)
        return mix(color, spec.get("color2", [0, 0, 0]), k)
    if kind == "rainbow":
        period = max(0.5, float(spec.get("period", 3.0)))
        r, g, b = colorsys.hsv_to_rgb((t / period) % 1.0, 1.0, 1.0)
        return scale((r, g, b), 255)
    return scale(color, 1.0)


class Flash:
    """A short color overlay that fades out. kind "rainbow" cycles hues, then fades."""

    def __init__(self, color: Sequence[int], ms: float, start: float, kind: str = "solid"):
        self.color = color
        self.dur = max(0.02, ms / 1000.0)
        self.start = start
        self.kind = kind

    def apply(self, base: RGB, now: float) -> Optional[RGB]:
        p = (now - self.start) / self.dur
        if p >= 1:
            return None
        if self.kind == "rainbow":
            c = light_at({"kind": "rainbow", "period": 0.9}, now - self.start)
            return mix(c, base, max(0.0, (p - 0.7) / 0.3))
        return mix(self.color, base, p * p)


class RumblePlayer:
    """Plays one pattern at a time; a new pattern replaces the current one."""

    def __init__(self):
        self.steps: List[Tuple[int, int, float]] = []
        self.start = 0.0
        self.total = 0.0

    def play(self, pattern: Sequence[Sequence[float]], now: float, scale_k: float = 1.0) -> None:
        steps = []
        for st in pattern or []:
            if len(st) >= 3:
                steps.append((int(st[0] * scale_k), int(st[1] * scale_k), st[2] / 1000.0))
        self.steps = steps
        self.start = now
        self.total = sum(s[2] for s in steps)

    def stop(self) -> None:
        self.steps = []

    @property
    def active(self) -> bool:
        return bool(self.steps)

    def at(self, now: float) -> Tuple[int, int]:
        if not self.steps:
            return (0, 0)
        t = now - self.start
        if t >= self.total:
            self.steps = []
            return (0, 0)
        for left, right, dur in self.steps:
            if t < dur:
                return (max(0, min(255, left)), max(0, min(255, right)))
            t -= dur
        return (0, 0)
