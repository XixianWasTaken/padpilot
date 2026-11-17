import os
import struct
import sys
import tempfile
import time
import unittest
import json
import socket
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PADPILOT_HOME", tempfile.mkdtemp(prefix="padpilot-test-"))
# Never read the real ~/.claude (transcripts, keybindings).
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="padpilot-claude-")

from padpilot import config as C  # noqa: E402
from padpilot.controls import ControlTracker  # noqa: E402
from padpilot.daemon import Daemon, Target  # noqa: E402
from padpilot.dualsense import (PLAYER_PATTERNS, InputState, OutputState, build_bt_report,  # noqa: E402
                               build_usb_report, parse_input, trigger_feedback, trigger_weapon)
from padpilot.effects import RumblePlayer, light_at  # noqa: E402
from padpilot.keys import encode_key, split_action, to_items  # noqa: E402
from padpilot.macos import CMD, CTRL, SHIFT, key_event  # noqa: E402
from padpilot.bindings import ACTIONS, Resolver, normalize_keystroke, parse_accelerator  # noqa: E402
from padpilot.daemon import _can  # noqa: E402
from pathlib import Path  # noqa: E402


def make_resolver(kb=None, desktop=None):
    """Resolver on temp files (never the real user's settings)."""
    tmp = Path(tempfile.mkdtemp(prefix="padpilot-kb-"))
    if kb is not None:
        (tmp / "keybindings.json").write_text(kb if isinstance(kb, str) else json.dumps(kb))
    if desktop is not None:
        (tmp / "desk.json").write_text(desktop if isinstance(desktop, str) else json.dumps(desktop))
    r = Resolver(claude_path_fn=lambda: tmp / "keybindings.json", desktop_path_fn=lambda: tmp / "desk.json",
                 encodable=lambda k: _can(encode_key, k), desktop_encodable=lambda k: _can(key_event, k))
    r.tmp = tmp
    return r


def check_mapping_steps(validate):
    for prof in [C.DEFAULTS["mapping"]] + list(C.DEFAULTS["profiles"].values()):
        for action in prof.values():
            for st in split_action(action):
                if st.startswith("act:"):
                    assert st[4:].rstrip("?") in ACTIONS, st
                elif not st.startswith("@") and not st.startswith("text:"):
                    validate(st)

# A real USB input report captured from the controller (sticks centred, nothing pressed).
USB_IDLE = bytes.fromhex("01807e7f7c0000220800000019befeb9") + bytes(37) + bytes([0x29]) + bytes(10)


def usb_report(b0=0x08, b1=0, b2=0, lx=128, ly=128, rx=128, ry=128, l2=0, r2=0, touch=None):
    r = bytearray(64)
    r[0] = 0x01
    r[1], r[2], r[3], r[4], r[5], r[6] = lx, ly, rx, ry, l2, r2
    r[8], r[9], r[10] = b0, b1, b2
    if touch is None:
        r[33] = 0x80
    else:
        x, y = touch
        r[33] = 0x01
        r[34] = x & 0xFF
        r[35] = ((x >> 8) & 0x0F) | ((y & 0x0F) << 4)
        r[36] = (y >> 4) & 0xFF
    r[37] = 0x80
    r[53] = 0x15  # 55 %, charging
    return bytes(r)


class ProtocolTest(unittest.TestCase):
    def test_parse_captured_idle(self):
        s = parse_input(USB_IDLE)
        self.assertIsNotNone(s)
        self.assertFalse(any(s.buttons.values()))
        self.assertEqual(s.battery, 100)
        self.assertEqual(s.charging, "full")

    def test_parse_buttons(self):
        s = parse_input(usb_report(b0=0x20 | 0x02, b1=0x01 | 0x20, b2=0x01))
        on = {k for k, v in s.buttons.items() if v}
        self.assertEqual(on, {"cross", "right", "l1", "options", "ps"})
        self.assertEqual(s.battery, 55)
        self.assertEqual(s.charging, "charging")

    def test_parse_touch(self):
        s = parse_input(usb_report(touch=(1500, 900)))
        self.assertTrue(s.touches[0].active)
        self.assertEqual((s.touches[0].x, s.touches[0].y), (1500, 900))
        self.assertFalse(s.touches[1].active)

    def test_parse_bt_full_and_simple(self):
        usb = usb_report(b0=0x40)
        bt = bytes([0x31, 0x00]) + usb[1:] + bytes(78 - 1 - 63)
        self.assertTrue(parse_input(bt[:78]).buttons["circle"])
        simple = bytes([0x01, 128, 128, 128, 128, 0x88, 0x00, 0x00, 0, 0])
        s = parse_input(simple)
        self.assertTrue(s.buttons["triangle"])
        self.assertFalse(s.full_report)

    def test_usb_output_layout(self):
        o = OutputState(rgb=(1, 2, 3), rumble_left=40, rumble_right=50, player_leds=0b00100, mic_led=1,
                        trigger_right=trigger_weapon(3, 6, 7))
        r = build_usb_report(o)
        self.assertEqual(len(r), 48)
        self.assertEqual(r[0], 0x02)
        self.assertEqual(r[3], 50)   # motor right
        self.assertEqual(r[4], 40)   # motor left
        self.assertEqual(r[9], 1)    # mic led
        self.assertEqual(r[11], 0x25)  # right trigger mode
        self.assertEqual(r[44], 0b00100)
        self.assertEqual(tuple(r[45:48]), (1, 2, 3))

    def test_bt_output_crc(self):
        r = build_bt_report(OutputState(rgb=(9, 8, 7)), seq=5)
        self.assertEqual(len(r), 78)
        self.assertEqual(r[0], 0x31)
        self.assertEqual(r[1], 0x50)
        self.assertEqual(tuple(r[3 + 44:3 + 47]), (9, 8, 7))
        crc = struct.unpack("<I", r[74:78])[0]
        self.assertEqual(crc, zlib.crc32(b"\xa2" + r[:74]) & 0xFFFFFFFF)

    def test_trigger_encodings(self):
        w = trigger_weapon(3, 6, 7)
        self.assertEqual(w[:4], bytes([0x25, 0x48, 0x00, 6]))
        f = trigger_feedback(2, 3)
        self.assertEqual(f[0], 0x21)
        self.assertEqual(f[1] | f[2] << 8, 0b1111111100)
        self.assertEqual(len(f), 11)


class KeysTest(unittest.TestCase):
    def test_encode(self):
        self.assertEqual(encode_key("enter"), "\r")
        self.assertEqual(encode_key("shift+tab"), "\x1b[Z")
        self.assertEqual(encode_key("up"), "\x1b[A")
        self.assertEqual(encode_key("up", app_cursor=True), "\x1bOA")
        self.assertEqual(encode_key("ctrl+c"), "\x03")
        self.assertEqual(encode_key("ctrl+o"), "\x0f")
        with self.assertRaises(KeyError):
            encode_key("hyper+q")
        self.assertEqual(encode_key("esc", kitty_flags=1), "\x1b[27u")
        self.assertEqual(encode_key("ctrl+c", kitty_flags=1), "\x1b[99;5u")
        self.assertEqual(encode_key("enter", kitty_flags=1), "\r")
        self.assertEqual(encode_key("shift+tab", kitty_flags=1), "\x1b[Z")

    def test_default_mapping_is_valid(self):
        check_mapping_steps(encode_key)  # raises on unknown keys / actions


