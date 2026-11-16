"""Key names -> terminal byte sequences, and parsing of mapping actions."""
from __future__ import annotations

from typing import List, Union

# Cursor keys depend on DECCKM (application cursor mode); see encode_key.
_CURSOR = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}

KEYS = {
    "enter": "\r",
    "return": "\r",
    "esc": "\x1b",
    "escape": "\x1b",
    "tab": "\t",
    "shift+tab": "\x1b[Z",
    "backspace": "\x7f",
    "delete": "\x1b[3~",
    "space": " ",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "shift+enter": "\x1b\r",
    "alt+enter": "\x1b\r",
    # Clears a multi-line input box: Ctrl+U (delete to line start) + Backspace, per line.
    "kill_input": "\x15\x7f" * 24,
}


def encode_key(name: str, app_cursor: bool = False, kitty_flags: int = 0) -> str:
    """Return the bytes (as str) a terminal sends for key `name`. Raises KeyError.

    With the kitty keyboard protocol's "disambiguate" flag active, Esc and ctrl/alt combos
    are sent as CSI u sequences, as a kitty-protocol terminal would.
    """
    name = name.strip().lower()
    if kitty_flags & 1:
        if name in ("esc", "escape"):
            return "\x1b[27u"
        if name.startswith("ctrl+") and len(name) == 6 and "a" <= name[5] <= "z":
            return "\x1b[%d;5u" % ord(name[5])
        if name.startswith("alt+") and len(name) == 5:
            return "\x1b[%d;3u" % ord(name[4])
    if name in _CURSOR:
        return ("\x1bO" if app_cursor else "\x1b[") + _CURSOR[name]
    if name in KEYS:
        return KEYS[name]
    if name.startswith("ctrl+") and len(name) == 6:
        ch = name[5]
        if "a" <= ch <= "z":
            return chr(ord(ch) - 96)
        if ch in "[\\]^_":
            return chr(ord(ch) - 64)
        if ch == "@" or ch == " ":
            return "\x00"
    if name.startswith("alt+") and len(name) == 5:
        return "\x1b" + name[4]
    if len(name) == 1:
        return name
    raise KeyError(name)


Action = Union[str, List[str]]


def split_action(action: Action) -> List[str]:
    """Normalize a mapping value to a list of steps."""
    if action is None:
        return []
    if isinstance(action, str):
        return [action] if action else []
    return [a for a in action if isinstance(a, str) and a]


def to_items(steps: List[str], validate=encode_key) -> List[dict]:
    """Convert non-builtin steps into items {"key": name} or {"text": s}. `validate` raises
    KeyError for keys the destination cannot send (terminal by default)."""
    items = []
    for s in steps:
        if s.startswith("@"):
            continue
        if s.startswith("text:"):
            items.append({"text": s[5:]})
        else:
            validate(s)  # validate early
            items.append({"key": s})
    return items
