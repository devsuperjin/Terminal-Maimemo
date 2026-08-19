"""Pronunciation audio playback.

The web app's word sound comes from real MP3 files on maimemo's CDN —
``word.pronunciations[].url`` (e.g. ``https://cdn-by.maimemo.com/speeches/<uuid>.mp3``),
selected by accent (US / UK).  Phrase "sound" on the site is browser
``speechSynthesis`` (TTS); this module deliberately does NOT use TTS, only the
real pronunciation files.

Playback strategy (first one that works wins):

1. ``miniaudio.PlaybackDevice`` (bundled decoders + audio backends; works on
   most desktops).
2. Decode to a temp WAV and play it with a system player: ``paplay``,
   ``aplay`` on every ALSA playback device found via ``aplay -l``
   (``plughw:X,Y`` — needed when the ALSA default device is broken, e.g.
   HDMI-only machines), then ``ffplay`` / ``mpv``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import httpx
import miniaudio

log = logging.getLogger(__name__)

# serialize playback so overlapping words do not stack audio
_play_lock = asyncio.Lock()


def choose_pronunciation(word: dict, accent: str = "") -> str | None:
    """Pick the best pronunciation URL for a word dict.

    Prefers the requested accent ("US"/"UK"), then any accent.
    """
    prons = (word or {}).get("pronunciations") or []
    if not prons:
        return None
    if accent:
        for p in prons:
            if p.get("accent") == accent and p.get("url"):
                return p["url"]
    for p in prons:
        if p.get("url"):
            return p["url"]
    return None


# ---------------------------------------------------------------------------
# decoding / WAV conversion
# ---------------------------------------------------------------------------

def _decoded_to_wav(path: Path, decoded) -> None:
    """Write decoded samples to a 16-bit PCM WAV file."""
    samples = decoded.samples
    width = decoded.sample_width
    if width == 4:  # float32 -> int16
        pcm = bytearray()
        for v in samples:
            iv = int(v * 32767)
            iv = max(-32768, min(32767, iv))
            pcm += struct.pack("<h", iv)
        width = 2
    else:
        pcm = samples.tobytes() if hasattr(samples, "tobytes") else bytes(samples)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(decoded.nchannels)
        w.setsampwidth(width)
        w.setframerate(decoded.sample_rate)
        w.writeframes(bytes(pcm))


# ---------------------------------------------------------------------------
# system-player fallback
# ---------------------------------------------------------------------------

def _alsa_devices() -> list[str]:
    """Parse `aplay -l` and return `plughw:<card>,<device>` playback targets."""
    try:
        out = subprocess.run(
            ["aplay", "-l"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return []
    devices: list[str] = []
    card: str | None = None
    for line in out.splitlines():
        cm = re.search(r"card (\d+):", line)
        if cm:
            card = cm.group(1)
        dm = re.search(r"device (\d+):", line)
        if dm and card is not None:
            devices.append(f"plughw:{card},{dm.group(1)}")
    return devices


def _default_sink() -> str | None:
    """System default audio sink via pactl (PulseAudio / pipewire-pulse)."""
    if shutil.which("pactl"):
        try:
            r = subprocess.run(
                ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return None


def _system_play_wav(path: str) -> bool:
    """Play a WAV file with the system's audio framework.

    Ubuntu's system audio framework is PipeWire (WirePlumber manages the
    default sink) with pipewire-pulse for PulseAudio compatibility.  The
    players below therefore target the *system default device* (the one the
    user picked, e.g. a Jabra headset):

    1. ``paplay``  — PulseAudio client → pipewire-pulse → default sink
    2. ``pw-play`` — PipeWire native client → default target
    3. ``aplay -D default`` — ALSA default route
    4. ``aplay -D plughw:X,Y`` — every ALSA playback device from `aplay -l`
       (fallback for machines with a broken default route, e.g. HDMI-only)
    5. ``ffplay`` / ``mpv`` — last resort
    """
    sink = _default_sink()
    candidates: list[list[str]] = []
    if sink and shutil.which("paplay"):
        candidates.append(["paplay", "--device", sink, path])
    if shutil.which("paplay"):
        candidates.append(["paplay", path])
    if sink and shutil.which("pw-play"):
        candidates.append(["pw-play", "--target", sink, path])
    if shutil.which("pw-play"):
        candidates.append(["pw-play", path])
    if shutil.which("aplay"):
        candidates.append(["aplay", "-D", "default", path])
    for dev in _alsa_devices():
        if shutil.which("aplay"):
            candidates.append(["aplay", "-D", dev, path])
    if shutil.which("ffplay"):
        candidates.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path])
    if shutil.which("mpv"):
        candidates.append(["mpv", "--no-video", "--really-quiet", path])
    if not candidates:
        log.warning("no audio player available on this system")
        return False
    for cmd in candidates:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception as exc:
            log.warning("player %s failed to start: %s", cmd[0], exc)
            continue
        if r.returncode == 0:
            return True
        log.debug("player %s exited %d: %s", cmd[0], r.returncode, r.stderr.strip()[:200])
    return False


# ---------------------------------------------------------------------------
# playback
# ---------------------------------------------------------------------------

def _play_decoded(decoded) -> bool:
    """Play decoded samples. Returns True if audio actually played."""
    nch, rate, width = decoded.nchannels, decoded.sample_rate, decoded.sample_width
    if not nch or not rate:
        return False

    # 1) system audio framework first — respects the user's default device
    #    (PipeWire / pipewire-pulse / ALSA default, e.g. a Jabra headset).
    tmp = Path(tempfile.gettempdir()) / f"maimemo_pron_{int(time.time() * 1000)}.wav"
    try:
        _decoded_to_wav(tmp, decoded)
    except Exception as exc:
        log.warning("wav conversion failed: %s", exc)
    else:
        if _system_play_wav(str(tmp)):
            return True

    # 2) miniaudio in-process fallback (no temp file; uses miniaudio's own
    #    ALSA/PulseAudio backends).
    duration = len(decoded.samples) / (rate * nch)
    fmt = (
        miniaudio.SampleFormat.SIGNED16
        if width == 2
        else miniaudio.SampleFormat.FLOAT32
    )
    chunk = 4096 * nch
    samples = decoded.samples

    def gen():
        i = 0
        while i < len(samples):
            yield samples[i : i + chunk]
            i += chunk

    device = None
    try:
        device = miniaudio.PlaybackDevice(
            output_format=fmt, nchannels=nch, sample_rate=rate
        )
    except Exception as exc:
        log.warning("miniaudio device unavailable: %s", exc)
    if device is not None:
        try:
            device.start(gen())
            time.sleep(duration + 0.3)
            return True
        except Exception as exc:
            log.warning("miniaudio playback failed: %s", exc)
        finally:
            try:
                device.stop()
            except Exception:
                pass
            try:
                device.close()
            except Exception:
                pass

    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def _play_bytes(mp3_bytes: bytes) -> bool:
    try:
        decoded = miniaudio.decode(mp3_bytes)
    except Exception as exc:
        log.warning("audio decode failed: %s", exc)
        return False
    return _play_decoded(decoded)


async def play_mp3(url: str) -> bool:
    """Download and play an mp3 URL. Returns True on success."""
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as exc:
        log.warning("audio download failed: %s", exc)
        return False
    async with _play_lock:
        return await asyncio.to_thread(_play_bytes, resp.content)


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr).strip()
    except Exception as exc:
        return f"<{exc}>"


def _sinks_info() -> list[str]:
    lines: list[str] = []
    if shutil.which("pactl"):
        out = _run(["pactl", "info"])
        for line in out.splitlines():
            if "Default Sink" in line or "Server Name" in line:
                lines.append(line.strip())
    if shutil.which("wpctl"):
        out = _run(["wpctl", "status"])
        for line in out.splitlines():
            if "default" in line.lower() and ("sink" in line.lower() or "output" in line.lower()):
                lines.append(f"wpctl: {line.strip()}")
    return lines


def diagnose() -> str:
    """Return a human-readable report of the audio environment."""
    lines: list[str] = []
    lines.append("== audio environment ==")
    lines.append(f"PULSE_SERVER={os.environ.get('PULSE_SERVER', '')}")
    lines.append(f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '')}")
    lines.append(f"PIPEWIRE_RUNTIME_DIR={os.environ.get('PIPEWIRE_RUNTIME_DIR', '')}")
    lines.extend(_sinks_info())
    lines.append(f"default sink: {_default_sink() or '(none found)'}")
    players = ["paplay", "pw-play", "aplay", "ffplay", "mpv", "pactl", "wpctl"]
    lines.append(
        "players: "
        + ", ".join(f"{p}={'yes' if shutil.which(p) else 'no'}" for p in players)
    )
    lines.append(f"alsa devices: {', '.join(_alsa_devices()) or '(none)'}")
    try:
        lines.append(f"miniaudio backends: {[str(b) for b in miniaudio.get_enabled_backends()]}")
    except Exception as exc:
        lines.append(f"miniaudio backends: <{exc}>")
    return "\n".join(lines)


def make_test_tone(path: str, seconds: float = 0.6) -> bool:
    """Write a short 440 Hz test tone WAV (16-bit mono)."""
    import math

    rate = 44100
    frames = bytearray()
    for i in range(int(rate * seconds)):
        v = int(0.3 * 32767 * math.sin(2 * math.pi * 440 * i / rate))
        frames += struct.pack("<h", v)
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(frames))
        return True
    except Exception:
        return False


def play_tone_diagnose() -> str:
    """Play a test tone through every available method and report results."""
    tmp = Path(tempfile.gettempdir()) / "maimemo_test_tone.wav"
    if not make_test_tone(str(tmp)):
        return "Could not generate the test tone"
    lines = [f"Test tone file: {tmp}", "Trying each playback method:"]
    sink = _default_sink()
    attempts = []
    if sink and shutil.which("paplay"):
        attempts.append(["paplay", "--device", sink, str(tmp)])
    if shutil.which("paplay"):
        attempts.append(["paplay", str(tmp)])
    if sink and shutil.which("pw-play"):
        attempts.append(["pw-play", "--target", sink, str(tmp)])
    if shutil.which("pw-play"):
        attempts.append(["pw-play", str(tmp)])
    if shutil.which("aplay"):
        attempts.append(["aplay", "-D", "default", str(tmp)])
    for dev in _alsa_devices():
        attempts.append(["aplay", "-D", dev, str(tmp)])
    for name in ("ffplay", "mpv"):
        if shutil.which(name):
            attempts.append([name, str(tmp)])
    for cmd in attempts:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            status = "OK" if r.returncode == 0 else f"FAIL({r.returncode})"
            lines.append(f"  {' '.join(cmd)} -> {status}")
            if r.returncode == 0:
                lines.append("  ^ this one played successfully (you should hear it)")
                return "\n".join(lines)
        except Exception as exc:
            lines.append(f"  {' '.join(cmd)} -> ERROR {exc}")
    lines.append("All playback methods failed — see the errors above, or send this output for help.")
    return "\n".join(lines)
