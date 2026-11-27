"""Build and run the ffmpeg conversion command."""
from __future__ import annotations

import math
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .encoders import DEFAULT_ENCODER, Encoder
from .jobobject import kill_with_app
from .models import Decision, MediaInfo, Stream

LOG_TAIL_BYTES = 8 * 1024
KILL_AFTER_SECONDS = 10
ENCODER_CHECK_SECONDS = 30

MP4_EXTENSIONS = (".mp4", ".m4v")
MP4_AUDIO = frozenset({"aac", "ac3", "eac3", "mp3", "opus", "flac", "alac"})
MP4_SUBTITLES = frozenset({"mov_text"})


@dataclass
class EncodeResult:
    returncode: int
    stopped: bool
    log_tail: str


def _mapped_streams(info: MediaInfo, decision: Decision) -> list:
    """Streams that go into the output, in source order."""
    removed = set(decision.remove_audio)
    return [
        s for s in info.streams
        if s.kind != "data" and not (s.kind == "audio" and s.index in removed)
    ]


def output_extension(info: MediaInfo, decision: Decision) -> str:
    ext = (info.ext or "").lower()
    if ext == ".mkv":
        return ".mkv"
    if ext in MP4_EXTENSIONS:
        for s in _mapped_streams(info, decision):
            if s.kind == "subtitle" and s.codec not in MP4_SUBTITLES:
                return ".mkv"
            if s.kind == "audio" and s.codec not in MP4_AUDIO:
                return ".mkv"
        return ext
    return ".mkv"


def _is_main_video(stream: Stream) -> bool:
    return stream.kind == "video" and not stream.is_cover_art


def build_command(info: MediaInfo, decision: Decision, level: str,
                  source: Path, output: Path, ffmpeg: str,
                  encoder: Encoder = DEFAULT_ENCODER) -> list:
    """The ffmpeg command for one file. `encoder` supplies everything hardware-specific; the
    mapping, containers, subtitles and colour tags below are the same for every encoder."""
    source, output = Path(source), Path(output)
    if source == output:
        raise ValueError("Output must not be the source file")
    out_ext = output.suffix.lower()
    to_mp4 = out_ext in MP4_EXTENSIONS
    converting = decision.action == "convert"
    streams = _mapped_streams(info, decision)

    # Hardware device setup, if the encoder needs any, has to come before the input it applies to.
    cmd = [str(ffmpeg), "-y", "-v", "warning", *encoder.input_args(), "-i", str(source)]
    for s in streams:
        cmd += ["-map", f"0:{s.index}"]

    # Output index of each mapped stream within its own kind.
    counters: dict = {}
    main_video_out = None if streams else 0
    subs_to_srt = []
    other_video = []
    for s in streams:
        n = counters.get(s.kind, 0)
        counters[s.kind] = n + 1
        if main_video_out is None and _is_main_video(s):
            main_video_out = n
        elif s.kind == "video":
            other_video.append(n)
        if s.kind == "subtitle" and s.codec == "mov_text" and not to_mp4:
            subs_to_srt.append(n)

    encoding = converting and main_video_out is not None
    if encoding:
        for n in other_video:
            cmd += [f"-c:v:{n}", "copy"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "copy", "-c:s", "copy", "-c:t", "copy"]
    for n in subs_to_srt:
        cmd += [f"-c:s:{n}", "srt"]

    if encoding:
        v = main_video_out
        deep = (info.bit_depth or 8) > 8
        cmd += encoder.codec_args(v, level, deep)
        for flag, value in (("-color_primaries", info.color_primaries),
                            ("-color_trc", info.color_transfer),
                            ("-colorspace", info.colorspace)):
            if value and value != "unknown":
                cmd += [f"{flag}:v:{v}", value]
        deinterlace = info.field_order not in (None, "", "progressive", "unknown")
        filters = encoder.filters(deinterlace, deep)
        if filters:
            cmd += [f"-filter:v:{v}", ",".join(filters)]
        if to_mp4:
            cmd += [f"-tag:v:{v}", "hvc1"]

    cmd += ["-map_metadata", "0", "-map_chapters", "0",
            "-max_muxing_queue_size", "4096", str(output)]
    return cmd


def encoder_works(ffmpeg, encoder: Encoder = DEFAULT_ENCODER) -> bool:
    """Whether the encoder can encode one blank frame, with no video file involved."""
    cmd = [str(ffmpeg), "-v", "error", *encoder.input_args(), "-f", "lavfi", "-i",
           "color=size=640x360:rate=25", *encoder.self_test_args(), "-f", "null", "-"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        return False
    kill_with_app(proc)
    try:
        return proc.wait(timeout=ENCODER_CHECK_SECONDS) == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False


def _positive(text: str) -> Optional[float]:
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def parse_fps(text: str) -> Optional[float]:
    """The frame rate from FFmpeg's `fps=` progress line, or None before it has a real one."""
    return _positive(text)


def parse_speed(text: str) -> Optional[float]:
    """The speed against playback from FFmpeg's `speed=` progress line, such as `6.2x`."""
    text = text.strip()
    return _positive(text[:-1]) if text.endswith("x") else None


def run_ffmpeg(cmd: list, duration: float, on_progress: Callable[[float], None],
               should_stop: Callable[[], bool],
               on_rate: Optional[Callable[[Optional[float], Optional[float]], None]] = None,
               ) -> EncodeResult:
    full = list(cmd[:1]) + ["-progress", "pipe:1", "-nostats"] + list(cmd[1:])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=flags)
    kill_with_app(proc)
    tail = bytearray()
    lock = threading.Lock()

    def drain_stderr():
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            with lock:
                tail.extend(chunk)
                del tail[:-LOG_TAIL_BYTES]

    reader = threading.Thread(target=drain_stderr, daemon=True)
    reader.start()

    stopped = False
    fps = speed = None
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    us = -1
                if us >= 0 and duration > 0:
                    on_progress(min(1.0, us / 1_000_000 / duration))
            elif line.startswith("fps="):
                fps = parse_fps(line[4:])
            elif line.startswith("speed="):
                speed = parse_speed(line[6:])
            elif line.startswith("progress=") and on_rate is not None:
                on_rate(fps, speed)
            if should_stop():
                stopped = True
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=KILL_AFTER_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait()
        reader.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
    with lock:
        text = bytes(tail).decode("utf-8", "replace")
    return EncodeResult(proc.returncode, stopped, text)