class ControlsTest(unittest.TestCase):
    def make(self, holds=("ps",)):
        return ControlTracker(C.DEFAULTS, lambda n: n in holds)

    def feed(self, tr, report, t):
        return tr.update(parse_input(report), t)

    def test_press_once(self):
        tr = self.make()
        self.assertEqual(self.feed(tr, usb_report(b0=0x28), 0.0), ["cross"])
        self.assertEqual(self.feed(tr, usb_report(b0=0x28), 0.1), [])
        self.assertEqual(self.feed(tr, usb_report(), 0.2), [])

    def test_dpad_repeat(self):
        tr = self.make()
        self.assertEqual(self.feed(tr, usb_report(b0=0x00), 0.0), ["up"])
        self.assertEqual(self.feed(tr, usb_report(b0=0x00), 0.2), [])
        self.assertEqual(self.feed(tr, usb_report(b0=0x00), 0.36), ["up"])
        self.assertEqual(self.feed(tr, usb_report(b0=0x00), 0.44), ["up"])

    def test_hold_vs_tap(self):
        tr = self.make()
        self.assertEqual(self.feed(tr, usb_report(b2=0x01), 0.0), [])
        self.assertEqual(self.feed(tr, usb_report(), 0.1), ["ps"])
        self.assertEqual(self.feed(tr, usb_report(b2=0x01), 1.0), [])
        self.assertEqual(self.feed(tr, usb_report(b2=0x01), 1.7), ["ps_hold_ready"])
        self.assertEqual(self.feed(tr, usb_report(b2=0x01), 1.8), [])
        self.assertEqual(self.feed(tr, usb_report(), 1.9), ["ps_hold"])

    def test_modifier_and_button_in_one_report(self):
        tr = self.make()
        self.assertEqual(self.feed(tr, usb_report(b0=0x28, b1=0x02), 0.0), ["r1+cross"])
        self.assertEqual(self.feed(tr, usb_report(), 0.1), [])  # no R1 tap (/) afterwards

    def test_wheel_takes_a_stick_already_pushed(self):
        tr = self.make()
        self.assertEqual(self.feed(tr, usb_report(ly=0), 0.0), ["stick_up"])
        out = []
        for i, t in enumerate((0.1, 0.5, 0.6, 0.7, 0.8)):
            out += self.feed(tr, usb_report(b0=0x88, ly=0), t)  # △ held, stick still up
        self.assertNotIn("stick_up", out)  # no arrow repeats while it steers the wheel
        self.assertEqual(self.feed(tr, usb_report(ly=0), 0.9), ["wheel_pick:0"])
        self.assertEqual(self.feed(tr, usb_report(), 1.0), [])

    def test_r1_triangle_combo_does_not_steer_the_wheel(self):
        tr = self.make()
        self.feed(tr, usb_report(b1=0x02), 0.0)
        self.assertEqual(self.feed(tr, usb_report(b0=0x88, b1=0x02), 0.1), ["r1+triangle"])
        self.assertEqual(self.feed(tr, usb_report(b0=0x88), 0.2), [])          # R1 up, △ held
        self.assertEqual(self.feed(tr, usb_report(b0=0x88, rx=255), 0.3), ["rstick_right"])
        self.assertEqual(self.feed(tr, usb_report(), 0.4), [])
        self.assertIsNone(tr.wheel)

    def test_combos(self):
        tr = self.make()
        self.assertEqual(self.feed(tr, usb_report(b2=0x01), 0.0), [])             # PS down
        self.assertEqual(self.feed(tr, usb_report(b0=0x00, b2=0x01), 0.1), ["ps+up"])
        self.assertEqual(self.feed(tr, usb_report(b0=0x00, b2=0x01), 0.5), ["ps+up"])  # repeats
        self.assertEqual(self.feed(tr, usb_report(b2=0x01), 0.6), [])
        self.assertEqual(self.feed(tr, usb_report(b2=0x01), 1.5), [])             # no hold after a combo
        self.assertEqual(self.feed(tr, usb_report(), 1.6), [])                    # nor a tap
        self.assertEqual(self.feed(tr, usb_report(b1=0x02), 2.0), [])             # R1 down
        self.assertEqual(self.feed(tr, usb_report(b0=0x28, b1=0x02), 2.1), ["r1+cross"])
        self.assertEqual(self.feed(tr, usb_report(b1=0x02), 2.2), [])
        self.assertEqual(self.feed(tr, usb_report(), 2.3), [])
        self.assertEqual(self.feed(tr, usb_report(b1=0x02), 3.0), [])
        self.assertEqual(self.feed(tr, usb_report(), 3.1), ["r1"])                # plain tap
        self.assertEqual(self.feed(tr, usb_report(b1=0x01), 4.0), [])             # L1 down
        self.assertEqual(self.feed(tr, usb_report(b0=0x18, b1=0x01), 4.1), ["l1+square"])
        self.assertEqual(self.feed(tr, usb_report(), 4.2), [])
        self.assertEqual(self.feed(tr, usb_report(b1=0x01), 5.0), [])
        self.assertEqual(self.feed(tr, usb_report(), 5.1), ["l1"])
        # Right stick left/right (session switching) does not repeat.
        self.assertEqual(self.feed(tr, usb_report(rx=255), 6.0), ["rstick_right"])
        self.assertEqual(self.feed(tr, usb_report(rx=255), 7.0), [])
        self.assertEqual(self.feed(tr, usb_report(), 7.1), [])

    def test_prompt_wheel(self):
        tr = self.make()
        tri = 0x88
        self.assertEqual(self.feed(tr, usb_report(b0=tri), 0.0), [])
        self.assertEqual(self.feed(tr, usb_report(b0=tri, ry=0), 0.1), ["wheel:0"])
        self.assertEqual(self.feed(tr, usb_report(b0=tri, rx=255), 0.2), ["wheel:2"])
        self.assertEqual(self.feed(tr, usb_report(b0=tri, rx=255), 0.3), [])
        self.assertEqual(self.feed(tr, usb_report(b0=tri), 0.4), [])              # stick back: keep 2
        self.assertEqual(self.feed(tr, usb_report(), 0.5), ["wheel_pick:2"])
        self.assertEqual(self.feed(tr, usb_report(b0=tri), 1.0), [])
        self.assertEqual(self.feed(tr, usb_report(), 1.1), ["triangle"])          # tap = continue
        # The left stick steers too (and sends no arrow keys meanwhile).
        self.assertEqual(self.feed(tr, usb_report(b0=tri), 2.0), [])
        self.assertEqual(self.feed(tr, usb_report(b0=tri, lx=255), 2.1), ["wheel:2"])
        self.assertEqual(self.feed(tr, usb_report(b0=tri, lx=255), 2.6), [])
        self.assertEqual(self.feed(tr, usb_report(b0=tri), 2.7), [])
        self.assertEqual(self.feed(tr, usb_report(), 2.8), ["wheel_pick:2"])
        self.assertEqual(self.feed(tr, usb_report(), 2.9), [])
        # Held without picking: tick at hold_ms, release cancels (no "continue").
        self.assertEqual(self.feed(tr, usb_report(b0=tri), 3.0), [])
        self.assertEqual(self.feed(tr, usb_report(b0=tri), 3.7), ["triangle_hold_ready"])
        self.assertEqual(self.feed(tr, usb_report(), 4.9), [])

    def test_push_to_talk(self):
        tr = self.make()
        self.assertEqual(self.feed(tr, usb_report(b2=0x04), 0.0), ["mic"])
        self.assertEqual(self.feed(tr, usb_report(), 0.2), [])                    # tap: toggle only
        self.assertEqual(self.feed(tr, usb_report(b2=0x04), 1.0), ["mic"])
        self.assertEqual(self.feed(tr, usb_report(), 1.8), ["mic_up"])            # held: stop on release

    def test_trigger_full_and_stick(self):
        tr = self.make()
        self.assertEqual(self.feed(tr, usb_report(r2=250), 0.0), ["r2_full"])
        self.assertEqual(self.feed(tr, usb_report(r2=200), 0.1), [])  # hysteresis keeps it held
        self.assertEqual(self.feed(tr, usb_report(r2=10), 0.2), [])
        self.assertEqual(self.feed(tr, usb_report(ly=0), 0.3), ["stick_up"])
        self.assertEqual(self.feed(tr, usb_report(rx=255), 0.4), ["rstick_right"])

    def test_swipe(self):
        tr = self.make()
        self.feed(tr, usb_report(touch=(200, 500)), 0.0)
        self.feed(tr, usb_report(touch=(900, 520)), 0.05)
        self.assertEqual(self.feed(tr, usb_report(), 0.1), ["swipe_right"])


class EffectsTest(unittest.TestCase):
    def test_light(self):
        self.assertEqual(light_at({"kind": "solid", "color": [1, 2, 3]}, 5), (1, 2, 3))
        b0 = light_at({"kind": "breathe", "color": [0, 0, 200], "period": 2, "min": 0.2}, 0)
        b1 = light_at({"kind": "breathe", "color": [0, 0, 200], "period": 2, "min": 0.2}, 1)
        self.assertEqual(b0, (0, 0, 200))
        self.assertEqual(b1, (0, 0, 40))

    def test_rumble(self):
        p = RumblePlayer()
        p.play([[100, 0, 100], [0, 50, 100]], 10.0)
        self.assertEqual(p.at(10.05), (100, 0))
        self.assertEqual(p.at(10.15), (0, 50))
        self.assertEqual(p.at(10.25), (0, 0))
        self.assertFalse(p.active)


class FakeConn:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)


NO_DESKTOP = (lambda: None, lambda prompt=False: False, lambda items, gap: None)


