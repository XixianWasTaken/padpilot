"""macOS-only helpers (ctypes, no extra dependencies):

- post keystrokes to the frontmost app with Quartz CGEvents (needs Accessibility permission),
- look up the frontmost app's bundle id,
- spawn the daemon as its own "responsible process", so the Accessibility grant belongs to
  Python.app no matter whether a Claude hook, a terminal or the desktop app started it.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import POINTER, byref, c_bool, c_char_p, c_int, c_uint16, c_uint32, c_uint64, c_ulong, c_void_p
from typing import List, Optional, Tuple

IS_MAC = sys.platform == "darwin"

SHIFT, CTRL, ALT, CMD = 0x20000, 0x40000, 0x80000, 0x100000

# Virtual key codes (ANSI layout, Carbon kVK_*).
_VK = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
}
_NAMED = {
    "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51, "esc": 53, "escape": 53,
    "delete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
}
_MODS = {"shift": SHIFT, "ctrl": CTRL, "alt": ALT, "cmd": CMD}


def key_event(name: str) -> Tuple[int, int]:
    """Key name (same names as keys.encode_key, plus cmd+x) -> (virtual keycode, modifier flags)."""
    parts = name.strip().lower().split("+")
    base, flags = parts[-1], 0
    if name.strip() == "+":
        base, parts = "+", ["+"]
    for m in parts[:-1]:
        if m not in _MODS:
            raise KeyError(name)
        flags |= _MODS[m]
    if base in _NAMED:
        return _NAMED[base], flags
    if base in _VK:
        return _VK[base], flags
    raise KeyError(name)


# ---------------------------------------------------------------- Quartz
class _Quartz:
    def __init__(self):
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cg.CGEventCreateKeyboardEvent.restype = c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint16, c_bool]
        cg.CGEventSetFlags.argtypes = [c_void_p, c_uint64]
        cg.CGEventKeyboardSetUnicodeString.argtypes = [c_void_p, c_ulong, POINTER(c_uint16)]
        cg.CGEventPost.argtypes = [c_uint32, c_void_p]
        cf.CFRelease.argtypes = [c_void_p]
        ax.AXIsProcessTrusted.restype = c_bool
        ax.AXIsProcessTrustedWithOptions.restype = c_bool
        ax.AXIsProcessTrustedWithOptions.argtypes = [c_void_p]
        cf.CFDictionaryCreate.restype = c_void_p
        cf.CFDictionaryCreate.argtypes = [c_void_p, POINTER(c_void_p), POINTER(c_void_p), ctypes.c_long,
                                          c_void_p, c_void_p]
        self.cg, self.cf, self.ax = cg, cf, ax

    def trusted(self, prompt: bool = False) -> bool:
        if not prompt:
            return bool(self.ax.AXIsProcessTrusted())
        # Posting events is gated by Accessibility; this shows the system prompt that adds us
        # to Privacy & Security > Accessibility.
        key = c_void_p.in_dll(self.ax, "kAXTrustedCheckOptionPrompt")
        val = c_void_p.in_dll(self.cf, "kCFBooleanTrue")
        keys, vals = (c_void_p * 1)(key.value), (c_void_p * 1)(val.value)
        kcb = ctypes.addressof(ctypes.c_char.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks"))
        vcb = ctypes.addressof(ctypes.c_char.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks"))
        d = self.cf.CFDictionaryCreate(None, keys, vals, 1, kcb, vcb)
        try:
            return bool(self.ax.AXIsProcessTrustedWithOptions(d))
        finally:
            self.cf.CFRelease(d)

    def _post(self, code: int, flags: int, down: bool, text: Optional[str] = None) -> None:
        ev = self.cg.CGEventCreateKeyboardEvent(None, code, down)
        if not ev:
            raise OSError("CGEventCreateKeyboardEvent failed")
        try:
            self.cg.CGEventSetFlags(ev, flags)
            if text is not None:
                units = text.encode("utf-16-le")
                buf = (c_uint16 * (len(units) // 2)).from_buffer_copy(units)
                self.cg.CGEventKeyboardSetUnicodeString(ev, len(buf), buf)
            self.cg.CGEventPost(0, ev)  # kCGHIDEventTap
        finally:
            self.cf.CFRelease(ev)

    def key(self, name: str) -> None:
        code, flags = key_event(name)
        self._post(code, flags, True)
        self._post(code, flags, False)

    def text(self, s: str) -> None:
        for chunk in utf16_chunks(s):
            self._post(0, 0, True, chunk)
            self._post(0, 0, False, chunk)
            time.sleep(0.005)


def utf16_chunks(s: str, limit: int = 16) -> List[str]:
    """Split text for keyboard events: one event carries at most ~20 UTF-16 units, and a character
    outside the BMP (most emoji) takes two, so count units, not characters."""
    out, cur, n = [], [], 0
    for ch in s:
        u = 2 if ord(ch) > 0xFFFF else 1
        if cur and n + u > limit:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(ch)
        n += u
    if cur:
        out.append("".join(cur))
    return out


_quartz: Optional[_Quartz] = None


def quartz() -> Optional[_Quartz]:
    global _quartz
    if _quartz is None and IS_MAC:
        try:
            _quartz = _Quartz()
        except (OSError, AttributeError):
            _quartz = None
    return _quartz


def accessibility_trusted(prompt: bool = False) -> bool:
    q = quartz()
    return bool(q and q.trusted(prompt))


def post_items(items: List[dict], gap_ms: int = 90) -> None:
    """Post wrapper-style items ({"key": name} / {"text": s}) to the frontmost app."""
    q = quartz()
    if q is None:
        return
    for i, it in enumerate(items):
        if i:
            time.sleep(gap_ms / 1000.0)
        if "text" in it:
            q.text(it["text"])
        else:
            q.key(it["key"])


def frontmost_bundle_id() -> Optional[str]:
    """Bundle id of the frontmost app. lsappinfo asks the window server each time, unlike
    NSWorkspace, which goes stale in a process without a run loop."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=1).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(["lsappinfo", "info", "-only", "bundleid", asn],
                             capture_output=True, text=True, timeout=1).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # "CFBundleIdentifier"="com.anthropic.claudefordesktop"
    if "=" not in out:
        return None
    return out.split("=", 1)[1].strip().strip('"') or None


