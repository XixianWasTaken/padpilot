"""Hook entry point shared by Claude Code and Codex (both use the same hook JSON on stdin).

Never fails the agent: any error results in exit 0 with no output.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

from . import client
from . import config as C

START_ON = {"SessionStart", "UserPromptSubmit", "PermissionRequest"}
KEEP_FIELDS = ("tool_name", "notification_type", "source", "is_interrupt", "error", "trigger", "reason",
               "transcript_path", "tool_use_id")
MAX_PROMPT = 20000


QUESTION_TOOL = "AskUserQuestion"  # the multiple-choice question box
PLAN_TOOL = "ExitPlanMode"         # leaving plan mode: "may I go ahead with this plan?"


def call_key(p: dict) -> str:
    """Names a tool call by its tool and input, without passing the input on. A permission
    request carries no tool_use_id, but the same tool and input as the PreToolUse just before
    it, which does: that is how the daemon tells which call a request is for."""
    try:
        raw = json.dumps([p.get("tool_name"), p.get("tool_input")], sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def question_input(ti) -> dict:
    """The parts of an AskUserQuestion input padpilot needs: option labels and multiSelect."""
    qs = []
    for q in (ti.get("questions") or [])[:4] if isinstance(ti, dict) else []:
        if isinstance(q, dict):
            qs.append({"multiSelect": bool(q.get("multiSelect")),
                       "options": [str(o.get("label", ""))[:200] for o in (q.get("options") or [])[:4]
                                   if isinstance(o, dict)]})
    return {"questions": qs}


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
        "wrapper": os.environ.get("PADPILOT_WRAPPER_ID") or None,
        "data": {k: p[k] for k in KEEP_FIELDS if k in p},
    }
    # Where the session runs, as Claude Code tells its hooks: the entry point (the desktop app,
    # a terminal, an editor) and whether someone attends it. `claude -p` is not attended, also
    # when it is started inside a desktop session and so inherits that entry point.
    msg["entry"] = os.environ.get("CLAUDE_CODE_ENTRYPOINT") or ""
    msg["attended"] = {"1": True, "0": False}.get(os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") or "")
    if msg["attended"] is False and not C.load_config().get("background_sessions"):
        return  # nobody is watching it: the controller stays out of it
    # The last prompt, for the retry button (kept in the daemon's memory only).
    if event == "UserPromptSubmit" and isinstance(p.get("prompt"), str):
        msg["data"]["prompt"] = p["prompt"][:MAX_PROMPT]
    # The reply buttons of the Claude desktop app's next-step card, for R1 + face buttons.
    ti = p.get("tool_input")
    if event == "PreToolUse" and is_choice_tool(p.get("tool_name")) and isinstance(ti, dict):
        msg["data"]["tool_input"] = {"actions": [
            {"label": str(a.get("label", ""))[:200], "action": a.get("action")}
            for a in (ti.get("actions") or [])[:4] if isinstance(a, dict)]}
    # The options of a question box, so R1 + face buttons can pick them.
    if event == "PreToolUse" and p.get("tool_name") == QUESTION_TOOL and isinstance(ti, dict):
        msg["data"]["tool_input"] = question_input(ti)
    if event in ("PreToolUse", "PermissionRequest") and p.get("tool_name"):
        msg["data"]["call"] = call_key(p)  # ("" if the input cannot be named: then it is not used)
    if event in START_ON:
        client.start_daemon()

    # A question box also asks for permission, but it is answered in the box, not approved:
    # don't hold it, or the first ✕ meant for the box would approve it.
    if event == "PermissionRequest" and not msg["wrapper"] and p.get("tool_name") != QUESTION_TOOL:
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
                elif p.get("tool_name") == PLAN_TOOL:
                    # Claude Code takes "allow" for a plan only with the tool's input handed
                    # back; a bare "allow" leaves the plan waiting on screen (2.1.325 and
                    # 2.1.326, tested). The session then returns to the mode it had before.
                    d["updatedInput"] = ti if isinstance(ti, dict) else {}
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
                        "wrapper": os.environ.get("PADPILOT_WRAPPER_ID") or None, "data": {}})
    return 0