class DaemonLogicTest(unittest.TestCase):
    def setUp(self):
        self.d = Daemon(C.load_config(), None, desktop=NO_DESKTOP, mic_probe=lambda: False,
                        resolver=make_resolver())

    def ev(self, event, **data):
        self.d.handle_event({"op": "event", "source": "claude", "event": event, "session": "s1",
                             "cwd": "/tmp/proj", "wrapper": data.pop("wrapper", None), "data": data})

    def test_done_survives_idle_prompt_and_late_events(self):
        self.ev("UserPromptSubmit")
        self.ev("Stop")
        s = self.d.sessions["claude:s1"]
        self.ev("Notification", notification_type="idle_prompt")
        self.ev("PostToolUse")
        self.assertEqual(s.state, "done")

    def test_compact_keeps_the_turn_busy(self):
        self.ev("UserPromptSubmit")
        self.ev("SessionStart", source="compact")
        self.assertEqual(self.d.sessions["claude:s1"].state, "thinking")

    def test_odd_config_values_keep_the_defaults(self):
        cfg = C.deep_merge(C.DEFAULTS, {"mapping": None, "listening": None, "states": {"done": None},
                                        "idle_shutdown_minutes": None, "sounds": "loud"})
        self.assertEqual(cfg["mapping"], C.DEFAULTS["mapping"])
        self.assertEqual(cfg["states"]["done"], C.DEFAULTS["states"]["done"])
        self.assertEqual(cfg["idle_shutdown_minutes"], C.DEFAULTS["idle_shutdown_minutes"])
        self.assertEqual(cfg["sounds"], C.DEFAULTS["sounds"])
        self.assertEqual(C.deep_merge(C.DEFAULTS, [1]), C.DEFAULTS)
        # null still unbinds a control
        self.assertIsNone(C.deep_merge(C.DEFAULTS, {"mapping": {"cross": None}})["mapping"]["cross"])

    def test_a_bad_mapping_does_not_kill_the_input_loop(self):
        self.d.cfg["mapping"]["cross"] = 5
        self.assertIsNone(self.d._guarded(self.d.handle_control, "cross"))

    def test_controller_shared_only_when_configured(self):
        self.assertFalse(self.d._want_shared())  # exclusive: macOS must not see the PS button
        self.d.practice = Target("watch", "watch", "", FakeConn())
        self.assertFalse(self.d._want_shared())  # practice too: the page reads the daemon
        self.d.practice = None
        self.d.cfg["share_controller"] = True
        self.assertTrue(self.d._want_shared())

    def test_set_shared_open_without_the_c_switch_is_harmless(self):
        from padpilot.dualsense import set_shared_open
        class NoLib: __file__ = "/nonexistent/hid.so"
        self.assertFalse(set_shared_open(NoLib, True))

    def test_lost_controller_forgets_held_buttons(self):
        self.d.tracker.down["ps"] = 0.0
        self.d._controller_lost()
        self.assertEqual(self.d.tracker.down, {})

    def test_locked_pad_leaves_approvals_to_the_screen(self):
        self.d.dev = object()
        self.d.locked = True
        t = time.monotonic()
        self.assertIsNone(self.d.request_approval({"op": "approval", "source": "claude", "session": "s1",
                                                   "timeout": 5, "data": {"tool_name": "Bash"}}))
        self.assertLess(time.monotonic() - t, 1)

    def test_target_that_stops_reading_is_dropped(self):
        a, b = socket.socketpair()
        try:
            t = Target("w", "claude", "x", a)
            start = time.monotonic()
            ok = all(t.send({"op": "type", "items": [{"text": "x" * 20000}]}) for _ in range(40))
            self.assertFalse(ok)
            self.assertLess(time.monotonic() - start, 3)
        finally:
            a.close()
            b.close()

    def test_lifecycle(self):
        self.ev("SessionStart", source="startup")
        s = self.d.sessions["claude:s1"]
        self.assertEqual(s.state, "idle")
        self.ev("UserPromptSubmit")
        self.assertEqual(s.state, "thinking")
        self.ev("PreToolUse", tool_name="Bash")
        self.assertEqual(s.state, "tool")
        self.ev("PermissionRequest", tool_name="Bash")
        self.assertEqual(s.state, "permission")
        o = self.d.frame(s.since + 0.01)
        self.assertEqual(o.trigger_right[0], 0x25)
        self.assertGreater(o.rumble_left, 0)  # attention rumble
        self.ev("PostToolUse")
        self.assertEqual(s.state, "thinking")
        self.ev("Stop")
        self.assertEqual(s.state, "done")
        self.d.frame(s.since + 21)
        self.assertEqual(s.state, "done")  # stays green until a button is pressed
        self.assertFalse(s.reminded)
        self.d.frame(s.since + 601)
        self.assertTrue(s.reminded)  # one reminder
        self.d.handle_control("l3")
        self.assertEqual(s.state, "idle")
        self.ev("SessionEnd")
        self.assertNotIn("claude:s1", self.d.sessions)

    def test_stale_hook_sessions_expire(self):
        self.ev("UserPromptSubmit")
        s = self.d.sessions["claude:s1"]
        self.d.frame(s.updated + 11 * 60)
        self.assertEqual(s.state, "idle")
        self.d.frame(s.updated + 31 * 60)
        self.assertNotIn("claude:s1", self.d.sessions)

    def test_a_finished_session_and_its_card_wait_for_you(self):
        self.ev("UserPromptSubmit", prompt="do it")
        self.ev("PreToolUse", tool_name="mcp__ccd_turn__wrap_up", tool_input={"actions": [{"label": "Yes"}]})
        self.ev("Stop")
        s = self.d.sessions["claude:s1"]
        self.d.frame(s.updated + 31 * 60)  # back from lunch: still green
        self.assertEqual(self.d.sessions["claude:s1"].state, "done")
        self.d.handle_control("l3")  # any button: acknowledged
        self.assertEqual(s.state, "idle")
        self.d.frame(s.updated + 31 * 60)  # the card can still be answered
        self.assertEqual(self.d.sessions["claude:s1"].replies(), 1)
        self.d.frame(s.updated + 13 * 3600)  # but not for ever
        self.assertNotIn("claude:s1", self.d.sessions)

    def test_controls_go_to_focused_wrapper(self):
        a, b = FakeConn(), FakeConn()
        self.d.attach(Target("w-a", "claude", "a", a))
        self.d.attach(Target("w-b", "codex", "b", b))
        self.assertEqual(self.d.focus, "w-b")
        self.d.handle_control("cross")
        self.assertIn(b'"key": "enter"', b.sent[-1])
        self.assertEqual(a.sent, [])
        self.d.handle_control("options")  # codex profile overrides options
        self.assertIn(b'"ctrl+t"', b.sent[-1])
        self.d.handle_control("create")   # cycle focus
        self.assertEqual(self.d.focus, "w-a")
        self.d.handle_control("triangle")
        self.assertIn(b'"text": "continue"', a.sent[-1])

    def test_permission_steals_focus_and_esc_resets(self):
        a, b = FakeConn(), FakeConn()
        self.d.attach(Target("w-a", "claude", "a", a))
        self.d.attach(Target("w-b", "claude", "b", b))
        self.ev("PermissionRequest", wrapper="w-a")
        self.assertEqual(self.d.focus, "w-a")
        self.d.handle_control("circle")
        self.assertEqual(self.d.sessions["w-a"].state, "idle")

    def test_mic_lock(self):
        a = FakeConn()
        self.d.attach(Target("w-a", "claude", "a", a))
        self.d.handle_control("ps+mic")
        self.d.handle_control("cross")
        self.assertEqual(a.sent, [])
        self.assertEqual(self.d.frame(0).mic_led, 1)
        self.d.handle_control("ps+mic")
        self.d.handle_control("cross")
        self.assertEqual(len(a.sent), 1)

    def test_hook_approval(self):
        import threading
        self.d.dev = object()  # pretend a controller is connected
        result = {}
        th = threading.Thread(target=lambda: result.update(
            d=self.d.request_approval({"source": "claude", "session": "s9", "timeout": 5, "data": {}})))
        th.start()
        for _ in range(100):
            if self.d.approvals:
                break
            threading.Event().wait(0.01)
        self.d.approvals[0].created -= 1  # the request has been there for a moment
        self.d.handle_control("r2_full")
        th.join(2)
        self.assertEqual(result.get("d"), "allow")
        self.d.dev = None
        self.assertIsNone(self.d.request_approval({"source": "claude", "session": "s9", "timeout": 5}))

    def ask(self, d=None, timeout=5):
        """Start a hook approval request in a thread; returns (thread, result dict)."""
        import threading
        d = d or self.d
        result = {}
        th = threading.Thread(target=lambda: result.update(
            d=d.request_approval({"source": "claude", "session": "s9", "timeout": timeout, "data": {}})))
        th.start()
        for _ in range(200):
            if d.approvals or not th.is_alive():
                break
            time.sleep(0.005)
        return th, result

    def test_a_press_made_as_the_request_appears_does_not_answer_it(self):
        self.d.dev = object()
        th, result = self.ask()
        self.d.handle_control("cross")  # already on its way when the request appeared
        self.assertEqual(len(self.d.approvals), 1)
        self.assertEqual(self.d.flashes[-1].color, [255, 0, 0])  # the "denied" buzz
        self.d.handle_control("triangle")  # handing it to the screen is harmless: no wait
        th.join(2)
        self.assertEqual(result, {"d": None})
        th, result = self.ask()
        self.d.approvals[0].created -= 1  # a moment later the same press counts
        self.d.handle_control("cross")
        th.join(2)
        self.assertEqual(result, {"d": "allow"})

    def test_a_scripted_press_does_not_answer_a_request(self):
        self.d.dev = object()
        th, result = self.ask()
        self.d.approvals[0].created -= 1
        self.d.handle_control("cross", injected=True)  # `padpilot press cross`
        self.assertEqual(len(self.d.approvals), 1)
        self.d.cfg["scripted_input_answers_permission"] = True  # test scripts may opt in
        self.d.handle_control("cross", injected=True)
        th.join(2)
        self.assertEqual(result, {"d": "allow"})

    def test_practice_mode_hands_requests_to_the_screen(self):
        self.d.dev = object()
        th, result = self.ask()
        w = Target("watch", "", "", FakeConn())
        self.d.add_watcher(w, practice=True)  # a request already waiting goes to the screen
        th.join(2)
        self.assertEqual(result, {"d": None})
        t = time.monotonic()  # and a new one is not held at all
        self.assertIsNone(self.d.request_approval({"source": "claude", "session": "s9", "timeout": 5, "data": {}}))
        self.assertLess(time.monotonic() - t, 0.5)

    def test_no_controller_does_not_delay_a_request(self):
        d = Daemon(C.load_config(), object(), desktop=NO_DESKTOP, mic_probe=lambda: False,
                   resolver=make_resolver())  # hidapi is there, the controller is not
        d.started -= 10  # not the daemon's first seconds
        t = time.monotonic()
        self.assertIsNone(d.request_approval({"source": "claude", "session": "s9", "timeout": 45, "data": {}}))
        self.assertLess(time.monotonic() - t, 0.5)

    def test_enter_does_not_confirm_a_prompt_that_just_appeared(self):
        a, b = FakeConn(), FakeConn()
        self.d.attach(Target("w-a", "claude", "a", a))
        self.d.attach(Target("w-b", "claude", "b", b))
        self.ev("PermissionRequest", wrapper="w-a")  # focus jumps to a
        self.d.handle_control("cross")  # ✕ was meant for b
        self.assertEqual((a.sent, b.sent), ([], []))
        self.d.handle_control("down")  # other keys still reach the prompt
        self.assertIn(b'"down"', a.sent[-1])
        self.d.sessions["w-a"].since -= 1  # a moment later
        n = len(a.sent)
        self.d.handle_control("cross", injected=True)  # a scripted press is no answer either
        self.assertEqual(len(a.sent), n)
        self.d.handle_control("cross")
        self.assertIn(b'"enter"', a.sent[-1])
        self.assertEqual(self.d.sessions["w-a"].state, "tool")

    def test_socket_presses_and_text_do_not_answer_a_request(self):
        from padpilot.daemon import _serve_conn
        import threading
        conn = FakeConn()
        self.d.attach(Target("w-a", "claude", "a", conn))
        self.ev("PermissionRequest", wrapper="w-a")
        self.d.sessions["w-a"].since -= 1
        ours, theirs = socket.socketpair()
        th = threading.Thread(target=_serve_conn, args=(self.d, theirs), daemon=True)
        th.start()
        try:
            f = ours.makefile("rb")
            ours.sendall(b'{"op": "send", "text": "1"}\n')
            self.assertEqual(json.loads(f.readline()), {"ok": False})
            ours.sendall(b'{"op": "control", "name": "cross"}\n')
            self.assertEqual(json.loads(f.readline()), {"ok": True})
            self.assertEqual(conn.sent, [])
            self.ev("PostToolUse", wrapper="w-a")  # answered on the keyboard: text goes through again
            ours.sendall(b'{"op": "send", "text": "hi"}\n')
            self.assertEqual(json.loads(f.readline()), {"ok": True})
            self.assertIn(b'"text": "hi"', conn.sent[-1])
        finally:
            ours.close()
            th.join(2)


