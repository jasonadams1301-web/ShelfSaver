"""Find video files in the libraries and keep the queue up to date.

The scanner only decides what is queued, skipped or missing. It never touches a file the
worker owns, and it never marks files missing unless the library's folder is reachable.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import swap
from .decide import TRIED_REASONS, decide
from .languages import load_language
from .models import IGNORED_REASON, FileRow, Library
from .probe import UNREADABLE, ProbeError, file_details, probe
from .swap import ext_path

logger = logging.getLogger(__name__)

EXTENSIONS = frozenset(
    ".mkv .mp4 .m4v .avi .mov .wmv .mpg .mpeg .ts .m2ts .flv .asf .webm".split())
SKIP_SUFFIXES = (".partial", ".part", ".!qb")
SKIP_FOLDERS = frozenset({"video_ts", "bdmv"})
OWNED_STATUSES = frozenset({"working", "waiting_in_use"})
OWNED_RUN_STATES = ("swapping", "waiting_in_use")
READ_ONLY_ATTRIBUTE = 0x1  # FILE_ATTRIBUTE_READONLY

STABLE_SECONDS = 60.0
PENDING_INTERVAL = 15.0
FULL_INTERVAL = 6 * 3600.0


class _TryAgain(Exception):
    """The file is busy or not settled yet; look at it again on the next check."""


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _is_inside(path: str, folder: str) -> bool:
    path, folder = _norm(path), _norm(folder)
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


class Scanner:
    def __init__(self, db, config, stable_seconds: float = STABLE_SECONDS,
                 pending_interval: float = PENDING_INTERVAL,
                 full_interval: float = FULL_INTERVAL,
                 clock: Callable[[], float] = time.monotonic):
        self.db = db
        self.config = config
        self.stable_seconds = stable_seconds
        self.pending_interval = pending_interval
        self.full_interval = full_interval
        self.clock = clock
        self._lock = threading.Lock()
        self._seen: dict[str, tuple] = {}   # path -> (size, mtime, first time seen so)
        self._pending: set[str] = set()     # paths to look at again
        self._to_check: set[str] = set()    # paths a running full scan has yet to reach
        self._scans_running = 0
        self._observer = None
        self._watches: dict[str, object] = {}  # normalised library path -> watch
        self._stop: Optional[threading.Event] = None

    def status(self) -> dict:
        """Safe from any thread: whether a scan is running and how many files await a look."""
        with self._lock:
            return {"scanning": self._scans_running > 0,
                    "pending": len(self._pending | self._to_check)}

    # ---- rules

    def _stopping(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    @staticmethod
    def _reachable(library: Library) -> bool:
        try:
            return os.path.isdir(ext_path(library.path))
        except OSError:
            return False

    @staticmethod
    def _work_root(library: Library) -> str:
        return str(swap.work_root(library.path))

    def _is_candidate(self, path: str, work: Optional[str] = None,
                      root: Optional[str] = None) -> bool:
        name = os.path.basename(path)
        lower = name.lower()
        if lower.endswith(SKIP_SUFFIXES) or os.path.splitext(lower)[1] not in EXTENSIONS:
            return False
        inner = os.path.relpath(path, root) if root and _is_inside(path, root) else path
        if any(part.lower() in SKIP_FOLDERS for part in Path(inner).parts[:-1]):
            return False
        return not (work and _is_inside(path, work))

    def _owned(self, file: Optional[FileRow]) -> bool:
        """True while the worker owns this file, so its row and path must not change."""
        if file is None:
            return False
        if file.status in OWNED_STATUSES:
            return True
        return any(r.file_id == file.id for r in self.db.active_runs(OWNED_RUN_STATES))

    def _stable(self, path: str, size: int, mtime: float) -> bool:
        key, now = _norm(path), self.clock()
        with self._lock:
            seen = self._seen.get(key)
            if seen is None or seen[:2] != (size, mtime):
                self._seen[key] = (size, mtime, now)
                return self.stable_seconds <= 0
            return now - seen[2] >= self.stable_seconds

    def _can_skip_wait(self, path: str, st) -> bool:
        """For a full scan: nothing has changed this file for a while, so it needs no settling.

        A file a watcher event noted, or that had to be looked at again, always waits, because
        a copied file can arrive with an old time.
        """
        with self._lock:
            if path in self._pending:
                return False
        return time.time() - st.st_mtime >= self.stable_seconds

    def _forget(self, path: str) -> None:
        with self._lock:
            self._seen.pop(_norm(path), None)
            self._pending.discard(path)

    def _add_pending(self, path: str) -> None:
        with self._lock:
            self._pending.add(path)

    # ---- one file

    def _consider(self, library: Library, path: str, st, row: Optional[FileRow],
                  force: bool = False, full_scan: bool = False) -> None:
        """Bring one file's row up to date. Raises _TryAgain if it should be revisited."""
        if self._owned(row):
            self._forget(path)
            return
        nlink = st.st_nlink
        unchanged = row is not None and (row.size, row.mtime) == (st.st_size, st.st_mtime)
        if unchanged and row.status == "skipped" and row.reason == IGNORED_REASON:
            self._forget(path)
            return
        if (unchanged and not force and row.status != "missing"
                and not (row.status == "queued" and nlink > 1)):
            self._forget(path)
            return
        skipped_wait = not unchanged and full_scan and self._can_skip_wait(path, st)
        if not unchanged and not skipped_wait and not self._stable(path, st.st_size, st.st_mtime):
            raise _TryAgain(path)
        if row is None:
            row = self._follow_rename(library, path, st)
            if row is not None and row.status != "missing":
                self._forget(path)
                return
        try:
            with open(ext_path(path), "rb") as reader:
                reader.read(4096)
        except PermissionError as exc:  # in use or locked: the worker deals with locks later
            raise _TryAgain(path) from exc
        except FileNotFoundError:
            return
        except OSError:
            pass  # let the probe decide whether it is readable
        try:
            info = probe(ext_path(path), self.config.ffprobe)
        except ProbeError as exc:
            try:
                with open(ext_path(path), "rb") as reader:
                    reader.read(4096)
            except PermissionError as inner:
                raise _TryAgain(path) from inner
            except FileNotFoundError:
                return
            except OSError:
                pass
            # It may still be arriving, so it gets the wait it skipped before it counts as bad.
            if skipped_wait and not self._stable(path, st.st_size, st.st_mtime):
                raise _TryAgain(path) from exc
            logger.error("Could not read %s: %s", path, exc)
            self._write(row, library, path, st, "failed", UNREADABLE)
            self._forget(path)
            return

        decision = decide(info, library.level, library.english_only, nlink=nlink,
                          read_only=bool(getattr(st, "st_file_attributes", 0)
                                         & READ_ONLY_ATTRIBUTE),
                          language=load_language(self.db.get_setting("default_language")),
                          shrink_h265=library.shrink_h265,
                          remove_commentary=library.remove_commentary,
                          tried=row.reason if row and row.reason in TRIED_REASONS else None)
        details = file_details(info)
        if row is not None and row.status == "converted":
            # Never converted twice; only extra audio can still be removed. A changed
            # file in an older format is a new copy, unless it is an original that was put back.
            new_copy = (decision.action == "convert" and not unchanged
                        and not self.db.was_converted_from(row.id, st.st_size))
            if decision.action == "strip_only" or new_copy:
                self._write(row, library, path, st, "queued", None, **details)
            else:
                self._write(row, library, path, st, "converted", None, **details)
        elif decision.action == "skip":
            self._write(row, library, path, st, "skipped", decision.reason, **details)
        else:
            self._write(row, library, path, st, "queued", None, **details)
        self._forget(path)

    def _follow_rename(self, library: Library, path: str, st) -> Optional[FileRow]:
        """The row of a known file that now has this name, moved over to it, or None.

        A rename keeps a file's size and time, so one match whose old name is gone is that file.
        More than one could be any of them, so the file then counts as new.
        """
        matches = [r for r in self.db.files_with_size_and_time(library.id, st.st_size, st.st_mtime)
                   if _norm(r.path) == _norm(path) or not os.path.lexists(ext_path(r.path))]
        if len(matches) != 1 or self._owned(matches[0]):
            return None
        moved = self.db.follow_rename(matches[0].id, path)
        if moved is not None:
            logger.info("Renamed: %s is now %s", matches[0].path, path)
        return moved

    def _write(self, row, library, path, st, status, reason, **details) -> None:
        self.db.upsert_file_unless_owned(library.id, path, st.st_size, st.st_mtime, status,
                                         reason, **details)

    def _process(self, library: Library, path: str, row: Optional[FileRow] = None,
                 force: bool = False, full_scan: bool = False) -> bool:
        """Stat and consider one path. False when it should be revisited."""
        try:
            if row is None:
                row = self.db.get_file_by_path(path)
            try:
                st = os.stat(ext_path(path))
            except FileNotFoundError:
                self._forget(path)
                # A row with another path was renamed after the caller looked it up.
                if (row is not None and row.path == path and row.status != "missing"
                        and not self._owned(row) and self._reachable(library)):
                    self.db.set_missing_unless_owned(row.id)
                return True
            except PermissionError:
                return False
            self._consider(library, path, st, row, force, full_scan)
            return True
        except _TryAgain:
            return False
        except Exception:
            logger.exception("Could not scan %s", path)
            self._forget(path)
            return True

    # ---- scanning

    def _find_files(self, root: str, work: str, problems: list):
        """Yield (path, stat) for candidate files. Appends to problems when a folder or file
        could not be read."""
        stack = [root]
        while stack and not self._stopping():
            folder = stack.pop()
            try:
                with os.scandir(ext_path(folder)) as entries:
                    listing = list(entries)
            except OSError as exc:
                logger.warning("Could not list %s: %s", folder, exc)
                problems.append(folder)
                continue
            for entry in listing:
                path = os.path.join(folder, entry.name)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if (entry.name.lower() not in SKIP_FOLDERS
                                and not _is_inside(path, work)):
                            stack.append(path)
                        continue
                    if not self._is_candidate(path, work, root):
                        continue
                    yield path, entry.stat()
                except OSError as exc:
                    logger.warning("Could not read %s: %s", path, exc)
                    problems.append(path)

    @staticmethod
    def _needs_a_look(rows: dict, path: str, st) -> bool:
        row = rows.get(_norm(path))
        return (row is None or row.status == "missing"
                or (row.size, row.mtime) != (st.st_size, st.st_mtime))

    def scan_library(self, library_id: int) -> None:
        library = next((l for l in self.db.list_libraries() if l.id == library_id), None)
        if library is None:
            return
        if not self._reachable(library):
            if library.status != "offline":
                logger.warning("Library %s is offline", library.name)
                self.db.update_library(library.id, status="offline")
            return
        came_back = library.status != "ok"
        if came_back:
            self.db.update_library(library.id, status="ok")
            library = next(l for l in self.db.list_libraries() if l.id == library_id)
            self.refresh_watches()

        root = os.path.normpath(library.path)
        work = self._work_root(library)
        rows = {_norm(r.path): r for r in self.db.files_in_library(library.id)}
        problems: list = []
        with self._lock:
            self._scans_running += 1
        to_check: set[str] = set()
        try:
            listing = list(self._find_files(root, work, problems))
            found = {_norm(path) for path, _ in listing}
            to_check = {path for path, st in listing if self._needs_a_look(rows, path, st)}
            with self._lock:
                self._to_check |= to_check
            for path, _ in listing:
                if self._stopping():
                    break
                settled = self._process(library, path, full_scan=True)
                with self._lock:
                    if settled:
                        self._pending.discard(path)
                    else:
                        self._pending.add(path)
                    self._to_check.discard(path)
        finally:
            with self._lock:
                self._scans_running -= 1
                self._to_check -= to_check
        if came_back and not self._stopping():
            self.reevaluate(library.id)  # its settings may have changed while it was away
        if self._stopping() or problems or not self._reachable(library):
            return
        for key, row in rows.items():
            if key in found or row.status == "missing" or self._owned(row):
                continue
            if self.db.get_file_by_path(row.path) is None:
                continue  # renamed during this scan, so its row now belongs to the new name
            # Confirm it is really gone, and that the owner has not just picked it up.
            if os.path.lexists(ext_path(row.path)):
                continue
            logger.info("No longer there: %s", row.path)
            self.db.set_missing_unless_owned(row.id)

    def scan_all(self) -> None:
        for library in self.db.list_libraries():
            if self._stopping():
                return
            try:
                self.scan_library(library.id)
            except Exception:
                logger.exception("Could not scan library %s", library.name)

    def check_pending(self) -> None:
        with self._lock:
            paths = sorted(self._pending)
        if not paths:
            return
        libraries = self.db.list_libraries()
        for path in paths:
            if self._stopping():
                return
            library = self._library_for(path, libraries)
            if library is None or not self._reachable(library):
                self._forget(path)
                continue
            work = self._work_root(library)
            if not self._is_candidate(path, work, library.path):
                self._forget(path)
                continue
            if self._process(library, path):
                with self._lock:
                    self._pending.discard(path)

    def reevaluate(self, library_id: int) -> None:
        """Decide again after a library's level, its track removal or the language changed."""
        library = next((l for l in self.db.list_libraries() if l.id == library_id), None)
        if library is None or not self._reachable(library):
            return
        for row in self.db.files_in_library(library.id):
            if row.status not in ("queued", "skipped", "converted"):
                continue
            if self._stopping():
                return
            if (row.status == "converted" and not library.english_only
                    and not library.remove_commentary):
                continue  # nothing can change for a converted file without track removal
            if not self._process(library, row.path, self.db.get_file(row.id), force=True):
                self._add_pending(row.path)

    @staticmethod
    def _library_for(path: str, libraries) -> Optional[Library]:
        best = None
        for library in libraries:
            if _is_inside(path, library.path):
                if best is None or len(library.path) > len(best.path):
                    best = library
        return best

    # ---- watching

    def _on_event(self, path) -> None:
        path = os.fspath(path)
        with self._lock:
            roots = [p for p, _ in self._watches.values()]
        root = next((r for r in roots if _is_inside(path, r)), None)
        if root and self._is_candidate(path, root=root):
            self._add_pending(path)

    def refresh_watches(self) -> None:
        """Start watching new libraries and stop watching removed ones."""
        observer = self._observer
        if observer is None:
            return
        wanted = {}
        for library in self.db.list_libraries():
            if self._reachable(library):
                wanted[_norm(library.path)] = os.path.normpath(library.path)
        with self._lock:
            for key in [k for k in self._watches if k not in wanted]:
                try:
                    observer.unschedule(self._watches.pop(key)[1])
                except Exception:
                    logger.exception("Could not stop watching %s", key)
            for key, path in wanted.items():
                if key in self._watches:
                    continue
                try:
                    self._watches[key] = (path, observer.schedule(
                        _Handler(self), path, recursive=True))
                except Exception:
                    logger.exception("Could not watch %s", path)

    def _check_roots(self) -> None:
        for library in self.db.list_libraries():
            reachable = self._reachable(library)
            if reachable and library.status == "offline":
                self.scan_library(library.id)  # sets ok, rewatches and rescans
            elif not reachable and library.status != "offline":
                self.scan_library(library.id)  # sets offline, leaves rows alone

    def start(self, stop_event: threading.Event) -> None:
        """Watch and scan until stop_event is set. Run this in its own thread."""
        from watchdog.observers import Observer
        self._stop = stop_event
        self._observer = Observer()
        self._observer.start()
        try:
            self.refresh_watches()
            next_full = self.clock()
            while not stop_event.is_set():
                try:
                    if self.clock() >= next_full:
                        self.scan_all()
                        next_full = self.clock() + self.full_interval
                    else:
                        self._check_roots()
                    self.refresh_watches()
                    self.check_pending()
                except Exception:
                    logger.exception("Scanner pass failed")
                stop_event.wait(self.pending_interval)
        finally:
            observer, self._observer = self._observer, None
            observer.stop()
            observer.join(timeout=5)
            with self._lock:
                self._watches.clear()


try:
    from watchdog.events import FileSystemEventHandler
except ImportError:  # pragma: no cover
    FileSystemEventHandler = object


class _Handler(FileSystemEventHandler):
    """Runs on watchdog's thread: it only notes paths and never touches the database."""

    def __init__(self, scanner: Scanner):
        self.scanner = scanner

    def on_any_event(self, event) -> None:
        if event.is_directory or event.event_type == "opened" or event.event_type == "closed_no_write":
            return
        self.scanner._on_event(event.src_path)
        dest = getattr(event, "dest_path", "")
        if dest:
            self.scanner._on_event(dest)
