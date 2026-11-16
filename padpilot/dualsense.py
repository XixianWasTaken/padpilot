"""DualSense (PS5) HID protocol: input parsing and output report building.

Layouts follow the Linux hid-playstation driver. USB and Bluetooth are both
supported; Bluetooth output reports carry a CRC32 seeded with 0xA2.
"""
from __future__ import annotations

import struct
import sys
import time
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

VENDOR_SONY = 0x054C
PID_DUALSENSE = 0x0CE6
PID_DUALSENSE_EDGE = 0x0DF2
SUPPORTED_PIDS = (PID_DUALSENSE, PID_DUALSENSE_EDGE)

USB_INPUT_ID = 0x01
BT_INPUT_ID = 0x31
USB_OUTPUT_ID = 0x02
BT_OUTPUT_ID = 0x31
USB_OUTPUT_SIZE = 48  # report id + 47 bytes (checked against the HID descriptor)
BT_OUTPUT_SIZE = 78
COMMON_SIZE = 47

# valid_flag0
F0_COMPATIBLE_VIBRATION = 0x01
F0_HAPTICS_SELECT = 0x02
F0_RIGHT_TRIGGER = 0x04
F0_LEFT_TRIGGER = 0x08
# valid_flag1
# Audio (layout as in the Linux hid-playstation driver and SDL): flag0 bits 5 / 7 enable the
# speaker volume (byte 5) and audio control (byte 7); bits 4-5 of byte 7 pick the output path,
# 3 = the right channel of the USB sound card goes to the built-in speaker.
F0_SPEAKER_VOLUME = 0x20
F0_AUDIO_CONTROL = 0x80
AUDIO_PATH_SPEAKER = 3 << 4

F1_MIC_MUTE_LED = 0x01
F1_POWER_SAVE = 0x02
F1_LIGHTBAR = 0x04
F1_RELEASE_LEDS = 0x08
F1_PLAYER_LEDS = 0x10
# valid_flag2
F2_LED_BRIGHTNESS = 0x01
F2_LIGHTBAR_SETUP = 0x02
F2_COMPATIBLE_VIBRATION2 = 0x04
LIGHTBAR_SETUP_LIGHT_OUT = 0x02

DPAD = {
    0: ("up",), 1: ("up", "right"), 2: ("right",), 3: ("down", "right"),
    4: ("down",), 5: ("down", "left"), 6: ("left",), 7: ("up", "left"),
}

BUTTON_NAMES = (
    "up", "down", "left", "right",
    "square", "cross", "circle", "triangle",
    "l1", "r1", "l2", "r2", "create", "options", "l3", "r3",
    "ps", "touchpad", "mic",
)

# Player LED patterns used by the PS5 for players 1..5 (bits 0..4, left to right).
PLAYER_PATTERNS = (0b00000, 0b00100, 0b01010, 0b10101, 0b11011, 0b11111)


@dataclass
class Touch:
    active: bool = False
    x: int = 0
    y: int = 0


@dataclass
class InputState:
    lx: int = 128
    ly: int = 128
    rx: int = 128
    ry: int = 128
    l2: int = 0
    r2: int = 0
    buttons: Dict[str, bool] = field(default_factory=lambda: {b: False for b in BUTTON_NAMES})
    touches: Tuple[Touch, Touch] = field(default_factory=lambda: (Touch(), Touch()))
    battery: Optional[int] = None  # percent
    charging: Optional[str] = None  # "discharging" | "charging" | "full" | "error"
    full_report: bool = True


def _parse_buttons(b0: int, b1: int, b2: int) -> Dict[str, bool]:
    hat = b0 & 0x0F
    dirs = DPAD.get(hat, ())
    return {
        "up": "up" in dirs, "down": "down" in dirs,
        "left": "left" in dirs, "right": "right" in dirs,
        "square": bool(b0 & 0x10), "cross": bool(b0 & 0x20),
        "circle": bool(b0 & 0x40), "triangle": bool(b0 & 0x80),
        "l1": bool(b1 & 0x01), "r1": bool(b1 & 0x02),
        "l2": bool(b1 & 0x04), "r2": bool(b1 & 0x08),
        "create": bool(b1 & 0x10), "options": bool(b1 & 0x20),
        "l3": bool(b1 & 0x40), "r3": bool(b1 & 0x80),
        "ps": bool(b2 & 0x01), "touchpad": bool(b2 & 0x02), "mic": bool(b2 & 0x04),
    }


def _parse_touch(p: bytes) -> Touch:
    return Touch(
        active=not (p[0] & 0x80),
        x=p[1] | ((p[2] & 0x0F) << 8),
        y=(p[2] >> 4) | (p[3] << 4),
    )