class DesktopKeysTest(unittest.TestCase):
    CLAUDE = "com.anthropic.claudefordesktop"

    def setUp(self):
        self.front = self.CLAUDE
        self.trusted = True
        self.posted = []
        fake = (lambda: self.front, lambda prompt=False: self.trusted,
                lambda items, gap: self.posted.append(items))
        self.d = Daemon(C.load_config(), None, desktop=fake, resolver=make_resolver())

    def wait_posted(self, n):
        for _ in range(100):
            if len(self.posted) >= n:
                return
            time.sleep(0.01)

    def test_key_events(self):
        self.assertEqual(key_event("enter"), (36, 0))
        self.assertEqual(key_event("shift+tab"), (48, SHIFT))
        self.assertEqual(key_event("ctrl+c"), (8, CTRL))
        self.assertEqual(key_event("cmd+l"), (37, CMD))
        self.assertEqual(key_event("/"), (44, 0))
        self.assertEqual(key_event("up"), (126, 0))
        with self.assertRaises(KeyError):
            key_event("hyper+x")

    def test_all_default_keys_have_keycodes(self):
        check_mapping_steps(key_event)
        r = make_resolver()
        for a in ACTIONS:
            for k in r.resolve(a, "desktop") or []:
                key_event(k)
            for dest in ("claude", "codex"):
                for k in r.resolve(a, dest) or []:
                    if not k.startswith("text:"):
                        encode_key(k)

    def test_buttons_go_to_allowed_frontmost_app(self):
        self.d.handle_control("cross")
        self.d.handle_control("triangle")
        self.wait_posted(2)
        self.assertEqual(self.posted, [[{"key": "enter"}], [{"text": "continue"}, {"key": "enter"}]])

    def test_message_box_gets_focus_before_typing_not_before_arrows(self):
        calls = []
        fake = (lambda: self.front, lambda prompt=False: True,
                lambda items, gap: self.posted.append((list(calls), items)))
        d = Daemon(C.load_config(), None, desktop=fake, resolver=make_resolver(),
                   desktop_focus=lambda app: calls.append(app) or "focused (was AXButton)")
        d.handle_control("down")       # arrows act on whatever has focus (menus, lists)
        d.handle_control("triangle")   # typed text and Enter belong in the message box
        d.handle_control("square")
        d.handle_control("circle")     # Esc interrupts only from the message box
        self.wait_posted(4)
        self.assertEqual(self.posted, [
            ([], [{"key": "down"}]),
            ([self.CLAUDE], [{"text": "continue"}, {"key": "enter"}]),
            ([self.CLAUDE, self.CLAUDE], [{"key": "cmd+a"}, {"key": "backspace"}]),
            ([self.CLAUDE] * 3, [{"key": "esc"}]),
        ])

    def test_question_box_options_and_submit(self):
        calls = []
        fake = (lambda: self.front, lambda prompt=False: True, lambda items, gap: self.posted.append(items))
        d = Daemon(C.load_config(), None, desktop=fake, resolver=make_resolver(),
                   desktop_question=lambda app, qs, pick: calls.append((app, qs, pick)) or "ok")
        qs = [{"multiSelect": False, "options": ["A", "B", "C"]}]
        d.handle_event({"op": "event", "source": "claude", "event": "PreToolUse", "session": "s1",
                        "data": {"tool_name": "AskUserQuestion", "tool_input": {"questions": qs}}})
        d.handle_control("r1+circle")   # option 2
        d.handle_control("cross")       # Submit
        for _ in range(100):
            if len(calls) >= 2:
                break
            time.sleep(0.01)
        self.assertEqual(calls, [(self.CLAUDE, qs, 2), (self.CLAUDE, qs, None)])
        # The box's permission request is not an approval to answer with ✕.
        d.handle_event({"op": "event", "source": "claude", "event": "PermissionRequest", "session": "s1",
                        "data": {"tool_name": "AskUserQuestion"}})
        self.assertEqual(d.sessions["claude:s1"].state, "waiting")
        self.assertEqual(self.posted, [])  # no key went to the message box
        # Answered: the box is gone, so ✕ is a plain Enter again.
        d.handle_event({"op": "event", "source": "claude", "event": "PostToolUse", "session": "s1",
                        "data": {"tool_name": "AskUserQuestion"}})
        d.handle_control("cross")
        for _ in range(100):
            if self.posted:
                break
            time.sleep(0.01)
        self.assertEqual(self.posted, [[{"key": "enter"}]])
        self.assertEqual(len(calls), 2)

    def test_a_press_queued_behind_a_submit_is_dropped(self):
        calls = []

        def answer(app, qs, pick):  # like the real one: a submit takes a moment
            calls.append(pick)
            time.sleep(0.15)
            return "option 1 ticked (press)" if pick == 9 else "submitted"
        fake = (lambda: self.front, lambda prompt=False: True, lambda items, gap: self.posted.append(items))
        d = Daemon(C.load_config(), None, desktop=fake, resolver=make_resolver(), desktop_question=answer)
        qs = [{"multiSelect": False, "options": ["A", "B"]}, {"multiSelect": False, "options": ["C", "D"]}]
        d.handle_event({"op": "event", "source": "claude", "event": "PreToolUse", "session": "s1",
                        "data": {"tool_name": "AskUserQuestion", "tool_input": {"questions": qs}}})
        d.handle_control("cross")  # page 1
        d.handle_control("cross")  # an impatient second press: it must not submit page 2
        time.sleep(0.5)
        self.assertEqual(calls, [None])
        d.handle_control("cross")  # page 2 is on screen now: this one is for it
        time.sleep(0.3)
        self.assertEqual(calls, [None, None])
        # Ticking several options of a multiple-choice question in a row is not a submit.
        d.cfg["mapping"]["r1+cross"] = "@choose:9"
        d.handle_control("r1+cross")
        d.handle_control("r1+cross")
        time.sleep(0.5)
        self.assertEqual(calls, [None, None, 9, 9])

    def test_actions_the_app_lacks_send_nothing(self):
        self.d.handle_control("l1")
        self.d.handle_control("swipe_left")
        time.sleep(0.05)
        self.assertEqual(self.posted, [])
        self.assertEqual(self.d.flashes[-1].color, [255, 0, 0])

    def test_square_clears_the_message_box(self):
        self.d.handle_control("square")
        self.wait_posted(1)
        self.assertEqual(self.posted, [[{"key": "cmd+a"}, {"key": "backspace"}]])
        self.assertEqual(key_event("cmd+a"), (0, CMD))
        self.assertEqual(key_event("backspace"), (51, 0))

    def test_other_app_or_no_permission_gets_nothing(self):
        self.front = "com.google.Chrome"
        self.d.handle_control("cross")
        self.front, self.trusted = self.CLAUDE, False
        self.d.handle_control("cross")
        time.sleep(0.05)
        self.assertEqual(self.posted, [])
        self.assertEqual(self.d.status()["desktop"]["trusted"], False)

    def test_wrapper_wins_over_desktop(self):
        conn = FakeConn()
        self.d.attach(Target("w-a", "claude", "a", conn))
        self.d.handle_control("cross")
        time.sleep(0.05)
        self.assertEqual(self.posted, [])
        self.assertIn(b'"enter"', conn.sent[-1])

    def test_question_box_in_front_takes_its_buttons_from_the_terminal(self):
        calls = []
        fake = (lambda: self.front, lambda prompt=False: True, lambda items, gap: self.posted.append(items))
        d = Daemon(C.load_config(), None, desktop=fake, resolver=make_resolver(),
                   desktop_question=lambda app, qs, pick: calls.append(pick) or "ok")
        conn = FakeConn()
        d.attach(Target("w-a", "claude", "a", conn))
        qs = [{"multiSelect": False, "options": ["A", "B"]}]
        d.handle_event({"op": "event", "source": "claude", "event": "PreToolUse", "session": "s1",
                        "data": {"tool_name": "AskUserQuestion", "tool_input": {"questions": qs}}})
        d.handle_control("r1+circle")  # option 2 of the box
        d.handle_control("cross")      # its Submit
        d.handle_control("down")       # other buttons stay with the terminal
        for _ in range(100):
            if len(calls) >= 2:
                break
            time.sleep(0.01)
        self.assertEqual(calls, [2, None])
        self.assertEqual(len(conn.sent), 1)
        self.assertIn(b'"down"', conn.sent[0])
        # Another app in front: the terminal gets ✕.
        self.front = "com.apple.Terminal"
        d.handle_control("cross")
        self.assertIn(b'"enter"', conn.sent[-1])
        # The terminal session asking itself keeps its buttons.
        self.front = self.CLAUDE
        d.handle_event({"op": "event", "source": "claude", "event": "PermissionRequest", "session": "s2",
                        "wrapper": "w-a", "data": {"tool_name": "Bash"}})
        d.sessions["w-a"].since -= 1  # the prompt has been up for a moment
        n = len(conn.sent)
        d.handle_control("cross")
        self.assertEqual(len(conn.sent), n + 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.posted, [])

    def test_esc_resets_desktop_session(self):
        self.d.handle_event({"op": "event", "source": "claude", "event": "UserPromptSubmit", "session": "s9",
                             "cwd": "/tmp/p", "data": {}})
        self.assertEqual(self.d.sessions["claude:s9"].state, "thinking")
        self.d.handle_control("circle")
        self.assertEqual(self.d.sessions["claude:s9"].state, "idle")
        self.wait_posted(1)
        self.assertEqual(self.posted, [[{"key": "esc"}]])


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.mic = False
        self.posted = []
        fake = (lambda: "com.anthropic.claudefordesktop", lambda prompt=False: True,
                lambda items, gap: self.posted.append(items))
        self.d = Daemon(C.load_config(), None, desktop=fake, mic_probe=lambda: self.mic,
                        resolver=make_resolver())

    def ev(self, event, **data):
        self.d.handle_event({"op": "event", "source": "claude", "event": event, "session": "s1",
                             "cwd": "/tmp/p", "data": data})

    def test_tool_kind_colors_the_tool_state(self):
        self.ev("UserPromptSubmit")
        self.ev("PreToolUse", tool_name="Bash")
        self.assertEqual(self.d.flashes[-1].color, [255, 130, 0])
        self.d.flashes = []
        s = self.d.sessions["claude:s1"]
        o = self.d.frame(s.since)  # breathe starts at full brightness
        self.assertEqual(o.rgb, (255, 130, 0))
        self.ev("PreToolUse", tool_name="mcp__github__get_issue")
        self.assertEqual(self.d.flashes[-1].color, [0, 255, 150])
        self.ev("PreToolUse", tool_name="SomethingNew")
        self.assertEqual(self.d.flashes[-1].color, [255, 255, 255])

    def test_progress_leds_and_busy_trigger(self):
        o = self.d.frame(time.monotonic())
        self.assertEqual(o.player_leds, 0)
        self.assertEqual(o.trigger_left, bytes(11))
        self.ev("UserPromptSubmit")
        for _ in range(4):
            self.ev("PreToolUse", tool_name="Read")
            self.ev("PostToolUse", tool_name="Read")
        o = self.d.frame(time.monotonic())
        self.assertEqual(o.player_leds, PLAYER_PATTERNS[2])
        self.assertEqual(o.trigger_left[0], 0x21)  # feedback: pull L2 through to interrupt
        self.ev("UserPromptSubmit")
        self.assertEqual(self.d.frame(time.monotonic()).player_leds, 0)

    def test_long_turn_celebrates(self):
        self.ev("UserPromptSubmit")
        self.ev("Stop")
        self.assertFalse(any(f.kind == "rainbow" for f in self.d.flashes))
        self.ev("UserPromptSubmit")
        self.d.sessions["claude:s1"].turn_start -= 120
        self.ev("Stop")
        self.assertTrue(any(f.kind == "rainbow" for f in self.d.flashes))
        self.assertEqual(self.d.sessions["claude:s1"].state, "done")

    def test_listening(self):
        now = time.monotonic()
        self.mic = True
        self.d.poll_mic(now)
        self.assertTrue(self.d.listening)
        o = self.d.frame(now)
        self.assertEqual(o.mic_led, 1)
        self.assertEqual(o.rgb, (255, 255, 255))
        self.mic = False
        self.d.poll_mic(now + 0.1)  # rate limited
        self.assertTrue(self.d.listening)
        self.d.poll_mic(now + 1)
        self.assertFalse(self.d.listening)
        self.assertEqual(self.d.frame(now + 1).mic_led, 0)

    def test_mic_button_starts_claude_dictation(self):
        self.d.handle_control("mic")
        for _ in range(100):
            if self.posted:
                break
            time.sleep(0.01)
        self.assertEqual(self.posted, [[{"key": "cmd+d"}]])
        self.d.handle_control("mic_up")  # held past ptt_ms: toggle dictation off again
        for _ in range(100):
            if len(self.posted) == 2:
                break
            time.sleep(0.01)
        self.assertEqual(self.posted, [[{"key": "cmd+d"}], [{"key": "cmd+d"}]])
        self.d.handle_control("ps+mic")
        self.assertTrue(self.d.locked)
        self.d.handle_control("mic")  # locked: nothing posted
        time.sleep(0.05)
        self.assertEqual(len(self.posted), 2)
        self.d.handle_control("ps+mic")
        self.assertFalse(self.d.locked)

    def test_low_battery_warns_once_per_interval(self):
        self.d.battery, self.d.charging = 10, "discharging"
        now = time.monotonic()
        self.d.frame(now)
        self.d.frame(now + 1)
        self.assertEqual(sum(1 for f in self.d.flashes if f.color == [255, 0, 0]), 1)
        first = self.d.last_low_battery
        self.d.charging = "charging"
        self.d.frame(now + 400)
        self.assertEqual(self.d.last_low_battery, first)


