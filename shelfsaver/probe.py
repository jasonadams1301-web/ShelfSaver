"""Read a media file with ffprobe."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from array import array
from dataclasses import dataclass
from pathlib import Path

from .jobobject import kill_with_app
from .models import MediaInfo, Stream


class ProbeError(Exception):
    """The file is unreadable or has no usable duration."""


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kind(raw: dict) -> str:
    kind = raw.get("codec_type")
    return kind if kind in ("video", "audio", "subtitle", "attachment") else "data"


def _tags(raw: dict) -> dict:
    return {str(k).lower(): v for k, v in (raw.get("tags") or {}).items()}


def _usable_rate(value) -> bool:
    try:
        num, _, den = str(value).partition("/")
        return float(num) > 0 and float(den or 1) > 0
    except ValueError:
        return False


_COMMENTARY_TITLE = re.compile(r"(?<![a-z])commentary(?![a-z])", re.IGNORECASE)


def _is_commentary(raw: dict, kind: str) -> bool:
    """An audio track the file flags as commentary, or one whose title says so.

    Both are checked because releases are inconsistent: some set only the flag, some only a title
    such as "Director's Commentary". Only audio counts; a subtitle titled that is a subtitle.
    """
    if kind != "audio":
        return False
    if (raw.get("disposition") or {}).get("comment"):
        return True
    return bool(_COMMENTARY_TITLE.search(str(_tags(raw).get("title") or "")))


def _is_cover_art(raw: dict, kind: str) -> bool:
    if (raw.get("disposition") or {}).get("attached_pic"):
        return True
    if str(_tags(raw).get("mimetype", "")).lower().startswith("image/"):
        return True
    if kind == "video" and raw.get("codec_name") in ("mjpeg", "png", "bmp"):
        has_rate = _usable_rate(raw.get("avg_frame_rate")) or _usable_rate(raw.get("r_frame_rate"))
        has_duration = (_float(raw.get("duration")) or 0) > 0
        return not (has_rate and has_duration)
    return False


def _bit_depth(raw: dict):
    depth = _int(raw.get("bits_per_raw_sample"))
    if depth:
        return depth
    pix = raw.get("pix_fmt") or ""
    for d in (16, 14, 12, 10, 9):
        if f"p{d}" in pix:
            return d
    return 8 if pix else None


def _has_dovi(raw: dict) -> bool:
    return any(
        "DOVI configuration record" in str(sd.get("side_data_type", ""))
        for sd in raw.get("side_data_list") or []
    )


def _stream_bitrate(raw: dict):
    br = _int(raw.get("bit_rate"))
    if br:
        return br
    return _int(_tags(raw).get("bps")) or None


def parse_probe(data: dict, path, size: int) -> MediaInfo:
    """Build a MediaInfo from ffprobe JSON output."""
    raws = data.get("streams") or []
    duration = _float((data.get("format") or {}).get("duration"))
    if not duration:
        duration = max((_float(r.get("duration")) or 0.0 for r in raws), default=0.0)
    if not duration or duration <= 0:
        raise ProbeError("File has no duration")

    streams = []
    main = None
    audio_rates = []
    for pos, raw in enumerate(raws):
        kind = _kind(raw)
        tags = _tags(raw)
        cover = _is_cover_art(raw, kind)
        index = _int(raw.get("index"))
        streams.append(Stream(
            index=pos if index is None else index,
            kind=kind,
            codec=raw.get("codec_name"),
            language=tags.get("language"),
            title=tags.get("title"),
            is_default=bool((raw.get("disposition") or {}).get("default")),
            is_cover_art=cover,
            is_commentary=_is_commentary(raw, kind),
        ))
        if kind == "video" and not cover and main is None:
            main = raw
        if kind == "audio":
            audio_rates.append(_stream_bitrate(raw))

    video_bitrate = None
    if main is not None:
        video_bitrate = _stream_bitrate(main)
        if not video_bitrate:
            estimate = size * 8 / duration - sum(r for r in audio_rates if r)
            video_bitrate = int(estimate) if estimate > 0 else None

    m = main or {}
    return MediaInfo(
        path=str(path),
        size=size,
        duration=duration,
        ext=Path(path).suffix.lower(),
        video_codec=m.get("codec_name"),
        width=_int(m.get("width")) or 0,
        height=_int(m.get("height")) or 0,
        video_bitrate=video_bitrate,
        bit_depth=_bit_depth(m) if main else None,
        color_transfer=m.get("color_transfer"),
        color_primaries=m.get("color_primaries"),
        colorspace=m.get("color_space"),
        field_order=m.get("field_order"),
        dolby_vision=_has_dovi(m),
        streams=tuple(streams),
    )


def probe(path, ffprobe) -> MediaInfo:
    path = Path(path)
    try:
        size = path.stat().st_size
        proc = subprocess.run(
            [str(ffprobe), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"Could not read file: {exc}") from exc
    if proc.returncode != 0:
        raise ProbeError("File could not be read")
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ProbeError("File could not be read") from exc
    return parse_probe(data, path, size)


UNREADABLE = "Couldn't read this file"


def file_details(info: MediaInfo) -> dict:
    """The codec, container and resolution kept on a file's row."""
    return {
        "codec": info.video_codec,
        "container": info.ext.lstrip("."),
        "resolution": f"{info.width}x{info.height}" if info.width else None,
    }


class ScanStopped(Exception):
    """A packet scan was interrupted by a stop request."""


@dataclass(frozen=True)
class StreamSpan:
    """When a stream's packets begin and end, and its largest gap between packets."""
    start: float
    end: float
    max_gap: float


def scan_streams(path, ffprobe, should_stop=lambda: False, strict=True) -> dict:
    """Packet timing for every stream, keyed by stream index.

    strict=True fails on any ffprobe error output or non-zero exit (ProbeError);
    strict=False accepts whatever packets could be read. Raises ScanStopped if
    should_stop() turns true.
    """
    cmd = [str(ffprobe), "-v", "error",
           "-show_entries", "packet=stream_index,pts_time,dts_time,duration_time",
           "-of", "csv=p=0", str(path)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=flags)
    except OSError as exc:
        raise ProbeError(f"Could not read file: {exc}") from exc
    kill_with_app(proc)
    errors = bytearray()

    def drain():
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            if len(errors) < 65536:
                errors.extend(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    times: dict = {}
    stopped = False
    next_check = 0.0
    try:
        for raw in proc.stdout:
            now = time.monotonic()
            if now >= next_check:
                if should_stop():
                    stopped = True
                    break
                next_check = now + 0.2
            parts = raw.decode("utf-8", "replace").strip().split(",")
            if len(parts) < 3:
                continue
            # AVI stores no presentation time for video with B-frames, only the decode time.
            index, when = _int(parts[0]), _float(parts[1])
            if when is None:
                when = _float(parts[2])
            if index is None or when is None:
                continue
            times.setdefault(index, array("d")).append(when)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait()
        reader.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
    if stopped:
        raise ScanStopped()
    if strict and (proc.returncode != 0 or errors.strip()):
        raise ProbeError("File could not be scanned")
    spans = {}
    for index, values in times.items():
        ordered = sorted(values)
        gaps = [b - a for a, b in zip(ordered, ordered[1:])]
        spans[index] = StreamSpan(ordered[0], ordered[-1], max(gaps, default=0.0))
    return spans
