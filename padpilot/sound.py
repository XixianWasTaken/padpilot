"""Sound cues on the controller's built-in speaker.

Over USB macOS sees the DualSense as a 4-channel sound card. With the audio path set to the
speaker (see `OutputState.speaker_volume`) one channel of it plays on the built-in speaker.
`afplay` cannot pick a device, so this plays a sound file with an AudioQueue on that device only,
on the channel given. When the device is missing (Bluetooth, unplugged) nothing is played: it
never falls back to the Mac's own speakers.

Closing the audio stream makes the controller give a short noise (it sounds like "F"), so the
daemon switches the speaker on only while a cue plays and the stream stays open (silent) a while
longer, to close after the speaker is off again.

Playing runs in a child process (`python -m padpilot.sound play ...`), so a CoreAudio problem
cannot take the daemon down.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
import time
import wave
from ctypes import POINTER, Structure, byref, c_char_p, c_double, c_int32, c_uint32, c_void_p
from typing import List, Optional, Tuple

DEVICE = "DualSense Wireless Controller"
RATE = 48000
SYSTEM_SOUNDS = "/System/Library/Sounds"


# ---------------------------------------------------------------- the daemon side
class Speaker:
    """Plays cues in a child process; a new cue cuts off the one still playing."""

    def __init__(self, python: Optional[str] = None) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.python = python or sys.executable

    def play(self, sound: str, device: str = DEVICE, channel: int = 1, channels: int = 4,
             lead_ms: int = 0, gain: float = 1.0, tail_ms: int = 0) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
        args = [self.python, "-m", "padpilot.sound", "play", sound, "--device", device,
                "--channel", str(channel), "--channels", str(channels), "--lead", str(lead_ms),
                "--gain", str(gain), "--tail", str(tail_ms)]
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=env)
        except OSError:
            self.proc = None


# ---------------------------------------------------------------- files
def resolve(sound: str) -> Optional[str]:
    """A path, or the name of a macOS system sound ("Glass", "Ping", ...)."""
    if os.path.isfile(sound):
        return sound
    p = os.path.join(SYSTEM_SOUNDS, sound + ".aiff")
    return p if os.path.isfile(p) else None


def load(path: str, cache_dir: str) -> List[int]:
    """Mono 16-bit samples at 48 kHz (converted once with afconvert and cached)."""
    os.makedirs(cache_dir, exist_ok=True)
    st = os.stat(path)
    key = hashlib.sha1(f"{path}:{st.st_size}:{st.st_mtime}".encode()).hexdigest()[:16]
    wav = os.path.join(cache_dir, key + ".wav")
    if not os.path.exists(wav):
        tmp = wav + ".tmp.wav"
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", path, tmp],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(tmp, wav)
    with wave.open(wav, "rb") as w:
        data = w.readframes(w.getnframes())
    return list(memoryview(data).cast("h"))


def interleave(mono: List[int], channel: int, channels: int, lead_ms: int, gain: float,
               tail_ms: int = 0) -> bytes:
    """Put `mono` on one channel of a `channels`-channel 16-bit buffer, between `lead_ms` and
    `tail_ms` of silence."""
    lead = int(RATE * lead_ms / 1000)
    frames = lead + len(mono) + int(RATE * tail_ms / 1000)
    out = (ctypes.c_int16 * (frames * channels))()
    for i, v in enumerate(mono):
        v = int(v * gain)
        out[(lead + i) * channels + channel] = max(-32768, min(32767, v))
    return bytes(out)


# ---------------------------------------------------------------- CoreAudio
class _Addr(Structure):
    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]


class _ASBD(Structure):
    _fields_ = [("mSampleRate", c_double), ("mFormatID", c_uint32), ("mFormatFlags", c_uint32),
                ("mBytesPerPacket", c_uint32), ("mFramesPerPacket", c_uint32),
                ("mBytesPerFrame", c_uint32), ("mChannelsPerFrame", c_uint32),
                ("mBitsPerChannel", c_uint32), ("mReserved", c_uint32)]


class _AQBuffer(Structure):
    _fields_ = [("mAudioDataBytesCapacity", c_uint32), ("mAudioData", c_void_p),
                ("mAudioDataByteSize", c_uint32), ("mUserData", c_void_p),
                ("mPacketDescriptionCapacity", c_uint32), ("mPacketDescriptions", c_void_p),
                ("mPacketDescriptionCount", c_uint32)]


def _cc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


_CALLBACK = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, POINTER(_AQBuffer))


def _libs():
    ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    at = ctypes.CDLL("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    p = POINTER(_Addr)
    ca.AudioObjectGetPropertyDataSize.argtypes = [c_uint32, p, c_uint32, c_void_p, POINTER(c_uint32)]
    ca.AudioObjectGetPropertyData.argtypes = [c_uint32, p, c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
    cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, ctypes.c_long, c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    at.AudioQueueNewOutput.argtypes = [POINTER(_ASBD), _CALLBACK, c_void_p, c_void_p, c_void_p,
                                       c_uint32, POINTER(c_void_p)]
    at.AudioQueueNewOutput.restype = c_int32
    at.AudioQueueSetProperty.argtypes = [c_void_p, c_uint32, c_void_p, c_uint32]
    at.AudioQueueSetProperty.restype = c_int32
    at.AudioQueueAllocateBuffer.argtypes = [c_void_p, c_uint32, POINTER(POINTER(_AQBuffer))]
    at.AudioQueueAllocateBuffer.restype = c_int32
    at.AudioQueueEnqueueBuffer.argtypes = [c_void_p, POINTER(_AQBuffer), c_uint32, c_void_p]
    at.AudioQueueEnqueueBuffer.restype = c_int32
    at.AudioQueueStart.argtypes = [c_void_p, c_void_p]
    at.AudioQueueStart.restype = c_int32
    at.AudioQueueStop.argtypes = [c_void_p, ctypes.c_bool]
    at.AudioQueueDispose.argtypes = [c_void_p, ctypes.c_bool]
    return ca, at, cf


def _cfstr(cf, ref) -> str:
    buf = ctypes.create_string_buffer(512)
    return buf.value.decode() if ref and cf.CFStringGetCString(ref, buf, 512, 0x08000100) else ""


def find_output(ca, cf, name_prefix: str) -> Tuple[Optional[int], Optional[int]]:
    """(device id, UID CFString) of the first output device whose name starts with the prefix."""
    a = _Addr(_cc("dev#"), _cc("glob"), 0)
    size = c_uint32()
    if ca.AudioObjectGetPropertyDataSize(1, byref(a), 0, None, byref(size)):
        return None, None
    ids = (c_uint32 * (size.value // 4))()
    if ca.AudioObjectGetPropertyData(1, byref(a), 0, None, byref(size), ids):
        return None, None
    for d in ids:
        streams = _Addr(_cc("stm#"), _cc("outp"), 0)
        n = c_uint32()
        if ca.AudioObjectGetPropertyDataSize(d, byref(streams), 0, None, byref(n)) or not n.value:
            continue
        ref, rs = c_void_p(), c_uint32(ctypes.sizeof(c_void_p))
        if ca.AudioObjectGetPropertyData(d, byref(_Addr(_cc("lnam"), _cc("glob"), 0)), 0, None,
                                         byref(rs), byref(ref)):
            continue
        if not _cfstr(cf, ref.value).startswith(name_prefix):
            continue
        uid, us = c_void_p(), c_uint32(ctypes.sizeof(c_void_p))
        if ca.AudioObjectGetPropertyData(d, byref(_Addr(_cc("uid "), _cc("glob"), 0)), 0, None,
                                         byref(us), byref(uid)):
            continue
        return int(d), uid.value
    return None, None


def play_pcm(pcm: bytes, device: str, channels: int) -> int:
    """Play interleaved 16-bit PCM on the named device and wait for it. 0, or an error code."""
    ca, at, cf = _libs()
    dev, uid = find_output(ca, cf, device)
    if dev is None:
        return 2
    fmt = _ASBD(RATE, _cc("lpcm"), 0x4 | 0x8, 2 * channels, 1, 2 * channels, channels, 16, 0)
    q = c_void_p()
    cb = _CALLBACK(lambda *a: None)
    err = at.AudioQueueNewOutput(byref(fmt), cb, None, None, None, 0, byref(q))
    if err:
        return 3
    try:
        uidref = c_void_p(uid)
        if at.AudioQueueSetProperty(q, _cc("aqcd"), byref(uidref), ctypes.sizeof(c_void_p)):
            return 4  # could not pick the device: do not play on the default one
        buf = POINTER(_AQBuffer)()
        if at.AudioQueueAllocateBuffer(q, len(pcm), byref(buf)):
            return 5
        ctypes.memmove(buf.contents.mAudioData, pcm, len(pcm))
        buf.contents.mAudioDataByteSize = len(pcm)
        if at.AudioQueueEnqueueBuffer(q, buf, 0, None) or at.AudioQueueStart(q, None):
            return 6
        time.sleep(len(pcm) / (2 * channels * RATE) + 0.15)
        at.AudioQueueStop(q, True)
        return 0
    finally:
        at.AudioQueueDispose(q, True)


def main(argv: List[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m padpilot.sound")
    ap.add_argument("cmd", choices=["play"])
    ap.add_argument("sound")
    ap.add_argument("--device", default=DEVICE)
    ap.add_argument("--channel", type=int, default=1, help="0-based channel (1 = right)")
    ap.add_argument("--channels", type=int, default=4)
    ap.add_argument("--lead", type=int, default=0, help="silence before the sound, ms")
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--tail", type=int, default=0, help="silence after the sound, ms")
    a = ap.parse_args(argv)
    path = resolve(a.sound)
    if path is None:
        print(f"no such sound: {a.sound}", file=sys.stderr)
        return 1
    home = os.environ.get("PADPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".padpilot")
    mono = load(path, os.path.join(home, "sounds"))
    rc = play_pcm(interleave(mono, a.channel, a.channels, a.lead, a.gain, a.tail), a.device, a.channels)
    if rc:
        print(f"not played (code {rc})", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