class BindingsTest(unittest.TestCase):
    def test_claude_defaults(self):
        r = make_resolver()
        self.assertEqual(r.resolve("submit", "claude"), ["enter"])
        self.assertEqual(r.resolve("cancel", "claude"), ["esc"])
        self.assertEqual(r.resolve("clear_input", "claude"), ["kill_input"])  # Ctrl+L does not clear
        self.assertEqual(r.resolve("cycle_mode", "claude"), ["shift+tab"])
        self.assertEqual(r.resolve("transcript", "claude"), ["ctrl+o"])
        self.assertEqual(r.resolve("interrupt", "claude"), ["ctrl+c"])
        self.assertEqual(r.resolve("rewind", "claude"), ["esc", "esc"])
        self.assertIsNone(r.resolve("dictation", "claude"))
        self.assertEqual(r.resolve("transcript", "codex"), ["ctrl+t"])
        self.assertIsNone(r.resolve("clear_input", "codex"))

    def test_user_rebinds(self):
        r = make_resolver({"bindings": [
            {"context": "Chat", "bindings": {"escape": None, "ctrl+q": "chat:cancel",
                                             "enter": "chat:newline", "ctrl+s": "chat:submit",
                                             "ctrl+l": None, "ctrl+x ctrl+l": "chat:clearInput",
                                             "ctrl+o": "chat:stash"}},
            {"context": "Global", "bindings": {"ctrl+y": "app:toggleTranscript"}},
        ]})
        self.assertEqual(r.resolve("cancel", "claude"), ["ctrl+q"])
        self.assertEqual(r.resolve("submit", "claude"), ["ctrl+s"])
        self.assertEqual(r.resolve("clear_input", "claude"), ["ctrl+x", "ctrl+l"])
        self.assertEqual(r.resolve("transcript", "claude"), ["ctrl+y"])  # ctrl+o shadowed in Chat
        # Permission prompts use the Confirmation context, which the user did not touch.
        self.assertEqual(r.resolve("submit", "claude", permission=True), ["enter"])
        self.assertEqual(r.resolve("cancel", "claude", permission=True), ["esc"])

    def test_unsendable_binding_and_reload(self):
        r = make_resolver({"bindings": [{"context": "Chat", "bindings": {"Escape": None, "cmd+e": "chat:cancel"}}]})
        self.assertIsNone(r.resolve("cancel", "claude"))  # terminals cannot send cmd
        p = r.tmp / "keybindings.json"
        p.write_text(json.dumps({"bindings": [{"context": "Chat", "bindings": {"Escape": None, "Ctrl+K": "chat:cancel"}}]}))
        os.utime(p, ns=(1, 1))  # make sure the mtime changes
        self.assertEqual(r.resolve("cancel", "claude"), ["ctrl+k"])
        p.write_text("{broken")
        os.utime(p, ns=(2, 2))
        self.assertEqual(r.resolve("cancel", "claude"), ["esc"])  # broken file -> defaults
        self.assertIn("keybindings.json", r.describe()["claude_keybindings_error"])

    def test_desktop_dictation(self):
        self.assertEqual(make_resolver().resolve("dictation", "desktop"), ["cmd+d"])
        off = make_resolver(desktop={"preferences": {"dictationShortcut": "off"}})
        self.assertIsNone(off.resolve("dictation", "desktop"))
        custom = make_resolver(desktop={"preferences": {"dictationShortcut": {"accelerator": "CommandOrControl+Shift+D"}}})
        self.assertEqual(custom.resolve("dictation", "desktop"), ["cmd+shift+d"])
        self.assertEqual(custom.describe()["desktop_dictation"], "cmd+shift+d")
        self.assertEqual(make_resolver().resolve("clear_input", "desktop"), ["cmd+a", "backspace"])
        self.assertIsNone(make_resolver().resolve("transcript", "desktop"))

    def test_parsers(self):
        self.assertEqual(normalize_keystroke("Ctrl+Shift+Tab"), "ctrl+shift+tab")
        self.assertEqual(normalize_keystroke("meta+p"), "alt+p")
        self.assertEqual(normalize_keystroke("escape"), "esc")
        self.assertIsNone(normalize_keystroke("ctl+x"))
        self.assertEqual(parse_accelerator("Cmd+Alt+Space"), "cmd+alt+space")
        self.assertEqual(parse_accelerator("Control+Shift+K"), "ctrl+shift+k")
        self.assertIsNone(parse_accelerator("Cmd+"))

    def test_same_layout_everywhere(self):
        cfg = C.load_config()
        for prof in cfg["profiles"].values():
            self.assertEqual(prof, {})

    def test_daemon_uses_user_bindings(self):
        r = make_resolver({"bindings": [{"context": "Chat", "bindings": {"escape": None, "ctrl+q": "chat:cancel"}}]},
                          desktop={"preferences": {"dictationShortcut": {"accelerator": "Ctrl+Shift+D"}}})
        posted = []
        d = Daemon(C.load_config(), None, desktop=(lambda: "com.anthropic.claudefordesktop", lambda prompt=False: True,
                                                    lambda items, gap: posted.append(items)),
                   mic_probe=lambda: False, resolver=r)
        conn = FakeConn()
        d.attach(Target("w-a", "claude", "a", conn))
        d.handle_control("circle")
        self.assertIn(b'"ctrl+q"', conn.sent[-1])
        d.detach("w-a")
        d.handle_control("mic")
        d.handle_control("l1")  # no mode cycling in the desktop app: red flash, nothing sent
        for _ in range(100):
            if posted:
                break
            time.sleep(0.01)
        time.sleep(0.05)
        self.assertEqual(posted, [[{"key": "ctrl+shift+d"}]])
        self.assertEqual(d.flashes[-1].color, [255, 0, 0])