# ---------------------------------------------------------------- message box focus
# Key presses go to whatever has keyboard focus in the app. After a click on the sidebar or a
# button, typed text is lost and Enter presses that button, so before keys meant for the message
# box we move focus to it with the Accessibility API. Only roles, labels and the tree's shape are
# read, never what any element says.
_TEXT_ROLES = ("AXTextArea", "AXTextField", "AXComboBox", "AXSearchField")
_DIALOG_SUBROLES = ("AXApplicationDialog", "AXDialog", "AXSystemDialog", "AXFloatingWindow")
_kAXErrorNoValue = -25212


class _AX:
    def __init__(self):
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ax.AXUIElementCreateApplication.argtypes = [c_int]
        ax.AXUIElementCreateApplication.restype = c_void_p
        ax.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        ax.AXUIElementCopyAttributeValue.restype = c_int
        ax.AXUIElementSetAttributeValue.argtypes = [c_void_p, c_void_p, c_void_p]
        ax.AXUIElementSetAttributeValue.restype = c_int
        ax.AXUIElementSetMessagingTimeout.argtypes = [c_void_p, ctypes.c_float]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, ctypes.c_long, c_uint32]
        cf.CFStringGetCString.restype = c_bool
        cf.CFGetTypeID.argtypes = [c_void_p]
        cf.CFGetTypeID.restype = c_ulong
        cf.CFStringGetTypeID.restype = c_ulong
        cf.CFArrayGetTypeID.restype = c_ulong
        cf.CFArrayGetCount.argtypes = [c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = c_void_p
        cf.CFEqual.argtypes = [c_void_p, c_void_p]
        cf.CFEqual.restype = c_bool
        cf.CFRelease.argtypes = [c_void_p]
        self.ax, self.cf = ax, cf
        self.true = c_void_p.in_dll(cf, "kCFBooleanTrue").value
        self.names: dict = {}
        self.enabled: set = set()  # pids whose web content we asked to expose

    def _name(self, s: str) -> int:
        if s not in self.names:  # kept for the life of the process
            self.names[s] = self.cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)
        return self.names[s]

    def copy(self, el, attr: str):
        """(error, value); a value must be released."""
        out = c_void_p()
        err = self.ax.AXUIElementCopyAttributeValue(el, self._name(attr), byref(out))
        return err, (out.value if err == 0 else None)

    def text(self, el, attr: str) -> Optional[str]:
        err, v = self.copy(el, attr)
        if not v:
            return None
        try:
            if self.cf.CFGetTypeID(v) != self.cf.CFStringGetTypeID():
                return None
            buf = ctypes.create_string_buffer(1024)
            return buf.value.decode("utf-8", "replace") if self.cf.CFStringGetCString(v, buf, 1024, 0x08000100) else None
        finally:
            self.cf.CFRelease(v)

    def chain(self, el) -> list:
        """Ancestors of `el` from its window down to `el` (all retained; release them)."""
        out = [el]
        cur = el
        for _ in range(80):
            if self.text(cur, "AXRole") == "AXWindow":
                break
            err, p = self.copy(cur, "AXParent")
            if not p:
                break
            out.append(p)
            cur = p
        return out[::-1]

    def find_boxes(self, window) -> Tuple[List[Tuple[list, int]], list]:
        """Message boxes under `window` as (path of elements from the window down, 1 if labelled
        "Prompt"), and the arrays the path elements live in, which the caller releases."""
        found: List[Tuple[list, int]] = []
        arrays: list = []
        budget = [6000]

        def walk(el, path):
            budget[0] -= 1
            if budget[0] < 0 or len(path) > 90:
                return
            if self.text(el, "AXRole") == "AXTextArea":
                found.append((path, 1 if self.text(el, "AXDescription") == "Prompt" else 0))
                return
            err, kids = self.copy(el, "AXChildren")
            if not kids:
                return
            arrays.append(kids)
            if self.cf.CFGetTypeID(kids) != self.cf.CFArrayGetTypeID():
                return
            for i in range(self.cf.CFArrayGetCount(kids)):
                k = self.cf.CFArrayGetValueAtIndex(kids, i)
                walk(k, path + [k])

        walk(window, [window])
        return found, arrays


