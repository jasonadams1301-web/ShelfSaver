"""Decide what to do with a probed file."""
from __future__ import annotations

import re
from typing import Optional

from .languages import DEFAULT_LANGUAGE, is_language, primary_language
from .models import Decision, MediaInfo
from .quality import HDR_TRANSFERS, LEVELS, resolution_bucket

_DUB = re.compile(r"\bdub(bed)?\b", re.IGNORECASE)

_EFFICIENT_WHY = "Already stored in a modern, efficient format"
STRIP_WHY = "Only the extra audio tracks can be removed"
SAMPLE_SHORT = "Sample showed too little saving"
SAMPLE_FAILED = "Sample test failed"
# Skip reasons that mean an H.265 file was already tried, so it isn't sampled again.
TRIED_REASONS = (SAMPLE_SHORT, SAMPLE_FAILED, "Would not save enough")


def _over_limit(info: MediaInfo, level: str) -> bool:
    mbps = LEVELS[level].min_mbps[resolution_bucket(info.width)]
    return info.video_bitrate is not None and info.video_bitrate >= mbps * 1_000_000


def audio_to_remove(streams, language: str = DEFAULT_LANGUAGE) -> tuple:
    """Stream indexes of audio in other languages that are safe to drop; () when unsure."""
    audio = [s for s in streams if s.kind == "audio"]
    if len(audio) < 2:
        return ()
    if any(primary_language(s.language) in ("", "und") for s in audio):
        return ()
    if any(s.title and _DUB.search(s.title) for s in audio):
        return ()
    if not is_language(audio[0].language, language):
        return ()
    if any(s.is_default and not is_language(s.language, language) for s in audio):
        return ()
    return tuple(s.index for s in audio if not is_language(s.language, language))


def commentary_to_remove(streams, already_removed=()) -> tuple:
    """Stream indexes of commentary audio that are safe to drop; () when unsure.

    Safe means a real soundtrack is left: a file whose audio is all commentary, or whose default
    track is commentary, is not what this rule expects, so it is left as it is. `already_removed`
    is what the language rule is dropping, so the two rules together can never empty the audio.
    """
    audio = [s for s in streams if s.kind == "audio"]
    if len(audio) < 2:
        return ()
    if any(s.is_default and s.is_commentary for s in audio):
        return ()
    gone = set(already_removed)
    if not any(not s.is_commentary and s.index not in gone for s in audio):
        return ()
    return tuple(s.index for s in audio if s.is_commentary and s.index not in gone)


def _skip(reason: str, why: str) -> Decision:
    return Decision("skip", reason, (), why)


def decide(info: MediaInfo, level: str, english_only: bool,
           nlink: int = 1, read_only: bool = False,
           language: str = DEFAULT_LANGUAGE, shrink_h265: bool = False,
           tried: Optional[str] = None, remove_commentary: bool = False) -> Decision:
    """`tried` is the reason an earlier try at shrinking this H.265 file ended, if one did."""
    if nlink > 1:
        return _skip("Linked to another copy",
                     "Another file shares this one's data, so changing it would affect both")
    if read_only:
        return _skip("Read-only file", "The file is marked read-only")
    if not info.video_codec:
        return _skip("No video", "The file has no video track")
    if info.dolby_vision:
        return _skip("Dolby Vision, left as is",
                     "Converting would lose the Dolby Vision picture")

    removal = audio_to_remove(info.streams, language) if english_only else ()
    if remove_commentary:
        extra = commentary_to_remove(info.streams, removal)
        if extra:
            removal = tuple(sorted({*removal, *extra}))

    skip = None
    sample_first = False
    if info.video_codec == "hevc":
        oversized = (shrink_h265 and info.color_transfer not in HDR_TRANSFERS
                     and _over_limit(info, level))
        sample_first = oversized and not tried
        if not sample_first:
            skip = (tried if oversized else "Already H.265", _EFFICIENT_WHY)
    elif info.video_codec in ("av1", "vp9"):
        skip = ("Already an efficient format", _EFFICIENT_WHY)
    else:
        mbps = LEVELS[level].min_mbps[resolution_bucket(info.width)]
        if info.video_bitrate is not None and info.video_bitrate < mbps * 1_000_000:
            skip = ("Already small", "Already uses little data for its resolution")

    if skip and not removal:
        return _skip(*skip)
    # FFmpeg can't write a track it can't identify, so any new file would fail.
    if any(s.kind == "subtitle" and not s.codec for s in info.streams):
        return _skip("Unreadable subtitles, left as is",
                     "Some subtitle tracks are in a format that can't be carried over")
    if skip:
        return Decision("strip_only", skip[0], removal, STRIP_WHY)
    if sample_first:
        return Decision("convert", "Worth converting", removal,
                        "Already H.265, but uses far more data than it needs at this resolution",
                        sample_first=True)
    return Decision("convert", "Worth converting", removal,
                    "Uses far more data than it needs at this resolution")