class InteractionTest(unittest.TestCase):
    """Next-step card replies, retry, prompt wheel, PS settings layer."""

    def setUp(self):
        self.posted = []
        fake = (lambda: "com.anthropic.claudefordesktop", lambda prompt=False: True,
                lambda items, gap: self.posted.append(items))
        self.tmp = Path(tempfile.mkdtemp(prefix="padpilot-set-"))
        self.d = Daemon(C.load_config(), None, desktop=fake, mic_probe=lambda: False,
                        resolver=make_resolver(), settings_path=self.tmp / "config.json")

    def ev(self, event, session="s1", **data):
        self.d.handle_event({"op": "event", "source": "claude", "event": event, "session": session,
                             "cwd": "/tmp/p", "wrapper": data.pop("wrapper", None), "data": data})

    def wait(self, n):
        for _ in range(200):
            if len(self.posted) >= n:
                return
            time.sleep(0.01)

    def card(self, *actions, session="s1"):
        self.ev("UserPromptSubmit", session=session, prompt="hi")
        self.ev("PreToolUse", session=session, tool_name="mcp__ccd_turn__wrap_up",
                tool_input={"actions": list(actions)})
        self.ev("Stop", session=session)

    def test_spoken_cues(self):
        said = []
        d = Daemon(C.load_config(), None, desktop=self.d.desktop, mic_probe=lambda: False,
                   resolver=make_resolver(), settings_path=self.tmp / "config.json",
                   speak=lambda sound, **kw: said.append(sound))
        ev = lambda e, **kw: d.handle_event({"op": "event", "source": "claude", "event": e,
                                             "session": "s1", "cwd": "/tmp/p", "wrapper": None,
                                             "data": kw})
        ev("UserPromptSubmit", prompt="hi")
        ev("PermissionRequest", tool_name="Bash")
        ev("PostToolUse", tool_name="Bash")
        ev("Stop")
        self.assertEqual(said, ["Ping", "Glass"])
        d.handle_control("ps+triangle")  # PS + △: off, saved
        self.assertFalse(d.cfg["sounds"]["enabled"])
        self.assertEqual(json.loads((self.tmp / "config.json").read_text())["sounds"], {"enabled": False})
        ev("UserPromptSubmit", prompt="again")
        ev("Stop")
        self.assertEqual(said, ["Ping", "Glass"])
        d.handle_control("ps+triangle")  # on again, and says so
        self.assertEqual(said[-1], "Pop")
        self.assertIn("cues", d.cfg["sounds"])  # the rest of the defaults is kept

    def test_practice_does_not_change_settings(self):
        # The tutorial asks for PS + △ ("sounds off"); that must not switch the real sounds off.
        d = Daemon(C.load_config(), None, desktop=self.d.desktop, mic_probe=lambda: False,
                   resolver=make_resolver(), settings_path=self.tmp / "config.json",
                   speak=lambda sound, **kw: None)
        d.practice = Target("watch", "", "", FakeConn())
        d.watchers.append(d.practice)
        before = {k: d.cfg.get(k) for k in ("sounds", "brightness", "rumble_scale", "idle_light")}
        for combo in ("ps+triangle", "ps+up", "ps+down", "ps+left", "ps+right", "ps+square"):
            d.handle_control(combo)
        self.assertEqual({k: d.cfg.get(k) for k in before}, before)
        self.assertFalse((self.tmp / "config.json").exists())
        self.assertEqual(d.flashes[-1].color, d.cfg["flashes"]["setting"]["color"])  # still shows the press

    def test_speaker_on_only_while_a_cue_plays(self):
        # Closing the audio stream makes a noise; the speaker is off by then.
        d = Daemon(C.load_config(), None, desktop=self.d.desktop, mic_probe=lambda: False,
                   resolver=make_resolver(), settings_path=self.tmp / "config.json",
                   speak=lambda sound, **kw: None)
        now = time.monotonic()
        self.assertEqual(d.frame(now).speaker_volume, 0)
        d._cue("done")
        self.assertEqual(d.frame(time.monotonic()).speaker_volume, 100)
        self.assertEqual(d.frame(time.monotonic() + 3).speaker_volume, 0)
        d.cfg["sounds"] = dict(d.cfg["sounds"], enabled=False)
        self.assertIsNone(d.frame(time.monotonic()).speaker_volume)

    def test_card_kept_when_the_reply_could_not_be_sent(self):
        self.card({"label": "Yes"})
        front = ["other.app"]
        self.d.desktop = (lambda: front[0], lambda prompt=False: True,
                          lambda items, gap: self.posted.append(items))
        self.d.handle_control("r1+cross")  # app not in front: denied, card kept
        front[0] = "com.anthropic.claudefordesktop"
        self.d.handle_control("r1+cross")
        self.wait(1)
        self.assertEqual(self.posted[0][2], {"text": "Yes"})

    def test_typing_stops_when_another_app_comes_forward(self):
        fronts = iter(["com.anthropic.claudefordesktop"] * 2 + ["other.app"] * 10)
        self.d.desktop = (lambda: next(fronts), lambda prompt=False: True,
                          lambda items, gap: self.posted.append(items))
        self.d.cfg["sequence_gap_ms"] = 1
        self.d._run("test", ["text:a"] * 20, None)
        time.sleep(0.2)
        self.assertEqual(sum(len(p) for p in self.posted), 8)  # the first batch only

    def test_reply_buttons(self):
        self.card({"label": "先做第一批"}, {"label": "全部做"})
        self.assertEqual(self.d.frame(time.monotonic()).player_leds, PLAYER_PATTERNS[2])
        self.d.handle_control("r1+circle")
        self.wait(1)
        self.assertEqual(self.posted, [[{"key": "cmd+a"}, {"key": "backspace"}, {"text": "全部做"},
                                        {"key": "enter"}]])
        self.d.handle_control("r1+cross")  # used up
        time.sleep(0.05)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.d.flashes[-1].color, [255, 0, 0])

    def test_app_action_buttons_are_skipped(self):
        self.card({"action": "commit", "label": "Commit"}, {"label": "Not now"})
        self.d.handle_control("r1+cross")
        self.d.handle_control("r1+square")
        time.sleep(0.05)
        self.assertEqual(self.posted, [])
        self.d.handle_control("r1+circle")
        self.wait(1)
        self.assertEqual(self.posted[0][2], {"text": "Not now"})

    def test_card_cleared_by_next_prompt(self):
        self.card({"label": "Yes"})
        self.ev("UserPromptSubmit", prompt="something else")
        self.d.handle_control("r1+cross")
        time.sleep(0.05)
        self.assertEqual(self.posted, [])

    def test_retry(self):
        self.d.handle_control("r3")  # nothing to retry yet
        self.ev("UserPromptSubmit", prompt="修一下\n然后跑测试")
        self.d.handle_control("r3")  # still working: refused...
        time.sleep(0.05)
        self.assertEqual(self.posted, [])
        self.d.handle_control("circle")  # stop it (Esc) -> idle
        self.wait(1)
        self.d.handle_control("r3")
        self.wait(2)
        # Desktop: filled in, not sent (the app does not say which session is on screen).
        self.assertEqual(self.posted[1], [{"key": "cmd+a"}, {"key": "backspace"}, {"text": "修一下"},
                                          {"key": "shift+enter"}, {"text": "然后跑测试"}])

    def test_retry_twice_forces_when_state_looks_busy(self):
        self.ev("UserPromptSubmit", prompt="again")
        for s in self.d.sessions.values():
            s.updated -= 30  # no hook event for a while: the state may be stale
        self.d.handle_control("r3")
        self.d.handle_control("r3")
        self.wait(1)
        self.assertEqual(self.posted[0][-1], {"text": "again"})

    def test_retry_never_forced_while_the_session_is_still_active(self):
        # Prompt from a session that is still working (maybe not the one on screen): pressing
        # twice must not type it into whatever window is in front.
        self.ev("UserPromptSubmit", prompt="from another window")
        self.d.handle_control("r3")
        self.d.handle_control("r3")
        self.d.handle_control("r3")
        time.sleep(0.05)
        self.assertEqual(self.posted, [])

    def test_retry_in_terminal(self):
        conn = FakeConn()
        self.d.attach(Target("w-a", "claude", "a", conn))
        self.ev("UserPromptSubmit", wrapper="w-a", prompt="line1\nline2")
        self.ev("Stop", wrapper="w-a")
        self.d.handle_control("r3")
        msg = json.loads(conn.sent[-1])
        self.assertEqual(msg["items"], [{"key": "kill_input"}, {"text": "line1\nline2"}, {"key": "enter"}])

    def test_wheel_types_a_preset_without_sending(self):
        self.d.handle_control("wheel:1")
        self.d.handle_control("wheel_pick:1")
        self.wait(1)
        self.assertEqual(self.posted, [[{"key": "cmd+a"}, {"key": "backspace"},
                                        {"text": C.DEFAULTS["prompt_wheel"][1]}]])

    def test_ps_layer_settings_are_saved(self):
        (self.tmp / "config.json").write_text(json.dumps({"hold_ms": 700}))
        self.d.handle_control("ps+up")  # already at full brightness
        self.assertEqual(self.d.flashes[-1].color, [255, 0, 0])
        self.d.handle_control("ps+down")
        self.d.handle_control("ps+left")
        self.d.handle_control("ps+square")
        saved = json.loads((self.tmp / "config.json").read_text())
        self.assertEqual(saved, {"hold_ms": 700, "brightness": 0.8, "rumble_scale": 0.6, "idle_light": False})
        self.assertEqual(self.d.cfg["brightness"], 0.8)
        self.d.flashes = []
        self.assertEqual(self.d.frame(time.monotonic()).rgb, (0, 0, 0))  # idle light off
        self.assertEqual(self.posted, [])

    def test_new_actions_per_destination(self):
        r = make_resolver()
        self.assertEqual(r.resolve("review", "desktop"), ["cmd+shift+d"])
        self.assertEqual(r.resolve("new_session", "desktop"), ["cmd+n"])
        self.assertEqual(r.resolve("menu", "desktop"), ["cmd+k"])
        self.assertEqual(r.resolve("review", "claude"), ["ctrl+o"])
        self.assertEqual(r.resolve("new_session", "claude"), ["text:/clear", "enter"])
        self.assertIsNone(r.resolve("menu", "claude"))
        self.assertEqual(r.resolve("rewind", "desktop"), ["esc", "esc"])
        # L1 layer
        self.assertEqual(r.resolve("queue_submit", "claude"), ["ctrl+x", "enter"])
        self.assertEqual(r.resolve("kill_agents", "claude"), ["ctrl+x", "ctrl+k"])
        self.assertEqual(r.resolve("undo", "claude"), ["ctrl+_"])
        self.assertEqual(r.resolve("model_picker", "claude"), ["alt+p"])
        self.assertEqual(r.resolve("thinking", "claude"), ["alt+t"])
        self.assertEqual(r.resolve("queue_submit", "desktop"), ["cmd+enter"])
        self.assertEqual(r.resolve("undo", "desktop"), ["cmd+z"])
        self.assertEqual(r.resolve("model_picker", "desktop"), ["cmd+shift+i"])
        self.assertEqual(r.resolve("thinking", "desktop"), ["cmd+shift+e"])
        self.assertIsNone(r.resolve("kill_agents", "desktop"))
        self.assertEqual(r.resolve("next_session", "desktop"), ["cmd+shift+]"])
        self.assertEqual(key_event("cmd+shift+["), (33, CMD | SHIFT))
        self.assertEqual(r.resolve("review", "codex"), ["ctrl+t"])
        self.assertEqual(r.resolve("new_session", "codex"), ["text:/new", "enter"])

    def test_codex_retry_and_new_session_skip_the_missing_clear(self):
        conn = FakeConn()
        self.d.attach(Target("w-c", "codex", "c", conn))
        self.ev("UserPromptSubmit", wrapper="w-c", prompt="again")
        self.ev("Stop", wrapper="w-c")
        self.d.handle_control("r3")
        self.assertEqual(json.loads(conn.sent[-1])["items"], [{"text": "again"}, {"key": "enter"}])
        self.d.handle_control("create_hold")
        self.assertEqual(json.loads(conn.sent[-1])["items"], [{"text": "/new"}, {"key": "enter"}])

    def test_ptt_release_ignored_when_press_failed(self):
        conn = FakeConn()
        self.d.attach(Target("w-a", "claude", "a", conn))
        self.d.handle_control("mic")     # no dictation in the terminal: denied
        self.d.handle_control("mic_up")  # so nothing to toggle back
        self.assertEqual(conn.sent, [])

    def test_practice_mode_reports_instead_of_sending(self):
        w = FakeConn()
        self.d.practice = Target("watch", "", "", w)
        self.d.watchers.append(self.d.practice)
        self.d.handle_control("cross")
        self.d.handle_control("l1")
        time.sleep(0.05)
        self.assertEqual(self.posted, [])
        msgs = [json.loads(x) for x in w.sent]
        self.assertIn({"op": "would_send", "name": "cross", "dest": "desktop", "items": [{"key": "enter"}]}, msgs)
        self.assertEqual(msgs[-1]["op"], "denied")

    def test_right_stick_switches_sessions(self):
        self.d.handle_control("rstick_right")
        self.d.handle_control("rstick_left")
        self.wait(2)
        self.assertEqual(self.posted, [[{"key": "cmd+shift+]"}], [{"key": "cmd+shift+["}]])
        a, b = FakeConn(), FakeConn()
        self.d.attach(Target("w-a", "claude", "a", a))
        self.d.handle_control("rstick_right")  # one terminal session: nothing to switch to
        self.assertEqual(self.d.flashes[-1].color, [255, 0, 0])
        self.d.attach(Target("w-b", "claude", "b", b))
        self.d.handle_control("rstick_right")
        self.assertEqual(self.d.focus, "w-a")
        self.d.handle_control("rstick_left")
        self.assertEqual(self.d.focus, "w-b")

    def write_transcript(self, entries, name="sess-1"):
        d = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-tmp-proj"
        d.mkdir(parents=True, exist_ok=True)
        p = d / (name + ".jsonl")
        p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return p

    def test_card_and_retry_are_not_read_from_transcripts(self):
        # A transcript with a card and a prompt, but no hook event since the daemon started:
        # padpilot does not read messages from transcripts, so there is nothing to send.
        p = self.write_transcript([
            {"type": "user", "message": {"role": "user", "content": "做第一批"}},
            {"type": "last-prompt", "lastPrompt": "做第一批"},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__ccd_turn__wrap_up",
                 "input": {"actions": [{"label": "做第二批"}, {"label": "先调手感"}]}}]}},
        ])
        try:
            self.d.handle_control("r1+circle")
            self.d.handle_control("r3")
            time.sleep(0.1)
            self.assertEqual(self.posted, [])
            self.assertEqual(self.d.flashes[-1].color, [255, 0, 0])
            self.assertEqual(self.d.sessions, {})
        finally:
            p.unlink()

    def test_context_usage(self):
        p = self.write_transcript([
            {"type": "assistant", "message": {"model": "claude-sonnet-x", "usage": {
                "input_tokens": 10, "cache_read_input_tokens": 170000, "output_tokens": 990}}}], name="ctx")
        try:
            self.ev("UserPromptSubmit", prompt="x", transcript_path=str(p))
            self.ev("Stop")
            s = self.d.sessions["claude:s1"]
            self.assertAlmostEqual(s.context, 0.855)
            o = self.d.frame(s.since + 1.5)  # fade: half way to amber
            self.assertNotEqual(o.rgb, (0, 255, 60))
            self.d.battery = 80
            self.d.handle_control("ps")
            self.assertEqual(self.d.frame(time.monotonic()).player_leds, 0b11111)  # 86 % -> 5 of 5
        finally:
            p.unlink()


