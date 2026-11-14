"""The conversion pipeline: one file at a time, and never at the cost of an original.

The original stays untouched until the new file has passed every check and a verified copy
sits on the library's volume. From the moment a run is saved in state `swapping`, its temp
file can be the only copy of the new file, so nothing here deletes it unless the original
has been confirmed whole and unchanged where it started.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import hostos
from .decide import SAMPLE_FAILED, SAMPLE_SHORT, STRIP_WHY, TRIED_REASONS, decide
from .encode import EncodeResult, build_command, encoder_works, output_extension, run_ffmpeg
from .encoders import encoder_for
from .languages import load_language
from .models import IGNORED_REASON, RUN_STATES, Decision, FileRow, Library, MediaInfo, Run
from .probe import UNREADABLE, ProbeError, file_details, probe
from .quality import H265_MIN_SAVINGS
from .sample import SampleError, predict_saving
from .schedule import blackout_end, load_blackouts
from .swap import (
    FileInUse,
    SwapPlan,
    clear_holding,
    TargetExists,
    UnexpectedSwapState,
    _may_be_a_link,
    _present,
    _target_taken as target_is_taken,
    execute_swap,
    ext_path,
    is_volume_root,
    make_plan,
    recover_swap,
    refuse_linked_work_folders,
    work_root,
)
from .verify import HDR_TRANSFERS, verify_output

logger = logging.getLogger(__name__)

_local_now = datetime.now

GB = 1024 ** 3
SPACE_RESERVE_BYTES = 5 * GB
COPY_CHUNK_BYTES = 8 * 1024 * 1024
IN_USE_RETRY_SECONDS = 600
IDLE_WAIT_SECONDS = 1.0
BLOCKED_WAIT_SECONDS = 30.0
ENCODER_FAILURE_LIMIT = 3

SWAP_STATES = ("swapping", "waiting_in_use")

NO_SCRATCH_SET = "Choose a scratch disk in Settings"
NO_SCRATCH_FOUND = "The scratch disk can't be found"
NO_ROOM_SCRATCH = "Not enough room on the scratch disk"
NO_ROOM_LIBRARY = "Not enough room on the library drive"
BREAKER_MESSAGE = "Paused: conversions keep failing. Check the graphics card and logs."

WAITING_IN_USE = "Waiting, file is in use"
WHOLE_DRIVE = "This library is a whole drive. Choose a folder on the drive instead."
CONVERT_FAILED = "The conversion failed"
SOMETHING_WRONG = "Something went wrong with this file"
SWAP_KEPT = ("Couldn't finish replacing this file. Nothing was deleted. "
             "Retry it, or it will be tried again at the next start.")
SWAP_RECORD_LOST = ("Couldn't finish replacing this file, because the record of it was lost. "
                    "Nothing was deleted. Its files are in the ShelfSaver folder on this drive.")
TARGET_TAKEN = "A different file is already where the new file needs to go"
LOCKED_NOTE = "The file is in use, so it will be tried again"
STOPPED_NOTE = "Stopped before finishing, so it will start again"
INTERRUPTED_NOTE = "Interrupted by a restart, so it will start again"
RESOLVED_NOTE = ("Marked as handled by you. Nothing was deleted or moved. Any leftover files "
                 "are in the ShelfSaver folder on this drive.")
CHANGED_NOTE = "The original changed during the conversion, so it will start again"
DRIVE_OFFLINE_NOTE = "The library drive went offline, so this will start again once it is back"

_FORMAT_NAMES = {
    "h264": "H.264", "hevc": "H.265", "mpeg2video": "MPEG-2", "mpeg1video": "MPEG-1",
    "mpeg4": "MPEG-4", "msmpeg4v3": "DivX", "vc1": "VC-1", "wmv3": "WMV", "av1": "AV1",
    "vp8": "VP8", "vp9": "VP9",
}

_TEMP_STEM = re.compile(r"[0-9]{1,18}-[0-9]{1,18}")

_now = time.monotonic


def free_bytes(path) -> int:
    return shutil.disk_usage(ext_path(path)).free


def _list_folder(path) -> list[os.DirEntry]:
    with os.scandir(ext_path(path)) as entries:
        return list(entries)


def _format_name(codec: Optional[str]) -> str:
    return _FORMAT_NAMES.get(codec or "", (codec or "unknown").upper())


def _stream_record(stream) -> dict:
    return {"index": stream.index, "kind": stream.kind, "codec": stream.codec,
            "language": stream.language, "title": stream.title}


def _tracks_json(info: MediaInfo, decision: Decision) -> str:
    gone = set(decision.remove_audio)

    def is_removed(stream) -> bool:
        return stream.kind == "data" or stream.index in gone

    return json.dumps({
        "kept": [_stream_record(s) for s in info.streams if not is_removed(s)],
        "removed": [_stream_record(s) for s in info.streams if is_removed(s)],
        "hdr": info.color_transfer in HDR_TRANSFERS,
    })


class _Stopped(Exception):
    """A pause or shutdown interrupted a copy."""


class _SourceLocked(Exception):
    """Another program won't let the file being copied be read. Not an `OSError`, so it can't
    be mistaken for trouble with the destination."""


def _joined(*parts: str) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _temp_stem(run_id: int, started_at: float) -> str:
    """The name of a run's temp file. A run ID alone can come round again if the database is
    ever replaced, while a kept temp from before that still needs protecting."""
    return f"{run_id}-{int(started_at * 1000)}"


def _is_locked(path) -> bool:
    """True if another program won't let this file be read right now."""
    try:
        with open(ext_path(path), "rb") as reader:
            reader.read(4096)  # a lock on part of a file only shows up when reading it
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def _copy_file(source, target, on_fraction: Callable[[float], None],
               should_stop: Callable[[], bool]) -> None:
    """Copy contents only, so a read-only flag can't follow the file, and commit them to disk.

    Raises `FileExistsError` rather than ever writing over something at `target`. Whenever this
    raises anything else, whatever is at `target` was created here.
    """
    with open(ext_path(target), "xb") as writer:
        try:
            reader = open(ext_path(source), "rb")
        except PermissionError as exc:
            raise _SourceLocked(str(exc)) from exc
        done = 0
        with reader:
            total = os.fstat(reader.fileno()).st_size
            while True:
                try:
                    chunk = reader.read(COPY_CHUNK_BYTES)
                except PermissionError as exc:
                    raise _SourceLocked(str(exc)) from exc
                if not chunk:
                    break
                if should_stop():
                    raise _Stopped()
                writer.write(chunk)
                done += len(chunk)
                if total:
                    on_fraction(min(1.0, done / total))
        writer.flush()
        os.fsync(writer.fileno())


