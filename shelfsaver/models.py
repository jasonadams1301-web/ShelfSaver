"""Plain data records shared across the app."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

LIBRARY_STATUSES = ("ok", "offline")
FILE_STATUSES = (
    "queued", "working", "converted", "skipped", "failed", "waiting_in_use", "missing",
)
IGNORED_REASON = "Ignored, left as is"  # a skip only the owner sets, and only a new copy ends
RUN_STATES = (
    "running", "swapping", "waiting_in_use", "done", "failed", "not_worth_it", "interrupted",
)


@dataclass
class Library:
    id: int
    name: str
    path: str
    level: str
    english_only: bool
    status: str = "ok"
    shrink_h265: bool = False
    remove_commentary: bool = False


@dataclass
class FileRow:
    id: int
    library_id: int
    path: str
    size: int
    mtime: float
    status: str
    reason: Optional[str] = None
    codec: Optional[str] = None
    container: Optional[str] = None
    resolution: Optional[str] = None
    updated_at: float = 0.0


@dataclass
class Run:
    id: int
    file_id: int
    state: str
    step: Optional[str] = None
    started_at: float = 0.0
    finished_at: Optional[float] = None
    was_size: Optional[int] = None
    now_size: Optional[int] = None
    was_format: Optional[str] = None
    now_format: Optional[str] = None
    now_path: Optional[str] = None
    was_bitrate: Optional[int] = None
    now_bitrate: Optional[int] = None
    why: Optional[str] = None
    tracks_json: Optional[str] = None
    checks_json: Optional[str] = None
    swap_json: Optional[str] = None
    error: Optional[str] = None
    log_tail: Optional[str] = None
    convert_seconds: Optional[float] = None
    saved_bytes: int = 0


@dataclass(frozen=True)
class Stream:
    index: int
    kind: str  # video | audio | subtitle | attachment | data
    codec: Optional[str] = None
    language: Optional[str] = None
    title: Optional[str] = None
    is_default: bool = False
    is_cover_art: bool = False
    is_commentary: bool = False  # an audio track the file marks, or titles, as commentary


@dataclass(frozen=True)
class MediaInfo:
    path: str
    size: int
    duration: float
    ext: str
    video_codec: Optional[str]
    width: int
    height: int
    video_bitrate: Optional[int]
    bit_depth: Optional[int]
    color_transfer: Optional[str]
    color_primaries: Optional[str]
    colorspace: Optional[str]
    field_order: Optional[str]
    dolby_vision: bool
    streams: tuple = ()


@dataclass(frozen=True)
class Decision:
    action: str  # convert | strip_only | skip
    reason: str
    remove_audio: tuple = ()
    why: str = ""
    sample_first: bool = False  # an H.265 file: convert only if a sample shows a big saving


@dataclass(frozen=True)
class VerifyResult:
    passed: bool
    checks: dict
    reason: str = ""
    not_worth_it: bool = False
    log: str = ""
