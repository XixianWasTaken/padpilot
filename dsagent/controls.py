"""Turns a stream of raw InputState snapshots into named control events.

Control names: physical buttons (cross, circle, ...), stick_up/down/left/right (left stick),
rstick_* (right stick), l2_full / r2_full (analog triggers fully pulled), swipe_up/down/left/right
(touchpad), plus:

- `<mod>+<name>`: a control pressed while a modifier (PS, R1 by default) is held. The modifier's
  own tap/hold action is then skipped.
- `<name>_hold`: released after `hold_ms` (for controls that have a hold action, and modifiers).
  `<name>_hold_ready` fires once when the hold time is reached, so the pad can tick.
- push-to-talk controls (mic): `<name>` on press, `<name>_up` on release after `ptt_ms`.
- the prompt wheel: while triangle is held, moving either stick emits `wheel:<0-7>`
  (0 = up, clockwise); releasing triangle then emits `wheel_pick:<n>` instead of triangle.
  Held past `hold_ms` without picking, the release emits nothing (cancel).
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Set

from .dualsense import InputState

# Right stick left/right switch sessions, so they don't repeat.
REPEATING = {"up", "down", "left", "right",
             "stick_up", "stick_down", "stick_left", "stick_right",
             "rstick_up", "rstick_down"}
DIGITAL = ("up", "down", "left", "right", "square", "cross", "circle", "triangle",
           "l1", "r1", "create", "options", "l3", "r3", "ps", "touchpad", "mic")
WHEEL_BUTTON = "triangle"


def _stick_dir(x: int, y: int, threshold: float) -> Optional[str]:
    nx = (x - 128) / 127.0
    ny = (y - 128) / 127.0
    if max(abs(nx), abs(ny)) < threshold:
        return None
    if abs(nx) > abs(ny):
        return "right" if nx > 0 else "left"
    return "down" if ny > 0 else "up"


def wheel_sector(x: int, y: int, threshold: float) -> Optional[int]:
    """Right stick -> one of 8 sectors (0 = up, clockwise), or None near the center."""
    nx = (x - 128) / 127.0
    ny = (y - 128) / 127.0
    if math.hypot(nx, ny) < threshold:
        return None
    ang = math.degrees(math.atan2(nx, -ny)) % 360.0
    return int(((ang + 22.5) % 360.0) // 45.0)


class ControlTracker:
    def __init__(self, cfg: dict, has_hold: Callable[[str], bool]):
        self.cfg = cfg
        self.has_hold = has_hold
        self.down: Dict[str, float] = {}       # control -> press time
        self.next_repeat: Dict[str, float] = {}
        self.hold_ready: Dict[str, bool] = {}
        self.combo_used: Dict[str, bool] = {}  # modifier -> used in a combo during this press
        self.combo_of: Dict[str, str] = {}     # control pressed as part of a combo -> combo name
        self.wheel: Optional[int] = None       # selected sector while the wheel button is held
        self.touch_start = None
        self.touch_last = None
        self.raw: List[tuple] = []             # (name, pressed, held_s) transitions, for `dsagent watch`

    @property
    def modifiers(self) -> List[str]:
        return list(self.cfg.get("modifiers", ["ps", "r1"]))

    @property
    def push_to_talk(self) -> Set[str]:
        return set(self.cfg.get("push_to_talk", ["mic"]))

    def _virtual(self, s: InputState) -> Dict[str, bool]:
        th = float(self.cfg.get("stick_threshold", 0.6))
        full = int(self.cfg.get("trigger_full", 235))
        v = {name: bool(s.buttons.get(name)) for name in DIGITAL}
        ld = _stick_dir(s.lx, s.ly, th)
        rd = _stick_dir(s.rx, s.ry, th)
        for d in ("up", "down", "left", "right"):
            v["stick_" + d] = ld == d
            v["rstick_" + d] = rd == d
        # Hysteresis: once "full", stay pressed until the trigger is mostly released.
        for t, val in (("l2_full", s.l2), ("r2_full", s.r2)):
            v[t] = val >= full or (t in self.down and val > full * 0.5)
        return v

    def _wheel_enabled(self) -> bool:
        return bool(self.cfg.get("prompt_wheel"))

    def update(self, s: InputState, now: float) -> List[str]:
        out: List[str] = []
        delay = self.cfg.get("repeat_delay_ms", 350) / 1000.0
        rate = self.cfg.get("repeat_rate_ms", 70) / 1000.0
        hold = self.cfg.get("hold_ms", 600) / 1000.0
        ptt = self.cfg.get("ptt_ms", 500) / 1000.0
        mods = self.modifiers
        # Not when the wheel button was pressed as part of a combo (R1 + △ ...).
        wheel_on = (self._wheel_enabled() and WHEEL_BUTTON in self.down
                    and WHEEL_BUTTON not in self.combo_of)
        if wheel_on:  # either stick steers the wheel (right first)
            th = float(self.cfg.get("stick_threshold", 0.6))
            sec = wheel_sector(s.rx, s.ry, th)
            if sec is None:
                sec = wheel_sector(s.lx, s.ly, th)
            if sec is not None and sec != self.wheel:
                self.wheel = sec
                out.append(f"wheel:{sec}")

        v = self._virtual(s)
        # Modifiers first: R1 and ✕ arriving in the same report are still R1 + ✕.
        for name in [m for m in mods if m in v] + [n for n in v if n not in mods]:
            pressed = v[name]
            was = name in self.down
            if pressed != was:
                self.raw.append((name, pressed, 0.0 if pressed else now - self.down[name]))
            if pressed and not was:
                self.down[name] = now
                self.hold_ready[name] = False
                held_mods = [m for m in mods if m in self.down and m != name]
                if held_mods:
                    combo = f"{held_mods[0]}+{name}"
                    self.combo_used[held_mods[0]] = True
                    self.combo_of[name] = combo
                    out.append(combo)
                    if name in REPEATING:
                        self.next_repeat[name] = now + delay
                    continue
                if wheel_on and name.startswith(("rstick_", "stick_")):
                    self.combo_of[name] = ""  # the stick is steering the wheel
                    continue
                if name in mods:
                    self.combo_used[name] = False
                    continue  # tap / hold decided on release
                if name in self.push_to_talk:
                    out.append(name)
                    continue
                if self.has_hold(name) or (name == WHEEL_BUTTON and self._wheel_enabled()):
                    if name == WHEEL_BUTTON:
                        self.wheel = None
                        # A stick already pushed now steers the wheel: no more arrow repeats.
                        for n in list(self.down):
                            if n.startswith(("stick_", "rstick_")):
                                self.combo_of[n] = ""
                    continue  # decided on release
                out.append(name)
                if name in REPEATING:
                    self.next_repeat[name] = now + delay
            elif pressed and was:
                if name in self.combo_of:
                    combo = self.combo_of[name]
                    if combo and name in REPEATING and now >= self.next_repeat.get(name, now + 1):
                        out.append(combo)
                        self.next_repeat[name] = now + rate
                elif (name in mods or self.has_hold(name)) and not self.hold_ready[name] \
                        and not self.combo_used.get(name) and not (name == WHEEL_BUTTON and self.wheel is not None) \
                        and self.has_hold(name) and now - self.down[name] >= hold:
                    self.hold_ready[name] = True
                    out.append(name + "_hold_ready")
                elif name == WHEEL_BUTTON and self._wheel_enabled() and self.wheel is None \
                        and not self.hold_ready[name] and now - self.down[name] >= hold:
                    self.hold_ready[name] = True
                    out.append(name + "_hold_ready")  # wheel mode: releasing now cancels
                elif name in REPEATING and not self.has_hold(name) and now >= self.next_repeat.get(name, now + 1):
                    out.append(name)
                    self.next_repeat[name] = now + rate
            elif not pressed and was:
                dur = now - self.down.pop(name)
                self.next_repeat.pop(name, None)
                if name in self.combo_of:
                    self.combo_of.pop(name)
                elif name == WHEEL_BUTTON and self.wheel is not None:
                    out.append(f"wheel_pick:{self.wheel}")
                    self.wheel = None
                elif name in mods:
                    if not self.combo_used.pop(name, False):
                        out.append(name + "_hold" if self.has_hold(name) and dur >= hold else name)
                elif name in self.push_to_talk:
                    if dur >= ptt:
                        out.append(name + "_up")
                elif name == WHEEL_BUTTON and self._wheel_enabled() and not self.has_hold(name):
                    if dur < hold:
                        out.append(name)  # a tap; held without picking = cancel
                elif self.has_hold(name) or (name == WHEEL_BUTTON and self._wheel_enabled()):
                    out.append(name + "_hold" if self.has_hold(name) and dur >= hold else name)
                self.hold_ready.pop(name, None)
        out.extend(self._swipe(s))
        return out

    def _swipe(self, s: InputState) -> List[str]:
        t = s.touches[0]
        if t.active:
            if self.touch_start is None:
                self.touch_start = (t.x, t.y)
            self.touch_last = (t.x, t.y)
            return []
        if self.touch_start is None:
            return []
        (x0, y0), (x1, y1) = self.touch_start, self.touch_last or self.touch_start
        self.touch_start = self.touch_last = None
        dx, dy = x1 - x0, y1 - y0
        if max(abs(dx), abs(dy)) < self.cfg.get("swipe_min", 350):
            return []
        if abs(dx) > abs(dy):
            return ["swipe_right" if dx > 0 else "swipe_left"]
        return ["swipe_down" if dy > 0 else "swipe_up"]