class HookTest(unittest.TestCase):
    def run_hook(self, payload):
        from padpilot import hook
        sent = []
        orig = (hook.client.request, hook.client.start_daemon)
        hook.client.request = lambda msg, timeout=0: sent.append(msg)
        hook.client.start_daemon = lambda: None
        try:
            hook._dispatch("claude", payload)
        finally:
            hook.client.request, hook.client.start_daemon = orig
        return sent[0]

    def test_forwards_prompt_and_card_buttons_only(self):
        m = self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "x" * 30000})
        self.assertEqual(len(m["data"]["prompt"]), 20000)
        m = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "mcp__ccd_turn__wrap_up",
                           "tool_input": {"recap": "r", "ask": "?", "actions": [{"label": "Yes"},
                                                                                 {"action": "commit"}]}})
        self.assertEqual(m["data"]["tool_input"], {"actions": [{"label": "Yes", "action": None},
                                                               {"label": "", "action": "commit"}]})
        m = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                           "tool_input": {"questions": [{"question": "secret?", "header": "h", "multiSelect": True,
                                                         "options": [{"label": "A", "description": "d"}]}]}})
        self.assertEqual(m["data"]["tool_input"], {"questions": [{"multiSelect": True, "options": ["A"]}]})
        # Its permission request is passed on as a plain event, not held as an approval.
        m = self.run_hook({"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion", "session_id": "s"})
        self.assertEqual(m["op"], "event")
        m = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
                           "tool_input": {"content": "big"}})
        self.assertNotIn("tool_input", m["data"])


