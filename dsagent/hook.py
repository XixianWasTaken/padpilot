"""Hook entry point shared by Claude Code and Codex (both use the same hook JSON on stdin).

Never fails the agent: any error results in exit 0 with no output.
"""
from __future__ import annotations

import json
import os
import sys

from . import client
from . import config as C

START_ON = {"SessionStart", "UserPromptSubmit", "PermissionRequest"}
KEEP_FIELDS = ("tool_name", "notification_type", "source", "is_interrupt", "error", "trigger", "reason",
               "transcript_path")
MAX_PROMPT = 20000


def is_choice_tool(name) -> bool:
    return isinstance(name, str) and (name == "wrap_up" or name.endswith("__wrap_up"))


def run_hook(source: str) -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    try:
        _dispatch(source, payload)
    except Exception as e:  # pragma: no cover - defensive
        try:
            with open(C.HOME / "hook-errors.log", "a") as f:
                f.write(f"{source}: {e!r}\n")
        except OSError:
            pass
    return 0


def _dispatch(source: str, p: dict) -> None:
    event = p.get("hook_event_name") or ""
    if not event:
        return
    msg = {
        "op": "event",
        "source": source,
        "event": event,
        "session": p.get("session_id") or "",
        "cwd": p.get("cwd") or "",
        "wrapper": os.environ.get("DSAGENT_WRAPPER_ID") or None,
        "data": {k: p[k] for k in KEEP_FIELDS if k in p},
    }
    # The last prompt, for the retry button (kept in the daemon's memory only).
    if event == "UserPromptSubmit" and isinstance(p.get("prompt"), str):
        msg["data"]["prompt"] = p["prompt"][:MAX_PROMPT]
    # The reply buttons of the Claude desktop app's next-step card, for R1 + face buttons.
    ti = p.get("tool_input")
    if event == "PreToolUse" and is_choice_tool(p.get("tool_name")) and isinstance(ti, dict):
        msg["data"]["tool_input"] = {"actions": [
            {"label": str(a.get("label", ""))[:200], "action": a.get("action")}
            for a in (ti.get("actions") or [])[:4] if isinstance(a, dict)]}
    if event in START_ON:
        client.start_daemon()

    if event == "PermissionRequest" and not msg["wrapper"]:
        cfg = C.load_config()
        timeout = float(cfg.get("hook_approval_timeout_s", 0) or 0)
        if timeout > 0:
            msg["op"] = "approval"
            msg["timeout"] = timeout
            resp = client.request(msg, timeout=timeout + 3)
            decision = (resp or {}).get("decision")
            if decision in ("allow", "deny"):
                d = {"behavior": decision}
                if decision == "deny":
                    d["message"] = "Denied with the DualSense controller."
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}))
            return
    client.request(msg, timeout=0.5)


def run_codex_notify(argv) -> int:
    """Legacy Codex `notify = [...]` support: the JSON payload is the last argv element."""
    try:
        p = json.loads(argv[-1]) if argv else {}
    except Exception:
        return 0
    if p.get("type") == "agent-turn-complete":
        client.request({"op": "event", "source": "codex", "event": "AgentTurnComplete",
                        "session": p.get("thread-id") or "", "cwd": p.get("cwd") or "",
                        "wrapper": os.environ.get("DSAGENT_WRAPPER_ID") or None, "data": {}})
    return 0