def _sha256(path, should_stop: Callable[[], bool]) -> str:
    digest = hashlib.sha256()
    with open(ext_path(path), "rb") as reader:
        while chunk := reader.read(COPY_CHUNK_BYTES):
            if should_stop():
                raise _Stopped()
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path, size: int, mtime: float) -> bool:
    """True if the file still has this size and time. Raises `OSError` if it can't be read."""
    info = os.stat(ext_path(path))
    return info.st_size == size and info.st_mtime == mtime


def _load_swap(text: Optional[str]) -> tuple[SwapPlan, int, float]:
    """Read a saved plan. Raises `ValueError`, `TypeError` or `KeyError` if it is damaged."""
    data = json.loads(text or "")
    if not isinstance(data, dict):
        raise ValueError("The saved plan is not a record")
    # Every field without a default must be present (a missing one means the plan is damaged).
    # Fields WITH a default may be absent: a plan saved before that field existed is still valid
    # and must still load, or a swap interrupted across an upgrade could never be finished.
    kwargs = {
        f.name: data[f.name] if f.default is dataclasses.MISSING else data.get(f.name, f.default)
        for f in dataclasses.fields(SwapPlan)
    }
    plan = SwapPlan(**kwargs)
    for path in (plan.original, plan.temp, plan.final):
        if not isinstance(path, str):
            raise ValueError("The saved plan has a bad path")
    if plan.temp_fingerprint is not None and not isinstance(plan.temp_fingerprint, str):
        raise ValueError("The saved plan has a bad fingerprint")
    return plan, int(data["original_size"]), float(data["original_mtime"])


def _dump_swap(plan: SwapPlan, original_size: int, original_mtime: float) -> str:
    return json.dumps({**dataclasses.asdict(plan), "original_size": original_size,
                       "original_mtime": original_mtime})


class _RunFailed(Exception):
    """The run can't succeed. The message is the plain sentence for the file's row."""


@dataclass
class _Job:
    """One file on its way through the pipeline."""
    file: FileRow
    library: Library
    size: int
    mtime: float
    info: MediaInfo
    decision: Decision
    run: Run
    scratch_folder: Path
    output: Path
    temp: Optional[Path] = None
    log_tail: str = ""
    swap_saved: bool = False