_ax: Optional[_AX] = None


def _app_pid(bundle_id: str) -> Optional[int]:
    try:
        out = subprocess.run(["lsappinfo", "info", "-only", "pid", "-app", bundle_id],
                             capture_output=True, text=True, timeout=1).stdout
        return int(out.split("=", 1)[1]) if "=" in out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def focus_message_box(bundle_id: str) -> str:
    """Give the app's message box keyboard focus, unless a text field, a menu or a dialog has
    it. Returns what happened, for the log: "kept ...", "focused", or why it could not."""
    global _ax
    if not IS_MAC:
        return "not macOS"
    if _ax is None:
        try:
            _ax = _AX()
        except (OSError, AttributeError) as e:
            return f"no Accessibility API ({e})"
    a, cf = _ax, _ax.cf
    pid = _app_pid(bundle_id)
    if pid is None:
        return "app not running"
    app = a.ax.AXUIElementCreateApplication(pid)
    held: list = []
    try:
        a.ax.AXUIElementSetMessagingTimeout(app, 0.5)
        if pid not in a.enabled:
            # Electron builds the tree of its web content only for assistive apps that ask.
            a.ax.AXUIElementSetAttributeValue(app, a._name("AXManualAccessibility"), a.true)
            a.enabled.add(pid)
        focused = None
        for _ in range(20):  # the first time, the tree takes a moment to appear
            err, focused = a.copy(app, "AXFocusedUIElement")
            if focused or err != _kAXErrorNoValue:
                break
            time.sleep(0.05)
        if not focused:
            return "no focused element"
        held.append(focused)
        role = a.text(focused, "AXRole")
        if role in _TEXT_ROLES:
            return f"kept ({role})"
        chain = a.chain(focused)
        held += chain[:-1]
        for el in chain:
            r, sub = a.text(el, "AXRole"), a.text(el, "AXSubrole")
            if r in ("AXMenu", "AXMenuItem", "AXSheet") or sub in _DIALOG_SUBROLES:
                return f"kept ({r}/{sub})"
        window = chain[0]
        if a.text(window, "AXRole") != "AXWindow":
            err, window = a.copy(app, "AXFocusedWindow")
            if not window:
                return "no window"
            held.append(window)
        boxes, arrays = a.find_boxes(window)
        held += arrays
        if not boxes:
            return "no message box found"

        def shared(path):  # how far down the tree it shares with the focused element
            n = 0
            for x, y in zip(path, chain):
                if not cf.CFEqual(x, y):
                    break
                n += 1
            return n
        # The labelled box first, then the one nearest to where focus was (split view).
        path, _ = max(boxes, key=lambda b: (b[1], shared(b[0])))
        box = path[-1]
        err = a.ax.AXUIElementSetAttributeValue(box, a._name("AXFocused"), a.true)
        if err:
            return f"could not focus the message box (AX error {err})"
        for _ in range(10):
            err, now = a.copy(app, "AXFocusedUIElement")
            if now:
                same = cf.CFEqual(now, box)
                cf.CFRelease(now)
                if same:
                    return f"focused (was {role})"
            time.sleep(0.02)
        return f"asked to focus the message box (was {role}), not confirmed"
    finally:
        for v in held:
            cf.CFRelease(v)
        cf.CFRelease(app)


