"""Read-only helpers for Claude Code session transcripts (~/.claude/projects/*/<session>.jsonl).

The daemon keeps the last prompt and the next-step card in memory; these let it recover them
from the transcript after a restart, and read the context size of the last request.
"""
from __future__ import annotations

import glob
import json
import os
from typing import List, Optional, Tuple

TAIL_BYTES = 1_000_000
TAIL_MAX = 64_000_000


def projects_dir() -> str:
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(root, "projects")


def latest_transcript(root: Optional[str] = None) -> Optional[str]:
    """The most recently written main-session transcript (sub-agents live one level deeper)."""
    files = glob.glob(os.path.join(root or projects_dir(), "*", "*.jsonl"))
    best, best_t = None, -1.0
    for f in files:
        try:
            t = os.stat(f).st_mtime
        except OSError:
            continue
        if t > best_t:
            best, best_t = f, t
    return best


def _tail(path: Optional[str]) -> List[dict]:
    """The JSON entries at the end of the transcript. The window grows (up to TAIL_MAX) while it holds
    no complete entry, so one long line (a big tool result) does not hide everything."""
    if not path:
        return []
    window = TAIL_BYTES
    while True:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - window))
                data = f.read()
        except OSError:
            return []
        lines = data.split(b"\n")
        if size > window:
            lines = lines[1:]  # first line is probably cut
        out = []
        for line in lines:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict):
                out.append(m)
        if out or size <= window or window >= TAIL_MAX:
            return out
        window *= 4


def _msg(m: dict) -> dict:
    v = m.get("message")
    return v if isinstance(v, dict) else {}


def _user_text(m: dict) -> Optional[str]:
    """The text of a message the user typed (not tool results, commands or reminders)."""
    if m.get("type") != "user" or m.get("isMeta") or m.get("isSidechain"):
        return None
    c = _msg(m).get("content")
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    if not isinstance(c, str) or not c.strip() or c.lstrip().startswith("<"):
        return None
    return c


def last_prompt(path: Optional[str]) -> Optional[str]:
    entries = _tail(path)
    for m in reversed(entries):
        if m.get("type") == "last-prompt" and isinstance(m.get("lastPrompt"), str) and m["lastPrompt"].strip():
            return m["lastPrompt"]
    for m in reversed(entries):
        t = _user_text(m)
        if t:
            return t
    return None


def last_card(path: Optional[str]) -> List[Tuple[str, bool]]:
    """Buttons of the last next-step card, if no message was sent after it: [(label, is_app_action)]."""
    for m in reversed(_tail(path)):
        if _user_text(m):
            return []
        if m.get("type") != "assistant":
            continue
        content = _msg(m).get("content")
        for c in reversed(content if isinstance(content, list) else []):
            if isinstance(c, dict) and c.get("type") == "tool_use" and str(c.get("name", "")).endswith("wrap_up"):
                acts = (c.get("input") or {}).get("actions") or []
                return [(str(a.get("label") or ""), bool(a.get("action"))) for a in acts if isinstance(a, dict)][:4]
    return []


def context_tokens(path: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Input tokens of the last main-thread request (what fills the context window) and its model."""
    for m in reversed(_tail(path)):
        if m.get("type") != "assistant" or m.get("isSidechain"):
            continue
        msg = _msg(m)
        u = msg.get("usage")
        if isinstance(u, dict):
            n = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                  "cache_read_input_tokens", "output_tokens"))
            return n, msg.get("model")
    return None, None