class Worker:
    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.encoder = encoder_for(config)  # chosen once: a conversion must not switch mid-run
        self._lock = threading.Lock()  # guards everything status() and retry() touch
        self._status = self._idle_fields()
        self._step_started = 0.0
        self._paused = threading.Event()
        self._pause_message: Optional[str] = None
        self._stop: Optional[threading.Event] = None
        self._next_swap_try: dict[int, float] = {}  # run id -> when a waiting run is next due
        self._next_locked_try: dict[int, float] = {}  # file id -> when a locked file is next due
        self._swap_retry_asked: set[int] = set()  # run ids the owner asked to try now
        self._parked: Optional[_Job] = None  # converted and verified, waiting for library room
        self._failed_encodes: list[int] = []  # file ids of the current failures in a row
        self._holding_lock = threading.Lock()  # held while holding is cleared or a swap begins
        self._claimed: set[int] = set()  # run ids whose swap is being advanced or resolved

    # ---- Controls and status, safe to call from any thread ----

    def status(self) -> dict:
        with self._lock:
            current = dict(self._status)
            if self._paused.is_set():
                current["state"] = "paused"
                current["message"] = self._pause_message
        return current

    def pause(self, reason: Optional[str] = None) -> None:
        with self._lock:
            self._pause_message = reason
            self._paused.set()
        logger.info("Paused: %s", reason or "at the owner's request")

    def resume(self) -> None:
        with self._lock:
            self._pause_message = None
            self._failed_encodes = []
            self._paused.clear()
        logger.info("Resumed")

    def retry(self, file_id: int) -> None:
        """Try a failed file again. A run that still holds a new file carries on from there."""
        held = [r.id for r in self.db.active_runs(SWAP_STATES) if r.file_id == file_id]
        if held:
            with self._lock:
                self._swap_retry_asked.update(held)
            return
        file = self.db.get_file(file_id)
        if file is not None and (file.status in ("failed", "waiting_in_use")
                                 or file.reason == IGNORED_REASON):
            with self._lock:
                self._next_locked_try.pop(file_id, None)
            self.db.requeue([file_id])

    def resolve_swap(self, file_id: int) -> tuple[str, Optional[str]]:
        """End a kept swap at the owner's request, without touching any file.

        Returns (outcome, held_path). The outcome is "resolved", "none" (the file has no kept
        swap), "busy" (the swap is being tried right now) or "held" (the original is gone and
        a held file may be the only copy, so the swap must be sorted out first;
        held_path names it when it is known).
        """
        file = self.db.get_file(file_id)
        runs = [r for r in self.db.active_runs(("swapping",)) if r.file_id == file_id]
        if file is None or file.status != "failed" or not runs:
            return "none", None
        ids = {r.id for r in runs}
        # Claim the runs first, so no attempt can begin or be under way while this decides.
        with self._lock:
            if self._claimed & ids:
                return "busy", None
            self._claimed |= ids
            for run_id in ids:
                self._next_swap_try.pop(run_id, None)
                self._swap_retry_asked.discard(run_id)
        try:
            # Read again now that nothing else can change them.
            runs = [r for r in self.db.active_runs(("swapping",)) if r.id in ids]
            file = self.db.get_file(file_id)
            if file is None or file.status != "failed" or not runs:
                return "none", None
            for run in runs:
                blocker = self._only_copy_may_be_held(run, file)
                if blocker is not None:
                    return "held", blocker or None
            for run in runs:
                self._end_run(run.id, "failed", RESOLVED_NOTE)
            self.db.set_file_status(file_id, "failed", RESOLVED_NOTE)
            logger.info("The owner marked the kept swap for file %s as handled", file_id)
            return "resolved", None
        finally:
            with self._lock:
                self._claimed -= ids

    def _only_copy_may_be_held(self, run: Run, file: FileRow) -> Optional[str]:
        """The held path (or "" when unknown) if the original is gone while its copy may be in
        the holding folder, otherwise None."""
        try:
            plan = _load_swap(run.swap_json)[0]
            if plan.holding is not None and not isinstance(plan.holding, str):
                raise ValueError("The saved plan has a bad holding path")
        except (ValueError, TypeError, KeyError):
            plan = None
        original = plan.original if plan else file.path
        try:
            if _present(original):
                return None
            if plan is None:
                return ""
            return plan.holding if plan.holding and _present(plan.holding) else None
        except OSError:
            return plan.holding if plan and plan.holding else ""

    def clear_holding_when_idle(self, paths, states) -> Optional[int]:
        """Empty the holding folders unless a file is being replaced. Returns the bytes freed,
        or None when a run is in the way.

        Nothing can start replacing a file while this runs, because that step takes the same
        lock. A finished conversion parked for room on the library drive is not in the way:
        emptying the holding folder is how to make room.
        """
        with self._holding_lock:
            volumes = {os.path.normcase(str(work_root(p))) for p in paths}
            job = self._parked
            parked = job.run.id if job is not None else None
            for run in self.db.active_runs(states):
                if run.id == parked:
                    continue
                library_id = (self.db.run_record(run.id) or {}).get("library_id")
                library = next((l for l in self.db.list_libraries() if l.id == library_id), None)
                if library is None or os.path.normcase(str(work_root(library.path))) in volumes:
                    return None
            return clear_holding(paths)

    # ---- Status helpers ----

    @staticmethod
    def _idle_fields() -> dict:
        return {"state": "idle", "file": None, "step": None, "percent": None,
                "eta_seconds": None, "message": None, "blackout_until": None}

    def _set_status(self, state: str, file: Optional[str] = None, step: Optional[str] = None,
                    message: Optional[str] = None) -> None:
        with self._lock:
            self._status = {**self._idle_fields(), "state": state, "file": file, "step": step,
                            "message": message}
            self._step_started = _now()

    def _set_idle(self) -> None:
        self._set_status("idle")

    def _begin_step(self, job: _Job, step: str) -> None:
        logger.info("%s: %s", step, job.file.path)
        self._set_status("working", job.file.path, step)
        self.db.update_run(job.run.id, step=step)

    def _on_progress(self, fraction: float) -> None:
        with self._lock:
            self._status["percent"] = round(fraction * 100, 1)
            elapsed = _now() - self._step_started
            self._status["eta_seconds"] = (
                round(elapsed / fraction * (1 - fraction)) if fraction > 0 else None
            )

    def _should_stop(self) -> bool:
        return self._paused.is_set() or (self._stop is not None and self._stop.is_set())

    # ---- The main loop ----

    def run_forever(self, stop: threading.Event) -> None:
        self._stop = stop
        try:
            while not stop.is_set():
                try:
                    worked = self.process_one()
                except Exception:
                    logger.exception("The worker hit an unexpected error")
                    worked = False
                if not worked:
                    blocked = self.status()["state"] == "blocked"
                    stop.wait(BLOCKED_WAIT_SECONDS if blocked else IDLE_WAIT_SECONDS)
        finally:
            self._stop = None

    def process_one(self) -> bool:
        """Do the next piece of work. False means there was nothing to do right now."""
        if self._paused.is_set():
            return False
        worked = self._advance_due_swaps()
        self._requeue_due_locked_files()
        if self._parked is not None:
            return self._guarded(self._parked, self._place_new_file) or worked

        scratch = self._usable_scratch()
        if scratch is None:
            return worked
        # Only new files wait: a file already under way finishes, and swaps above still happen.
        until = blackout_end(load_blackouts(self.db.get_setting("blackouts")), _local_now())
        if until is not None:
            self._set_status("blackout")
            with self._lock:
                self._status["blackout_until"] = until.isoformat(timespec="minutes")
            return worked
        file = self._next_file()
        if file is None:
            self._set_idle()
            return worked
        try:
            finished = self._process_file(file, scratch)
        except Exception:
            # Once a run exists `_guarded` handles errors, so no new file can be at stake here.
            logger.exception("Unexpected error with %s", file.path)
            self.db.set_file_status(file.id, "failed", SOMETHING_WRONG)
            finished = True
        if finished:
            self._set_idle()
        return finished or worked

    def _usable_scratch(self) -> Optional[Path]:
        setting = self.db.get_setting("scratch_dir")
        if not setting:
            self._set_status("no_scratch", message=NO_SCRATCH_SET)
            return None
        if not os.path.isdir(ext_path(setting)):
            self._set_status("no_scratch", message=NO_SCRATCH_FOUND)
            return None
        return Path(setting)

    def _next_file(self) -> Optional[FileRow]:
        """The next queued file on a reachable library that has no swap still pending."""
        unreachable = [lib.id for lib in self.db.list_libraries()
                       if not os.path.isdir(ext_path(lib.path))]
        while True:
            file = self.db.next_queued(unreachable)
            if file is None:
                return None
            pending = [r for r in self.db.active_runs(SWAP_STATES) if r.file_id == file.id]
            if not pending:
                return file
            # Something requeued it, but a second run could fight the pending swap's new file.
            if pending[0].state == "waiting_in_use":
                self.db.set_file_status(file.id, "waiting_in_use", WAITING_IN_USE)
            else:
                self.db.set_file_status(file.id, "failed", pending[0].error or SWAP_KEPT)

    def _requeue_due_locked_files(self) -> None:
        """Files that were locked before a run could start go back in the queue when due."""
        now = _now()
        with_swap = {r.file_id for r in self.db.active_runs(SWAP_STATES)}
        for file in self.db.files_with_status("waiting_in_use"):
            if file.id in with_swap:
                continue
            with self._lock:
                due = now >= self._next_locked_try.get(file.id, now)
                if due:
                    self._next_locked_try.pop(file.id, None)
            if due:
                self.db.requeue([file.id])

    def _wait_for_lock(self, file_id: int, path: str) -> None:
        """Waiting rather than requeued, or the same large file would be picked again at once."""
        logger.info("In use, will try again later: %s", path)
        with self._lock:
            self._next_locked_try[file_id] = _now() + IN_USE_RETRY_SECONDS
        self.db.set_file_status(file_id, "waiting_in_use", WAITING_IN_USE)

    # ---- Steps 1 and 2: decide whether to work on it at all ----

    def _process_file(self, file: FileRow, scratch: Path) -> bool:
        library = next((lib for lib in self.db.list_libraries() if lib.id == file.library_id),
                       None)
        if library is None:
            self.db.set_file_status(file.id, "failed", "Its library no longer exists")
            return True
        if is_volume_root(library.path):
            logger.error("%s is in a library that is a whole drive", file.path)
            self.db.set_file_status(file.id, "failed", WHOLE_DRIVE)
            return True

        logger.info("Inspecting: %s", file.path)
        self._set_status("working", file.path)
        try:
            info_stat = os.stat(ext_path(file.path))
        except FileNotFoundError:
            logger.info("No longer there: %s", file.path)
            self.db.set_missing_unless_owned(file.id)
            return True
        except PermissionError:
            self._wait_for_lock(file.id, file.path)
            return True
        except OSError as exc:
            logger.error("Could not read %s: %s", file.path, exc)
            self.db.set_file_status(file.id, "failed", UNREADABLE)
            return True
        if _is_locked(file.path):
            self._wait_for_lock(file.id, file.path)
            return True
        size, mtime = info_stat.st_size, info_stat.st_mtime
        try:
            info = probe(ext_path(file.path), self.config.ffprobe)
        except ProbeError as exc:
            logger.error("Could not read %s: %s", file.path, exc)
            self.db.upsert_file(library.id, file.path, size, mtime, "failed", UNREADABLE)
            return True

        decision = decide(info, library.level, library.english_only,
                          nlink=info_stat.st_nlink,
                          read_only=not info_stat.st_mode & stat.S_IWRITE,
                          language=load_language(self.db.get_setting("default_language")),
                          shrink_h265=library.shrink_h265,
                          remove_commentary=library.remove_commentary,
                          tried=file.reason if file.reason in TRIED_REASONS else None)
        details = file_details(info)
        if decision.action == "skip":
            logger.info("Skipping %s: %s", file.path, decision.reason)
            self.db.upsert_file(library.id, file.path, size, mtime, "skipped", decision.reason,
                                **details)
            return True

        out_ext = output_extension(info, decision)
        final = os.path.splitext(file.path)[0] + out_ext
        if (os.path.normcase(final) != os.path.normcase(file.path)
                and os.path.lexists(ext_path(final))):
            logger.error("%s can't be converted, because %s already exists", file.path, final)
            self.db.upsert_file(library.id, file.path, size, mtime, "failed", TARGET_TAKEN,
                                **details)
            return True

        try:
            scratch_room = free_bytes(scratch)
        except OSError as exc:
            logger.warning("Could not check the room on %s: %s", scratch, exc)
            self._set_status("no_scratch", message=NO_SCRATCH_FOUND)
            return False
        if scratch_room < 2 * size + SPACE_RESERVE_BYTES:
            logger.info("Not enough room on the scratch disk for %s", file.path)
            self._set_status("blocked", file.path, message=NO_ROOM_SCRATCH)
            return False

        # Claim the row first. If the scanner changed it since it was picked, take nothing.
        if not self.db.claim_file(file.id, file.size, file.mtime, size, mtime, **details):
            logger.info("%s changed while being looked at, so it is left for later", file.path)
            return True
        try:
            run = self.db.start_run(file.id, size, _format_name(info.video_codec),
                                    info.video_bitrate, decision.why)
        except BaseException:
            self.db.set_file_status(file.id, "queued")
            raise
        folder = scratch / "ShelfSaver" / f"run-{run.id}"
        job = _Job(file, library, size, mtime, info, decision, run, folder,
                   folder / f"output{out_ext}")
        return self._guarded(job, self._convert_and_check)

    # ---- Failure handling shared by every step of a run ----

    def _guarded(self, job: _Job, step: Callable[[_Job], bool]) -> bool:
        """Run pipeline steps so that no failure can leave a run half-handled."""
        finished = True
        try:
            finished = step(job)
        except _Stopped:
            logger.info("Stopped, and will start again: %s", job.file.path)
            self._end_and_requeue(job, STOPPED_NOTE)
        except _SourceLocked:
            self._end_run(job.run.id, "interrupted", LOCKED_NOTE)
            self._delete_run_files(job)
            self._wait_for_lock(job.file.id, job.file.path)
        except _RunFailed as exc:
            logger.error("%s: %s\n%s", job.file.path, exc, job.log_tail)
            self._fail_run(job, str(exc))
        except Exception:
            logger.exception("Unexpected error with %s", job.file.path)
            if job.swap_saved:
                self._keep_swapping(job.run.id, job.file.id, "an unexpected error")
            else:
                self._fail_run(job, SOMETHING_WRONG)
        if finished:
            self._set_idle()
        return finished

    def _delete_run_files(self, job: _Job) -> None:
        self._parked = None
        if job.temp is not None:
            self._delete_temp(job.temp)
        self._delete_scratch_folder(job.scratch_folder)

    @staticmethod
    def _delete_temp(path) -> None:
        folder = Path(path).parent
        if (folder.name.lower() != "incoming"
                or folder.parent.name.lower() != "shelfsaver"):
            logger.error("Refusing to delete %s, because it isn't in ShelfSaver\\incoming", path)
            return
        try:
            os.unlink(ext_path(path))
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)

    @staticmethod
    def _delete_scratch_folder(folder) -> None:
        try:
            shutil.rmtree(ext_path(folder))
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not delete %s: %s", folder, exc)

    def _end_run(self, run_id: int, state: str, error: Optional[str]) -> None:
        self.db.update_run(run_id, state=state, error=error, finished_at=time.time())

    def _fail_run(self, job: _Job, reason: str) -> None:
        self._end_run(job.run.id, "failed", reason)
        self.db.set_file_status(job.file.id, "failed", reason)
        self._delete_run_files(job)

    def _end_and_requeue(self, job: _Job, note: str) -> None:
        self._end_run(job.run.id, "interrupted", note)
        self.db.requeue([job.file.id])
        self._delete_run_files(job)

    # ---- Steps 3 and 4: convert on the scratch disk and check the result ----

    def _convert_and_check(self, job: _Job) -> bool:
        os.makedirs(ext_path(job.scratch_folder), exist_ok=True)
        if job.decision.sample_first and not self._sample_says_go(job):
            return True
        source_copy = job.scratch_folder / f"source{job.info.ext}"

        self._begin_step(job, "Copying")
        try:
            _copy_file(job.file.path, source_copy, self._on_progress, self._should_stop)
        except OSError as exc:
            logger.error("Could not copy %s to the scratch disk: %s", job.file.path, exc)
            raise _RunFailed("Couldn't copy this file to the scratch disk") from exc
        return self._convert_copy(job, source_copy)

    def _convert_copy(self, job: _Job, source_copy: Path) -> bool:
        self._begin_step(job, "Converting")
        cmd = build_command(job.info, job.decision, job.library.level, source_copy, job.output,
                            self.config.ffmpeg, self.encoder)
        convert_began = _now()
        try:
            result = run_ffmpeg(cmd, job.info.duration, self._on_progress, self._should_stop)
        except OSError as exc:
            result = EncodeResult(-1, False, f"FFmpeg could not start: {exc}")
        job.log_tail = _joined(job.log_tail, result.log_tail)
        self.db.update_run(job.run.id, log_tail=job.log_tail,
                           convert_seconds=_now() - convert_began)

        out_info = None
        if result.returncode == 0:
            try:
                out_info = probe(job.output, self.config.ffprobe)
            except ProbeError as exc:
                logger.error("FFmpeg's result for %s can't be read: %s", job.file.path, exc)
        if out_info is None:
            if result.stopped or self._should_stop():
                raise _Stopped()
            self._encoder_failed(job, result)
            return True
        with self._lock:
            self._failed_encodes = []

        self._begin_step(job, "Checking")
        try:
            source_info = probe(source_copy, self.config.ffprobe)
        except ProbeError as exc:
            raise _RunFailed("The copy on the scratch disk could not be read") from exc
        verdict = verify_output(source_info, job.output, job.decision, self.config.ffmpeg,
                                self.config.ffprobe, self._should_stop)
        if verdict.log:
            job.log_tail = f"{job.log_tail.rstrip()}\n\nPlayback check:\n{verdict.log}"
        self.db.update_run(job.run.id, checks_json=json.dumps(verdict.checks),
                           log_tail=job.log_tail)
        if not verdict.passed:
            if verdict.reason == "Stopped":
                raise _Stopped()
            if verdict.not_worth_it:
                if job.decision.sample_first and self._strip_instead(job):
                    return self._convert_copy(job, source_copy)
                self._not_worth_it(job, verdict.reason)
                return True
            raise _RunFailed(verdict.reason)

        self.db.update_run(
            job.run.id, now_size=os.stat(ext_path(job.output)).st_size,
            now_format=_format_name(out_info.video_codec), now_bitrate=out_info.video_bitrate,
            tracks_json=_tracks_json(job.info, job.decision),
        )
        return self._place_new_file(job)

    def _sample_says_go(self, job: _Job) -> bool:
        """Test a sample of an H.265 file. False when the run has ended here."""
        self._begin_step(job, "Testing a sample")
        try:
            saving = predict_saving(job.info, job.library.level, ext_path(job.file.path),
                                    Path(ext_path(job.scratch_folder / "sample")),
                                    self.config.ffmpeg, self.config.ffprobe, self._should_stop,
                                    self.encoder)
        except SampleError as exc:
            self._add_to_log(job, f"{SAMPLE_FAILED}: {exc}")
            if self._strip_instead(job):
                return True
            self._not_worth_it(job, SAMPLE_FAILED)
            return False
        if saving is None:
            raise _Stopped()
        change = f"{saving:.0%} smaller" if saving >= 0 else f"{-saving:.0%} bigger"
        self._add_to_log(job, f"Sample: about {change}")
        if saving >= H265_MIN_SAVINGS or self._strip_instead(job):
            return True
        self._not_worth_it(job, SAMPLE_SHORT)
        return False

    def _strip_instead(self, job: _Job) -> bool:
        """Fall back to only removing extra audio, when there is any to remove."""
        if not job.decision.remove_audio:
            return False
        logger.info("Only removing the extra audio from %s", job.file.path)
        job.decision = Decision("strip_only", "Already H.265", job.decision.remove_audio, STRIP_WHY)
        self.db.update_run(job.run.id, why=STRIP_WHY)
        return True

    def _add_to_log(self, job: _Job, text: str) -> None:
        job.log_tail = _joined(job.log_tail, text)
        self.db.update_run(job.run.id, log_tail=job.log_tail)

    def _not_worth_it(self, job: _Job, reason: str) -> None:
        logger.info("Not worth replacing %s: %s", job.file.path, reason)
        self._end_run(job.run.id, "not_worth_it", reason)
        self.db.set_file_status(job.file.id, "skipped", reason)
        self._delete_run_files(job)

    def _encoder_failed(self, job: _Job, result: EncodeResult) -> None:
        if result.returncode == 0:
            logger.error("FFmpeg finished %s, but its result can't be read\n%s", job.file.path,
                         result.log_tail)
        else:
            logger.error("FFmpeg exited with %s on %s\n%s", result.returncode, job.file.path,
                         result.log_tail)
        self._fail_run(job, CONVERT_FAILED)
        with self._lock:
            self._failed_encodes.append(job.file.id)
            failed = list(self._failed_encodes)
        if len(failed) >= ENCODER_FAILURE_LIMIT:
            if encoder_works(self.config.ffmpeg, self.encoder):
                logger.warning("The graphics card works, so these files are the problem: %s",
                               failed)
                with self._lock:
                    self._failed_encodes = []
                return
            logger.error("Conversions keep failing, so pausing")
            self.db.requeue(failed)
            self.pause(BREAKER_MESSAGE)

    # ---- Steps 5 to 8: bring the new file to the library's volume and swap it in ----

    def _library_room(self, job: _Job) -> Optional[int]:
        """Free bytes on the library's drive, or None when it can't be reached."""
        try:
            if not os.path.isdir(ext_path(job.library.path)):
                return None
            return free_bytes(job.library.path)
        except OSError as exc:
            logger.warning("Could not check the room on %s: %s", job.library.path, exc)
            return None

    def _place_new_file(self, job: _Job) -> bool:
        room = self._library_room(job)
        if room is None:
            # Holding the only parked place until the drive is back would stop every other
            # library, so this one starts again later. Its original was never touched.
            logger.warning("The library drive is offline, so %s will start again later",
                           job.file.path)
            self.db.update_library(job.library.id, status="offline")
            self._end_and_requeue(job, DRIVE_OFFLINE_NOTE)
            return True
        if room < os.stat(ext_path(job.output)).st_size + SPACE_RESERVE_BYTES:
            # Keep the finished conversion: it is the expensive part, and nothing is at risk.
            logger.info("Not enough room on the library drive for %s", job.file.path)
            self._parked = job
            self._set_status("blocked", job.file.path, message=NO_ROOM_LIBRARY)
            return False
        self._parked = None

        self._begin_step(job, "Replacing")
        incoming = work_root(job.library.path) / "incoming"
        temp = incoming / f"{_temp_stem(job.run.id, job.run.started_at)}{job.output.suffix}"
        job.temp = temp
        try:
            # Never build into a work folder that has been replaced by a link.
            refuse_linked_work_folders(work_root(job.library.path), incoming)
            # Made like the library folder, so its owner can still open and clean up
            # `ShelfSaver/incoming` (see hostos.make_dirs_like).
            problems = hostos.make_dirs_like(ext_path(incoming), ext_path(job.library.path))
            if problems:
                logger.warning(
                    "The work folder %s could not be given the library folder's owner and "
                    "permissions (%s); it keeps the user ShelfSaver runs as. Run ShelfSaver as "
                    "the user that owns your library, or as root.", incoming, "; ".join(problems))
            _copy_file(job.output, temp, self._on_progress, self._should_stop)
            same = (_sha256(job.output, self._should_stop)
                    == _sha256(temp, self._should_stop))
        except FileExistsError as exc:
            job.temp = None  # not ours, so no clean-up may touch it
            logger.error("%s already exists, and may be the only copy of a new file", temp)
            raise _RunFailed("Couldn't copy the new file to the library drive") from exc
        except OSError as exc:
            logger.error("Could not copy the new file for %s: %s", job.file.path, exc)
            raise _RunFailed("Couldn't copy the new file to the library drive") from exc
        if not same:
            raise _RunFailed("The new file was damaged while copying to the library drive")
        if self._should_stop():
            raise _Stopped()

        try:
            unchanged = _matches(job.file.path, job.size, job.mtime)
        except OSError:
            unchanged = False
        if not unchanged:
            logger.info("The original changed during the conversion: %s", job.file.path)
            self._end_and_requeue(job, CHANGED_NOTE)
            return True

        holding = self.db.get_setting("holding_enabled") == "1"
        try:
            plan = make_plan(job.file.path, job.temp, job.output.suffix, job.library.path,
                             job.library.name, holding)
        except (ValueError, OSError) as exc:
            logger.error("Could not plan the replacement of %s: %s", job.file.path, exc)
            raise _RunFailed("Couldn't plan how to replace this file") from exc

        # Written before anything moves, so a crash at any later point can be picked up.
        with self._holding_lock:
            if plan.holding:
                self.db.remember_held_volume(str(work_root(job.library.path).parent))
            self.db.update_run(job.run.id, state="swapping", now_path=plan.final,
                               swap_json=_dump_swap(plan, job.size, job.mtime))
        job.swap_saved = True
        self._advance_swap(next(r for r in self.db.active_runs(("swapping",))
                                if r.id == job.run.id))
        return True

    def _advance_due_swaps(self) -> bool:
        """Try every swap that is due. An attempt that can't proceed never holds up other work."""
        now = _now()
        with self._lock:
            asked = set(self._swap_retry_asked)
            self._swap_retry_asked.clear()
            times = dict(self._next_swap_try)
        tried = False
        for run in self.db.active_runs(SWAP_STATES):
            due = run.id in asked or (run.state == "waiting_in_use"
                                      and now >= times.get(run.id, now))
            if not due:
                continue
            tried = True
            try:
                self._advance_swap(run)
            except Exception:
                logger.exception("Unexpected error retrying the swap for run %s", run.id)
        if tried:
            self._set_idle()
        return tried

    def _advance_swap(self, run: Run) -> None:
        with self._lock:
            if run.id in self._claimed:
                return  # being resolved, or already being tried
            self._claimed.add(run.id)
        try:
            current = self.db.run_record(run.id)
            if current is None or current["state"] not in SWAP_STATES:
                return  # it was closed while this attempt was waiting
            self._advance_swap_steps(run)
        finally:
            with self._lock:
                self._claimed.discard(run.id)

    def _advance_swap_steps(self, run: Run) -> None:
        """Move a saved swap as far as it will go, and record what happened. Never raises for
        anything a swap can report."""
        try:
            plan, size, mtime = _load_swap(run.swap_json)
        except (ValueError, TypeError, KeyError) as exc:
            self._keep_swapping(run.id, run.file_id, f"a damaged saved plan ({exc})",
                                SWAP_RECORD_LOST)
            return

        self._set_status("working", plan.original, "Replacing")
        logger.info("Replacing: %s", plan.original)
        try:
            outcome = recover_swap(plan)
            if outcome == "unexpected" and target_is_taken(plan):
                raise TargetExists(f"A different file is in the way for {plan.original}")
            if outcome == "not_started":
                if not _matches(plan.original, size, mtime):
                    self._original_changed(run, plan)
                    return
                execute_swap(plan)
                outcome = "finished"
        except FileInUse:
            self._wait_for_file(run, plan)
        except TargetExists as exc:
            self._target_taken(run, plan, size, mtime, exc)
        except (UnexpectedSwapState, OSError) as exc:
            self._keep_swapping(run.id, run.file_id, str(exc))
        else:
            if outcome == "finished":
                self._record_done(run, plan)
            else:
                self._keep_swapping(run.id, run.file_id, "files on disk that match no swap step")

    def _wait_for_file(self, run: Run, plan: SwapPlan) -> None:
        logger.info("In use, will try again later: %s", plan.original)
        with self._lock:
            self._next_swap_try[run.id] = _now() + IN_USE_RETRY_SECONDS
        self.db.update_run(run.id, state="waiting_in_use", step="Replacing", error=None)
        self.db.set_file_status(run.file_id, "waiting_in_use", WAITING_IN_USE)
        # The verified copy is on the library's volume now, so free the scratch disk.
        self._delete_run_scratch(run)

    def _keep_swapping(self, run_id: int, file_id: int, why: str, reason: str = SWAP_KEPT) -> None:
        """The temp file may be the only copy of the new file, so nothing is cleaned up."""
        logger.error("Keeping run %s for a later attempt, because of %s", run_id, why)
        self.db.update_run(run_id, state="swapping", error=reason)
        self.db.set_file_status(file_id, "failed", reason)

    def _target_taken(self, run: Run, plan: SwapPlan, size: int, mtime: float, exc) -> None:
        try:
            intact = _matches(plan.original, size, mtime)
        except OSError:
            intact = False
        if not intact:
            self._keep_swapping(run.id, run.file_id, f"a blocked swap ({exc})")
            return
        logger.error("Something is in the way of the swap for %s: %s", plan.original, exc)
        self._end_run(run.id, "failed", TARGET_TAKEN)
        self.db.set_file_status(run.file_id, "failed", TARGET_TAKEN)
        self._clean_up_swap(run, plan)

    def _original_changed(self, run: Run, plan: SwapPlan) -> None:
        logger.info("The original changed before it could be replaced: %s", plan.original)
        self._end_run(run.id, "interrupted", CHANGED_NOTE)
        self.db.requeue([run.file_id])
        self._clean_up_swap(run, plan)

    def _clean_up_swap(self, run: Run, plan: SwapPlan) -> None:
        self._delete_temp(plan.temp)
        self._delete_run_scratch(run)
        self._forget_swap(run.id)

    def _forget_swap(self, run_id: int) -> None:
        with self._lock:
            self._next_swap_try.pop(run_id, None)
            self._swap_retry_asked.discard(run_id)

    def _delete_run_scratch(self, run: Run) -> None:
        setting = self.db.get_setting("scratch_dir")
        if setting:
            self._delete_scratch_folder(Path(setting) / "ShelfSaver" / f"run-{run.id}")

    # ---- Step 9: record the outcome ----

    def _record_done(self, run: Run, plan: SwapPlan) -> None:
        now_size = run.now_size if run.now_size is not None else plan.expected_size
        saved = (run.was_size or 0) - now_size
        # The row goes first: a crash before the run is marked just repeats this harmlessly.
        self._update_row_after_swap(run, plan, now_size)
        self.db.update_run(run.id, state="done", step="Done", finished_at=time.time(),
                           error=None, now_path=plan.final, now_size=now_size,
                           saved_bytes=saved)
        self._delete_run_scratch(run)
        self._forget_swap(run.id)
        logger.info("Finished %s, saving %s bytes", plan.final, saved)

    def _update_row_after_swap(self, run: Run, plan: SwapPlan, now_size: int) -> None:
        """Store the new file's size and time, so a later scan knows it needs no converting."""
        file = self.db.get_file(run.file_id)
        if file is None:
            return
        try:
            info = os.stat(ext_path(plan.final))
            size, mtime = info.st_size, info.st_mtime
        except OSError as exc:
            logger.warning("Could not read the finished file %s: %s", plan.final, exc)
            size, mtime = now_size, file.mtime
        try:
            details = file_details(probe(ext_path(plan.final), self.config.ffprobe))
        except ProbeError as exc:
            logger.warning("Could not inspect the finished file %s: %s", plan.final, exc)
            details = {"codec": file.codec, "container": Path(plan.final).suffix.lstrip("."),
                       "resolution": file.resolution}
        self.db.record_converted(file.id, plan.final, size, mtime, **details)

    # ---- Startup ----

    def recover(self) -> None:
        """Pick up after a crash or restart. One bad run or folder can't stop the rest."""
        self._parked = None
        for run in self.db.active_runs(SWAP_STATES):
            try:
                self._advance_swap(run)
            except Exception:
                logger.exception("Could not recover run %s", run.id)

        undecided = set()
        for run in self.db.active_runs(("running",)):
            try:
                mid_swap = self._looks_mid_swap(run)
            except OSError as exc:
                logger.error("Leaving run %s until the next start, because the disk can't say "
                             "whether it began replacing its file: %s", run.id, exc)
                undecided.add(run.file_id)
                continue
            if mid_swap:
                self._keep_swapping(run.id, run.file_id, "an original that is gone while its "
                                    "new file is still waiting", SWAP_RECORD_LOST)
            else:
                self._end_run(run.id, "interrupted", INTERRUPTED_NOTE)
        leave_alone = undecided | {r.file_id for r in self.db.active_runs(SWAP_STATES)}
        working = [f.id for f in self.db.files_with_status("working")
                   if f.id not in leave_alone]
        if working:
            logger.info("Starting %s interrupted files again", len(working))
            self.db.requeue(working)

        self._delete_leftover_run_folders()
        self._delete_orphan_incoming_files()
        self._set_idle()

    def _looks_mid_swap(self, run: Run) -> bool:
        """True if a run with no saved plan may have started swapping anyway.

        With its original gone and its temp file there, the temp can be the only new copy and
        the original can be in the holding folder. Nothing proves which files are which without
        the plan, so they are left for the owner. Keeping the run in `swapping` also stops the
        holding folder being cleared. Raises `OSError` if the disk can't say, including when
        the whole volume is gone.
        """
        record = self.db.run_record(run.id)
        original = record and record["file_path"]
        if not original or _present(original):
            return False
        work = work_root(original)
        if not _present(work.parent):
            raise OSError(f"The volume holding {original} can't be reached")
        try:
            found = _list_folder(work / "incoming")
        except FileNotFoundError:
            return False
        stem = _temp_stem(run.id, run.started_at)
        return any(Path(entry.name).stem == stem for entry in found)

    def _delete_leftover_run_folders(self) -> None:
        setting = self.db.get_setting("scratch_dir")
        if not setting:
            return
        folder = Path(setting) / "ShelfSaver"
        try:
            if _may_be_a_link(folder):
                logger.warning("Refusing to clean %s because it may link elsewhere", folder)
                return
            entries = _list_folder(folder)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Could not read %s: %s", folder, exc)
            return
        for entry in entries:
            if not fnmatch.fnmatch(entry.name, "run-*"):
                continue
            try:
                # A link never counts as a run folder, so what it points at is never touched.
                if not _may_be_a_link(entry.path) and entry.is_dir(follow_symlinks=False):
                    self._delete_scratch_folder(entry.path)
            except OSError as exc:
                logger.warning("Could not clean %s: %s", entry.path, exc)

    def _delete_orphan_incoming_files(self) -> None:
        planned = self._planned_temp_ids()
        folders = {os.path.normcase(str(work_root(lib.path) / "incoming")):
                   work_root(lib.path) / "incoming" for lib in self.db.list_libraries()}
        for folder in folders.values():
            try:
                if _may_be_a_link(folder):
                    logger.warning("Refusing to clean %s because it may link elsewhere", folder)
                    continue
                entries = _list_folder(folder)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("Could not read %s: %s", folder, exc)
                continue
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if self._is_clear_orphan(entry.path, planned):
                        logger.info("Removing a leftover file: %s", entry.path)
                        self._delete_temp(entry.path)
                    else:
                        logger.error("Keeping %s, because it may be the only copy of a new "
                                     "file", entry.path)
                except OSError as exc:
                    logger.warning("Could not clean %s: %s", entry.path, exc)

    def _planned_temp_ids(self) -> set[int]:
        ids = set()
        for run in self.db.active_runs(RUN_STATES):
            if run.swap_json:
                try:
                    ids.add(_load_swap(run.swap_json)[0].temp_file_id)
                except (ValueError, TypeError, KeyError):
                    continue
        return ids

    def _is_clear_orphan(self, path: str, planned: set[int]) -> bool:
        """True only if no plan names this file, and its whole name matches one run instance
        that has ended with its original still in place. `lexists` reads any disk error as
        absent, which keeps it."""
        if os.lstat(ext_path(path)).st_ino in planned:
            return False
        stem = Path(path).stem
        if not _TEMP_STEM.fullmatch(stem):
            return False
        record = self.db.run_record(int(stem.split("-")[0]))
        if (record is None or record["state"] in ("running", *SWAP_STATES)
                or _temp_stem(record["id"], record["started_at"]) != stem):
            return False
        return bool(record["file_path"]) and os.path.lexists(ext_path(record["file_path"]))
