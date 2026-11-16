"""Controller actions -> the keys the user's agent actually listens to.

Mappings may name an action ("act:submit") instead of a key. The action is resolved per
destination when the button is pressed:

- Claude Code CLI: defaults plus the user's ~/.claude/keybindings.json (re-read when it changes),
  using the Confirmation bindings while a permission prompt is open;
- Codex CLI: its fixed shortcuts;
- Claude desktop app: fixed keys, plus the dictation shortcut from the app's
  claude_desktop_config.json ("preferences.dictationShortcut").
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# Our action names -> Claude Code action ids. The second id is used during permission prompts.
CLAUDE_ACTIONS: Dict[str, Tuple[str, Optional[str]]] = {
    "submit": ("chat:submit", "confirm:yes"),
    "cancel": ("chat:cancel", "confirm:no"),
    "cycle_mode": ("chat:cycleMode", "confirm:cycleMode"),
    "transcript": ("app:toggleTranscript", None),
    "review": ("app:toggleTranscript", None),
    "todos": ("app:toggleTodos", None),
    "history_search": ("history:search", None),
    "clear_input": ("chat:clearInput", None),
    "external_editor": ("chat:externalEditor", None),
    "interrupt": ("app:interrupt", None),
    "queue_submit": ("chat:queueSubmit", None),
    "kill_agents": ("chat:killAgents", None),
    "undo": ("chat:undo", None),
    "model_picker": ("chat:modelPicker", None),
    "thinking": ("chat:thinkingToggle", None),
}

# Claude Code default bindings for the contexts we use (from the keybindings docs).
CLAUDE_DEFAULTS: Dict[str, Dict[str, str]] = {
    "Global": {"ctrl+c": "app:interrupt", "ctrl+d": "app:exit", "ctrl+t": "app:toggleTodos",
               "ctrl+o": "app:toggleTranscript", "ctrl+r": "history:search"},
    "Chat": {"escape": "chat:cancel", "shift+tab": "chat:cycleMode", "enter": "chat:submit",
             "ctrl+l": "chat:clearInput", "ctrl+g": "chat:externalEditor",
             "ctrl+x ctrl+e": "chat:externalEditor", "ctrl+j": "chat:newline",
             "ctrl+x enter": "chat:queueSubmit", "ctrl+x ctrl+k": "chat:killAgents", "ctrl+_": "chat:undo",
             "meta+p": "chat:modelPicker", "meta+t": "chat:thinkingToggle"},
    "Confirmation": {"enter": "confirm:yes", "escape": "confirm:no", "shift+tab": "confirm:cycleMode"},
}

# Fixed keys (not in keybindings.json) per destination.
CLAUDE_FIXED: Dict[str, List[str]] = {
    "rewind": ["esc", "esc"], "new_session": ["text:/clear", "enter"],
}

CODEX_KEYS: Dict[str, List[str]] = {
    "submit": ["enter"], "cancel": ["esc"], "cycle_mode": ["shift+tab"], "transcript": ["ctrl+t"],
    "history_search": ["ctrl+r"], "interrupt": ["ctrl+c"], "rewind": ["esc", "esc"],
    "review": ["ctrl+t"], "new_session": ["text:/new", "enter"],
}

DESKTOP_KEYS: Dict[str, List[str]] = {
    "submit": ["enter"], "cancel": ["esc"], "interrupt": ["esc"], "clear_input": ["cmd+a", "backspace"],
    # Shortcuts of the Claude app (its menus and its keyboard-shortcut help).
    "review": ["cmd+shift+d"], "new_session": ["cmd+n"], "menu": ["cmd+k"], "rewind": ["esc", "esc"],
    "prev_session": ["cmd+shift+["], "next_session": ["cmd+shift+]"],
    "queue_submit": ["cmd+enter"], "undo": ["cmd+z"],
    "model_picker": ["cmd+shift+i"], "thinking": ["cmd+shift+e"],
}

# Every action a mapping can name ("act:<name>").
ACTIONS = ("submit", "cancel", "clear_input", "cycle_mode", "transcript", "todos", "history_search",
           "external_editor", "interrupt", "rewind", "dictation", "review", "new_session", "menu",
           "prev_session", "next_session", "queue_submit", "kill_agents", "undo", "model_picker", "thinking")

_MOD_ALIASES = {"control": "ctrl", "ctrl": "ctrl", "opt": "alt", "option": "alt", "alt": "alt",
                "meta": "alt", "shift": "shift", "cmd": "cmd", "command": "cmd", "super": "cmd", "win": "cmd"}
_KEY_ALIASES = {"escape": "esc", "return": "enter"}


def normalize_keystroke(stroke: str) -> Optional[str]:
    """Claude Code keystroke ("ctrl+X", "Escape", "meta+p") -> our key name, or None."""
    parts = [p for p in stroke.strip().lower().split("+")]
    if not parts or not parts[-1]:
        return None
    base = _KEY_ALIASES.get(parts[-1], parts[-1])
    mods = []
    for m in parts[:-1]:
        if m not in _MOD_ALIASES:
            return None
        mods.append(_MOD_ALIASES[m])
    order = [m for m in ("cmd", "ctrl", "alt", "shift") if m in mods]
    return "+".join(order + [base])


def claude_keybindings_path() -> Path:
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(root) / "keybindings.json"


def desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


class _Cached:
    """A JSON file parsed again only when its mtime changes."""

    def __init__(self, path_fn: Callable[[], Path]):
        self.path_fn = path_fn
        self.sig = None
        self.data = None
        self.error: Optional[str] = None

    def get(self):
        path = self.path_fn()
        try:
            st = path.stat()
            sig = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            sig = (str(path), None, None)
        if sig != self.sig:
            self.sig = sig
            self.data, self.error = None, None
            if sig[1] is not None:
                try:
                    self.data = json.loads(path.read_text())
                except (OSError, ValueError) as e:
                    self.error = f"{path}: {e}"
        return self.data


class Resolver:
    def __init__(self, claude_path_fn=claude_keybindings_path, desktop_path_fn=desktop_config_path,
                 encodable: Optional[Callable[[str], bool]] = None, desktop_encodable=None):
        self.claude_file = _Cached(claude_path_fn)
        self.desktop_file = _Cached(desktop_path_fn)
        self.encodable = encodable or (lambda k: True)
        self.desktop_encodable = desktop_encodable or (lambda k: True)

    # ------------------------------------------------------------ Claude Code CLI
    def claude_effective(self) -> Dict[str, Dict[str, Optional[str]]]:
        """Context -> {keystroke: action} after applying the user's file over the defaults."""
        eff: Dict[str, Dict[str, Optional[str]]] = {c: dict(b) for c, b in CLAUDE_DEFAULTS.items()}
        data = self.claude_file.get()
        blocks = data.get("bindings") if isinstance(data, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict) or not isinstance(block.get("bindings"), dict):
                continue
            ctx = eff.setdefault(str(block.get("context", "")), {})
            for stroke, action in block["bindings"].items():
                key = " ".join(normalize_keystroke(s) or "?" for s in str(stroke).split())
                ctx[key] = action if isinstance(action, str) else None
        # Normalize the defaults' spelling too ("escape" -> "esc").
        return {c: {" ".join(normalize_keystroke(s) or "?" for s in k.split()): a for k, a in b.items()}
                for c, b in eff.items()}

    def _claude_keys(self, action_id: str) -> Optional[List[str]]:
        eff = self.claude_effective()
        home = next((c for c, b in CLAUDE_DEFAULTS.items() if action_id in b.values()), "Chat")
        defaults = [" ".join(normalize_keystroke(s) or "?" for s in k.split())
                    for k, a in CLAUDE_DEFAULTS.get(home, {}).items() if a == action_id]
        cands = [k for k, a in eff.get(home, {}).items() if a == action_id]
        if home == "Global":  # a Chat binding of the same key shadows the global one
            cands = [k for k in cands if eff.get("Chat", {}).get(k) in (None, action_id)]
        cands.sort(key=lambda k: (k not in defaults, k.count(" ")))
        for k in cands:
            seq = k.split()
            if "?" not in seq and all(self.encodable(s) for s in seq):
                return seq
        return None

    # ------------------------------------------------------------ desktop app
    def desktop_dictation(self) -> Tuple[Optional[List[str]], str]:
        data = self.desktop_file.get()
        prefs = data.get("preferences") if isinstance(data, dict) else None
        val = prefs.get("dictationShortcut", "default") if isinstance(prefs, dict) else "default"
        if val == "off":
            return None, "dictation is turned off in Claude settings"
        if isinstance(val, dict) and isinstance(val.get("accelerator"), str):
            key = parse_accelerator(val["accelerator"])
            if key and self.desktop_encodable(key):
                return [key], "custom (Claude settings)"
            return None, f"cannot send Claude's dictation shortcut {val['accelerator']!r}"
        return (["cmd+d"] if sys.platform == "darwin" else ["ctrl+d"]), "default"

    # ------------------------------------------------------------ entry point
    def resolve(self, action: str, dest: str, permission: bool = False) -> Optional[List[str]]:
        """Keys (a sequence) to send for `action` to `dest` ("claude", "codex", "desktop",
        or another wrapped command name). None if the destination has no such action."""
        if dest == "desktop":
            if action == "dictation":
                return self.desktop_dictation()[0]
            return list(DESKTOP_KEYS[action]) if action in DESKTOP_KEYS else None
        if dest == "claude":
            if action in CLAUDE_FIXED:
                return list(CLAUDE_FIXED[action])
            ids = CLAUDE_ACTIONS.get(action)
            if not ids:
                return None
            aid = ids[1] if permission and ids[1] else ids[0]
            keys = self._claude_keys(aid)
            if action == "clear_input" and keys in (None, ["ctrl+l"]):
                # Claude Code 2.1.310 leaves the input as it is on Ctrl+L (tested); Ctrl+U
                # clears one line, so repeat it with Backspace to clear every line.
                return ["kill_input"]
            return keys
        return list(CODEX_KEYS[action]) if action in CODEX_KEYS else None

    def describe(self) -> dict:
        """What was detected, for `dsagent keys` / status."""
        self.claude_file.get()
        self.desktop_file.get()
        dict_keys, dict_src = self.desktop_dictation()
        return {
            "claude_keybindings": str(self.claude_file.path_fn()),
            "claude_keybindings_found": self.claude_file.data is not None,
            "claude_keybindings_error": self.claude_file.error,
            "desktop_config": str(self.desktop_file.path_fn()),
            "desktop_config_found": self.desktop_file.data is not None,
            "desktop_dictation": "+".join(dict_keys) if dict_keys else None,
            "desktop_dictation_source": dict_src,
            "desktop_config_error": self.desktop_file.error,
        }


def parse_accelerator(acc: str) -> Optional[str]:
    """Electron accelerator ("CommandOrControl+Shift+D") -> our key name ("cmd+shift+d")."""
    mods, base = [], None
    for tok in acc.replace(" ", "").split("+"):
        t = tok.lower()
        if t in ("cmd", "command", "commandorcontrol", "cmdorctrl", "super", "meta"):
            mods.append("cmd" if sys.platform == "darwin" or t in ("cmd", "command", "super", "meta") else "ctrl")
        elif t in ("ctrl", "control"):
            mods.append("ctrl")
        elif t in ("alt", "option", "altgr"):
            mods.append("alt")
        elif t == "shift":
            mods.append("shift")
        elif base is None and t:
            base = {"return": "enter", "escape": "esc", "space": "space"}.get(t, t)
        else:
            return None
    if not base:
        return None
    return "+".join([m for m in ("cmd", "ctrl", "alt", "shift") if m in mods] + [base])