def parse_input(report: bytes) -> Optional[InputState]:
    """Parse a raw input report (including report id). Returns None if unknown."""
    if not report:
        return None
    rid = report[0]
    if rid == USB_INPUT_ID and len(report) >= 64:
        base = 1
    elif rid == BT_INPUT_ID and len(report) >= 78:
        base = 2
    elif rid == USB_INPUT_ID and len(report) >= 10:
        # Bluetooth "simple" report, sent until the controller is switched to full mode.
        s = InputState(full_report=False)
        s.lx, s.ly, s.rx, s.ry = report[1], report[2], report[3], report[4]
        s.buttons = _parse_buttons(report[5], report[6], report[7])
        s.l2, s.r2 = report[8], report[9]
        return s
    else:
        return None
    d = report[base:]
    s = InputState()
    s.lx, s.ly, s.rx, s.ry, s.l2, s.r2 = d[0], d[1], d[2], d[3], d[4], d[5]
    s.buttons = _parse_buttons(d[7], d[8], d[9])
    s.touches = (_parse_touch(d[32:36]), _parse_touch(d[36:40]))
    status = d[52]
    level = status & 0x0F
    s.battery = min(level * 10 + 5, 100)
    s.charging = {0x0: "discharging", 0x1: "charging", 0x2: "full"}.get(status >> 4, "error")
    if s.charging == "full":
        s.battery = 100
    return s


# ---------------------------------------------------------------- triggers
# Adaptive trigger effects, 11 bytes each: [mode, p0..p9].
# Encodings follow the widely used "trigger effect factory" (Nielk1).

def trigger_off() -> bytes:
    return bytes(11)


def trigger_feedback(position: int, strength: int) -> bytes:
    """Constant resistance from `position` (0-9) on, strength 1-8."""
    position = max(0, min(9, position))
    strength = max(1, min(8, strength))
    force = (strength - 1) & 0x07
    active = 0
    zones = 0
    for i in range(position, 10):
        active |= 1 << i
        zones |= force << (3 * i)
    return bytes([0x21, active & 0xFF, (active >> 8) & 0xFF]) + struct.pack("<I", zones) + bytes(4)


def trigger_weapon(start: int, end: int, strength: int) -> bytes:
    """Resistance between start (2-7) and end (start+1..8) that 'snaps' like a gun trigger."""
    start = max(2, min(7, start))
    end = max(start + 1, min(8, end))
    strength = max(1, min(8, strength))
    zones = (1 << start) | (1 << end)
    return bytes([0x25, zones & 0xFF, (zones >> 8) & 0xFF, strength - 1]) + bytes(7)


def trigger_vibration(position: int, amplitude: int, frequency: int) -> bytes:
    """Trigger buzzes from `position` (0-9) on; amplitude 1-8, frequency in Hz (1-255)."""
    position = max(0, min(9, position))
    amplitude = max(1, min(8, amplitude))
    frequency = max(1, min(255, frequency))
    strength = (amplitude - 1) & 0x07
    active = 0
    zones = 0
    for i in range(position, 10):
        active |= 1 << i
        zones |= strength << (3 * i)
    out = bytearray(11)
    out[0] = 0x26
    out[1] = active & 0xFF
    out[2] = (active >> 8) & 0xFF
    out[3:7] = struct.pack("<I", zones)
    out[9] = frequency
    return bytes(out)


def make_trigger(spec) -> bytes:
    """Build a trigger effect from a config spec like {"type": "weapon", "start": 3, ...}."""
    if not spec:
        return trigger_off()
    kind = spec.get("type", "off")
    if kind == "feedback":
        return trigger_feedback(spec.get("position", 3), spec.get("strength", 4))
    if kind == "weapon":
        return trigger_weapon(spec.get("start", 3), spec.get("end", 6), spec.get("strength", 6))
    if kind == "vibration":
        return trigger_vibration(spec.get("position", 0), spec.get("amplitude", 4), spec.get("frequency", 30))
    return trigger_off()


# ---------------------------------------------------------------- output
@dataclass
class OutputState:
    rgb: Tuple[int, int, int] = (0, 0, 64)
    rumble_left: int = 0   # low frequency, strong motor
    rumble_right: int = 0  # high frequency, weak motor
    player_leds: int = 0   # bits 0-4
    mic_led: int = 0       # 0 off, 1 on, 2 pulse
    trigger_left: bytes = field(default_factory=trigger_off)
    trigger_right: bytes = field(default_factory=trigger_off)
    # None: leave the audio setup alone; 0: speaker off (the default headphone path);
    # 1-100: the right channel of the USB sound card plays on the built-in speaker.
    speaker_volume: Optional[int] = None


