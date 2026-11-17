"""Context size of a Claude Code session, from its transcript (the path a hook event names).

Hooks do not report token counts, so this reads the end of the transcript and takes only the
token numbers and model id of the last request; no message text is used or kept.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional, Tuple

TAIL_BYTES = 1_000_000
TAIL_MAX = 64_000_000


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