class IOHelpersTest(unittest.TestCase):
    def test_typed_text_chunks_count_utf16_units(self):
        from padpilot.macos import utf16_chunks
        s = "\U0001F600" * 20 + "ab"
        chunks = utf16_chunks(s)
        self.assertEqual("".join(chunks), s)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 16 for c in chunks))
        self.assertEqual(utf16_chunks("x" * 40), ["x" * 16, "x" * 16, "x" * 8])

    def test_transcript_tail_sees_past_one_long_line(self):
        from padpilot import transcript as T
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps({"type": "assistant", "message": {"model": "claude-x", "usage": {
                    "input_tokens": 5, "cache_read_input_tokens": 1000, "output_tokens": 20}}}) + "\n")
                f.write(json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "content": "z" * (T.TAIL_BYTES + 10)}]}}) + "\n")
            self.assertEqual(T.context_tokens(p), (1025, "claude-x"))

    def test_transcript_odd_message_shapes_are_skipped(self):
        from padpilot import transcript as T
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as f:
                for m in [{"type": "user", "message": {"content": "real"}},
                          {"type": "user", "message": "a string"},
                          {"type": "assistant", "message": ["x"]}]:
                    f.write(json.dumps(m) + "\n")
            self.assertEqual(T.context_tokens(p), (None, None))
            self.assertEqual(T.context_tokens(None), (None, None))
            self.assertFalse(hasattr(T, "last_prompt"))  # no message text is read

    def test_terminal_modes_split_across_reads(self):
        from padpilot.wrapper import Wrapper
        w = Wrapper(["claude"])
        w._track_modes(b"hello\x1b[?")
        w._track_modes(b"1h world")
        self.assertTrue(w.app_cursor)
        w._track_modes(b"\x1b[>")
        w._track_modes(b"1u")
        self.assertEqual(w.kitty, [1])
        w._track_modes(b"more output")  # the push in the carried tail is not counted twice
        self.assertEqual(w.kitty, [1])
        w._track_modes(b"\x1b[<u\x1b[?1l")
        self.assertEqual(w.kitty, [])
        self.assertFalse(w.app_cursor)


if __name__ == "__main__":
    unittest.main()


class TutorialPageTest(unittest.TestCase):
    """The page the daemon serves: practice mode while it is open, own origin only."""

    def setUp(self):
        import http.client
        from padpilot import web
        self.http = http.client
        self.d = Daemon(C.load_config(), None, desktop=NO_DESKTOP, mic_probe=lambda: False,
                        resolver=make_resolver())
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.srv = web.start(self.d, self.port)
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.d.running = False
        self.srv.shutdown()
        self.srv.server_close()

    def _req(self, method, path, body=None, headers=None):
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=3)
        c.request(method, path, body=body, headers=headers or {})
        try:
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_page_is_served_in_standards_mode(self):
        code, body = self._req("GET", "/")
        self.assertEqual(code, 200)
        self.assertTrue(body.lower().startswith(b"<!doctype html>"))

    def test_pages_load_nothing_from_the_internet(self):
        import re
        for path in ("/", "/manual"):
            code, body = self._req("GET", path)
            html = body.decode("utf-8")
            # Links people click may point elsewhere; nothing the page loads by itself may.
            self.assertEqual(re.findall(r"<link[^>]*https?://|src=.https?://|url\(.?https?://|@import", html), [])
            fonts = re.findall(r'url\("(fonts/[^"]+)"\)', html)
            self.assertEqual(len(fonts), 3)
            for font in fonts:
                code, data = self._req("GET", "/" + font)
                self.assertEqual(code, 200, font)
                self.assertTrue(data.startswith(b"wOF2"), font)
        self.assertEqual(self._req("GET", "/fonts/../daemon.py")[0], 404)
        self.assertEqual(self._req("GET", "/fonts/missing.woff2")[0], 404)

    def test_manual_is_served_and_is_not_practice_mode(self):
        for path in ("/manual", "/manual.html"):
            code, body = self._req("GET", path)
            self.assertEqual(code, 200)
            self.assertTrue(body.lower().startswith(b"<!doctype html>"))
            self.assertIn(b"padpilot Manual", body)
        self.assertIsNone(self.d.practice)  # only the tutorial's event stream turns practice on
        self.assertEqual(self._req("GET", "/manual", headers={"Host": "evil.example"})[0], 403)

    def test_other_hosts_and_origins_are_refused(self):
        self.assertEqual(self._req("GET", "/", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self._req("GET", "/events", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self._req("POST", "/rumble", b'{"pattern":"tick"}')[0], 403)  # no Origin
        self.assertEqual(self._req("POST", "/rumble", b'{"pattern":"tick"}',
                                   {"Origin": self.origin})[0], 200)

    def test_open_page_is_practice_mode_and_gets_presses(self):
        c = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        c.sendall(f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nOrigin: {self.origin}\r\n\r\n".encode())
        buf = b""
        while b'"hello"' not in buf:
            buf += c.recv(4096)
        self.assertIsNotNone(self.d.practice)
        self.assertTrue(self.d.web_light("thinking"))
        with self.d.lock:
            self.d._notify({"op": "raw", "name": "ps", "down": True, "held_s": 0})
        while b'"ps"' not in buf:
            buf += c.recv(4096)
        sent = []
        self.d._desktop_send = lambda items: sent.append(items) or True
        self.d.handle_control("cross")
        self.assertEqual(sent, [])  # practice: nothing reaches the app
        c.close()
        deadline = time.monotonic() + 5
        while self.d.practice is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNone(self.d.practice)  # closing the page ends practice
        self.assertIsNone(self.d.preview)
        self.assertEqual(self.d.watchers, [])

    def test_exit_practice_button_ends_practice_with_the_page_open(self):
        c = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        c.sendall(f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nOrigin: {self.origin}\r\n\r\n".encode())
        buf = b""
        while b'"hello"' not in buf:
            buf += c.recv(4096)
        hello = json.loads(buf.split(b"data: ", 1)[1].split(b"\n", 1)[0])
        self.assertIsNotNone(self.d.practice)
        post = lambda body, origin=self.origin: self._req("POST", "/leave", json.dumps(body).encode(),
                                                          {"Origin": origin} if origin else {})
        self.assertEqual(post({"id": hello["id"]}, origin=None)[0], 403)  # no Origin: refused
        self.assertEqual(post({"id": "not-a-page"})[0], 400)              # unknown page: nothing ends
        self.assertIsNotNone(self.d.practice)
        self.assertEqual(post({"id": hello["id"]})[0], 200)
        self.assertIsNone(self.d.practice)  # at once, while the page is still connected
        sent = []
        self.d._desktop_send = lambda items: sent.append(items) or True
        self.d.handle_control("cross")
        self.assertEqual(sent, [[{"key": "enter"}]])  # presses reach the app again
        c.close()


class RenameMigrationTest(unittest.TestCase):
    """0.4 renamed dualsense-agent to padpilot: the old home moves once, old Codex hooks are still ours."""

    def test_old_home_moves_to_the_new_one(self):
        from padpilot import config
        tmp = Path(tempfile.mkdtemp())
        old, new = tmp / ".dualsense-agent", tmp / ".padpilot"
        old.mkdir()
        (old / "config.json").write_text('{"brightness": 0.5}')
        saved = (config.HOME, config.OLD_HOME, os.environ.pop("PADPILOT_HOME", None))
        try:
            config.HOME, config.OLD_HOME = new, old
            config._migrate_old_home()
            self.assertFalse(old.exists())
            self.assertEqual((new / "config.json").read_text(), '{"brightness": 0.5}')
            old.mkdir()                       # a second run leaves both alone
            config._migrate_old_home()
            self.assertTrue(old.exists())
        finally:
            config.HOME, config.OLD_HOME = saved[0], saved[1]
            if saved[2] is not None:
                os.environ["PADPILOT_HOME"] = saved[2]

    def test_codex_hooks_from_before_the_rename_are_recognised(self):
        from padpilot.cli import _is_ours
        self.assertTrue(_is_ours({"hooks": [{"command": '"/x/dualsense-agent/bin/dsagent" hook codex'}]}))
        self.assertTrue(_is_ours({"hooks": [{"command": '"/x/padpilot/bin/padpilot" hook codex'}]}))
        self.assertFalse(_is_ours({"hooks": [{"command": "other-tool hook codex"}]}))