def build_common(o: OutputState, *, setup_lightbar: bool = False, release: bool = False) -> bytearray:
    c = bytearray(COMMON_SIZE)
    if release:
        # Hand the LEDs back to the system and switch every effect off.
        c[0] = F0_COMPATIBLE_VIBRATION | F0_HAPTICS_SELECT | F0_RIGHT_TRIGGER | F0_LEFT_TRIGGER
        c[1] = F1_RELEASE_LEDS | F1_MIC_MUTE_LED
        c[38] = F2_COMPATIBLE_VIBRATION2
        return c
    c[0] = F0_COMPATIBLE_VIBRATION | F0_HAPTICS_SELECT | F0_RIGHT_TRIGGER | F0_LEFT_TRIGGER
    c[1] = F1_MIC_MUTE_LED | F1_LIGHTBAR | F1_PLAYER_LEDS
    c[2] = _clamp(o.rumble_right)
    c[3] = _clamp(o.rumble_left)
    if o.speaker_volume is not None:
        c[0] |= F0_SPEAKER_VOLUME | F0_AUDIO_CONTROL
        c[5] = max(0, min(100, int(o.speaker_volume)))
        c[7] = AUDIO_PATH_SPEAKER if o.speaker_volume > 0 else 0
    c[8] = o.mic_led
    c[10:21] = o.trigger_right
    c[21:32] = o.trigger_left
    # Old firmware uses flag0 bit0 for rumble, firmware >= 2.21 uses flag2 bit2; set both.
    c[38] = F2_COMPATIBLE_VIBRATION2
    if setup_lightbar:
        c[38] |= F2_LIGHTBAR_SETUP
        c[41] = LIGHTBAR_SETUP_LIGHT_OUT
    c[43] = o.player_leds & 0x1F
    c[44], c[45], c[46] = (_clamp(v) for v in o.rgb)
    return c


def build_usb_report(o: OutputState, **kw) -> bytes:
    return bytes([USB_OUTPUT_ID]) + bytes(build_common(o, **kw))


def bt_crc(data: bytes, seed: int) -> int:
    return zlib.crc32(bytes([seed]) + data) & 0xFFFFFFFF


def build_bt_report(o: OutputState, seq: int, **kw) -> bytes:
    r = bytearray(BT_OUTPUT_SIZE)
    r[0] = BT_OUTPUT_ID
    r[1] = (seq & 0x0F) << 4
    r[2] = 0x10
    r[3:3 + COMMON_SIZE] = build_common(o, **kw)
    r[74:78] = struct.pack("<I", bt_crc(bytes(r[:74]), 0xA2))
    return bytes(r)


def _clamp(v) -> int:
    return max(0, min(255, int(v)))


# ---------------------------------------------------------------- device
def set_shared_open(hid_module, shared: bool) -> bool:
    """hidapi opens devices exclusively on macOS by default. That hides the controller from every
    other app: a browser's Gamepad API, games, and also macOS itself, which otherwise opens the Games
    app on a PS press. So exclusive is the normal mode, and shared is asked for only when another
    app should read the pad too. The Python package does not expose the switch, so this calls the
    C function in the extension module itself. Returns whether the switch was set."""
    if sys.platform != "darwin":
        return False
    try:
        import ctypes
        lib = ctypes.CDLL(getattr(hid_module, "__file__", "") or "")
        lib.hid_darwin_set_open_exclusive(0 if shared else 1)
        return True
    except (OSError, AttributeError, TypeError):
        return False


class DualSense:
    """Thin wrapper around a hidapi handle. Raises OSError on I/O failure."""

    def __init__(self, hid_module, info: dict, shared: bool = False):
        self._hid = hid_module
        self.info = info
        self.shared = shared
        self.dev = hid_module.device()
        set_shared_open(hid_module, shared)
        self.dev.open_path(info["path"])
        self.bluetooth = info.get("bus_type") == 2 or info.get("interface_number", 0) == -1
        self._seq = 0
        self._first_write = True
        self.serial = info.get("serial_number") or ""
        if self.bluetooth:
            # Reading calibration (feature 0x05) switches BT controllers to full 0x31 reports.
            try:
                self.dev.get_feature_report(0x05, 41)
            except Exception:
                pass

    @staticmethod
    def find(hid_module) -> List[dict]:
        found = []
        for d in hid_module.enumerate(VENDOR_SONY, 0):
            if d.get("product_id") in SUPPORTED_PIDS:
                # Skip non-gamepad collections when the OS exposes several.
                if d.get("usage_page") not in (None, 0, 1) or d.get("usage") not in (None, 0, 5):
                    continue
                found.append(d)
        return found

    def read(self, timeout_ms: int) -> Optional[bytes]:
        data = self.dev.read(78, timeout_ms)
        if not data:
            return None
        if data[0] == BT_INPUT_ID:
            self.bluetooth = True
        return bytes(data)

    def write(self, o: OutputState, release: bool = False) -> None:
        setup = self._first_write and not release
        if self.bluetooth:
            rep = build_bt_report(o, self._seq, setup_lightbar=setup, release=release)
            self._seq = (self._seq + 1) & 0x0F
        else:
            rep = build_usb_report(o, setup_lightbar=setup, release=release)
        n = self.dev.write(rep)
        if n < 0:
            raise OSError("hid write failed")
        self._first_write = False

    def close(self) -> None:
        try:
            self.dev.close()
        except Exception:
            pass


def monotonic() -> float:
    return time.monotonic()