# ---------------------------------------------------------------- spawning
POSIX_SPAWN_SETSID = 0x0400
POSIX_SPAWN_CLOEXEC_DEFAULT = 0x4000


def spawn_disclaimed(argv: List[str], env: dict, cwd: str, log_path: str) -> int:
    """posix_spawn `argv` detached (new session), stdin=/dev/null, stdout/stderr appended to
    log_path, with responsibility disclaimed so macOS privacy permissions (TCC) are checked
    against the spawned binary itself instead of whoever launched us. Raises OSError."""
    libc = ctypes.CDLL(None, use_errno=True)
    disclaim = getattr(libc, "responsibility_spawnattrs_setdisclaim", None)
    attr, fa = c_void_p(), c_void_p()
    if libc.posix_spawnattr_init(byref(attr)) or libc.posix_spawn_file_actions_init(byref(fa)):
        raise OSError("posix_spawn init failed")
    try:
        libc.posix_spawnattr_setflags(byref(attr), ctypes.c_short(POSIX_SPAWN_SETSID | POSIX_SPAWN_CLOEXEC_DEFAULT))
        if disclaim is not None:
            disclaim(byref(attr), c_int(1))
        O_WRONLY, O_APPEND, O_CREAT = 0x1, 0x8, 0x200
        libc.posix_spawn_file_actions_addopen(byref(fa), 0, b"/dev/null", c_int(0), c_int(0))
        libc.posix_spawn_file_actions_addopen(byref(fa), 1, log_path.encode(), c_int(O_WRONLY | O_APPEND | O_CREAT),
                                              c_int(0o600))
        libc.posix_spawn_file_actions_adddup2(byref(fa), 1, 2)
        libc.posix_spawn_file_actions_addchdir_np(byref(fa), cwd.encode())
        c_argv = (c_char_p * (len(argv) + 1))(*[a.encode() for a in argv], None)
        envl = [f"{k}={v}".encode() for k, v in env.items()]
        c_env = (c_char_p * (len(envl) + 1))(*envl, None)
        pid = c_int()
        rc = libc.posix_spawn(byref(pid), argv[0].encode(), byref(fa), byref(attr), c_argv, c_env)
        if rc:
            raise OSError(rc, os.strerror(rc))
        return pid.value
    finally:
        libc.posix_spawn_file_actions_destroy(byref(fa))
        libc.posix_spawnattr_destroy(byref(attr))


# ---------------------------------------------------------------- microphone activity
class _AudioAddr(ctypes.Structure):
    _fields_ = [("sel", c_uint32), ("scope", c_uint32), ("elem", c_uint32)]


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


_ca = None


def _coreaudio():
    global _ca
    if _ca is None:
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        p = POINTER(_AudioAddr)
        ca.AudioObjectGetPropertyDataSize.argtypes = [c_uint32, p, c_uint32, c_void_p, POINTER(c_uint32)]
        ca.AudioObjectGetPropertyData.argtypes = [c_uint32, p, c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
        _ca = ca
    return _ca


def mic_in_use() -> bool:
    """True while any process records from any input device (the same signal as the orange
    menu-bar dot). Reading it needs no microphone permission."""
    if not IS_MAC:
        return False
    try:
        ca = _coreaudio()
        a = _AudioAddr(_fourcc("dev#"), _fourcc("glob"), 0)
        size = c_uint32()
        if ca.AudioObjectGetPropertyDataSize(1, byref(a), 0, None, byref(size)):
            return False
        ids = (c_uint32 * (size.value // 4))()
        if ca.AudioObjectGetPropertyData(1, byref(a), 0, None, byref(size), ids):
            return False
        for d in ids:
            streams = _AudioAddr(_fourcc("stm#"), _fourcc("inpt"), 0)
            n = c_uint32()
            if ca.AudioObjectGetPropertyDataSize(d, byref(streams), 0, None, byref(n)) or not n.value:
                continue
            running = _AudioAddr(_fourcc("gone"), _fourcc("glob"), 0)
            v, vs = c_uint32(), c_uint32(4)
            if not ca.AudioObjectGetPropertyData(d, byref(running), 0, None, byref(vs), byref(v)) and v.value:
                return True
    except (OSError, AttributeError):
        pass
    return False
