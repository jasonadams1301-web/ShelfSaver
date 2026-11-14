"""Library scanning and watching. Every file lives under tmp_path."""
from __future__ import annotations

import os
import shutil
import threading
import time

import pytest

from shelfsaver import swap
from shelfsaver.config import Config
from shelfsaver.db import Database
from shelfsaver.models import IGNORED_REASON
from shelfsaver.scanner import Scanner
from tests.helpers import make_video

SIZE = "640x360"
UNREADABLE = "Couldn't read this file"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Env:
    def __init__(self, tmp_path, monkeypatch, stable=0.0):
        self.tmp_path = tmp_path
        self.volume = tmp_path / "vol"
        self.root = self.volume / "Movies"
        self.root.mkdir(parents=True)
        monkeypatch.setattr(swap, "_volume_root", lambda path: str(self.volume))
        self.db = Database(tmp_path / "data" / "test.db")
        self.lib = self.db.add_library("Movies", str(self.root), "high", False)
        self.clock = Clock()
        self.scanner = Scanner(self.db, Config(data_dir=tmp_path / "data"),
                               stable_seconds=stable, clock=self.clock)
        self.f = None

    def bloated(self, rel="big.mkv"):
        return make_video(self.root / rel, seconds=2, size=SIZE, crf=1)

    def efficient(self, rel="small.mkv"):
        return make_video(self.root / rel, seconds=2, size=SIZE, vcodec="libx265", crf=28)

    def row(self, path):
        return self.db.get_file_by_path(str(path))

    def reason(self, path):
        return self.row(path).reason

    def status(self, path):
        row = self.row(path)
        return row.status if row else None

    def remove_root(self):
        shutil.move(str(self.root), str(self.tmp_path / "gone"))

    def restore_root(self):
        shutil.move(str(self.tmp_path / "gone"), str(self.root))

    def library(self):
        return self.db.list_libraries()[0]


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    e.bloated("seed.mkv")
    e.scanner.scan_library(e.lib.id)
    e.f = e.row(e.root / "seed.mkv")
    return e


@pytest.fixture
def clock_env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch, stable=60.0)


def test_scan_queues_bloated_and_skips_x265(env):
    big, small = env.bloated(), env.efficient()
    env.scanner.scan_library(env.lib.id)
    assert env.status(big) == "queued"
    assert env.row(big).codec == "h264"
    assert env.status(small) == "skipped"
    assert env.reason(small) == "Already H.265"


def test_scan_ignores_work_root_when_library_is_a_drive_root(env, monkeypatch):
    # Defence: a whole-volume library must not pick up ShelfSaver's own folders.
    drive = env.tmp_path / "drive"
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(drive))
    lib = env.db.add_library("Drive", str(drive), "high", False)
    make_video(drive / "ShelfSaver" / "holding" / "Movies" / "old.mkv", seconds=2,
               size=SIZE, crf=1)
    make_video(drive / "ShelfSaver" / "incoming" / "new.mkv", seconds=2, size=SIZE, crf=1)
    keep = make_video(drive / "keep.mkv", seconds=2, size=SIZE, crf=1)
    env.scanner.scan_library(lib.id)
    paths = [f.path for f in env.db.files_in_library(lib.id)]
    assert paths == [str(keep)]


@pytest.mark.parametrize("rel", ["D/VIDEO_TS/VTS_01_1.VOB", "D/BDMV/STREAM/00001.m2ts",
                                 "a.mkv.partial", "b.mkv.part", "c.mkv.!qB",
                                 "D/VIDEO_TS/clip.mkv"])
def test_excluded_paths(env, rel):
    target = env.root / rel.replace("/", os.sep)
    if rel.endswith((".partial", ".part", ".!qB", ".VOB")):
        _write_junk(target)
    else:
        make_video(target, seconds=2, size=SIZE, crf=1)
    env.scanner.scan_library(env.lib.id)
    assert env.row(target) is None


def _write_junk(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 1000)


def test_growing_file_not_queued_until_stable(clock_env):
    e = clock_env
    target = e.bloated()
    e.scanner.scan_library(e.lib.id)
    assert e.row(target) is None
    e.clock.advance(30)
    e.scanner.scan_library(e.lib.id)
    assert e.row(target) is None
    with open(target, "ab") as grow:  # it changed, so the 60 seconds start again
        grow.write(b"\0" * 10)
    e.clock.advance(45)
    e.scanner.scan_library(e.lib.id)
    assert e.row(target) is None
    e.clock.advance(61)
    e.scanner.check_pending()
    assert e.status(target) == "queued"


def test_watcher_picks_up_dropped_file(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    e.scanner.pending_interval = 0.2
    stop = threading.Event()
    thread = threading.Thread(target=e.scanner.start, args=(stop,))
    thread.start()
    try:
        time.sleep(1.0)  # let the first scan and the watch start
        target = e.bloated("dropped.mkv")
        deadline = time.time() + 10
        while time.time() < deadline and e.status(target) is None:
            time.sleep(0.2)
        assert e.status(target) == "queued"
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_start_ends_promptly_when_stopped(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    stop = threading.Event()
    thread = threading.Thread(target=e.scanner.start, args=(stop,))
    thread.start()
    time.sleep(0.5)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_refresh_watches_follows_libraries(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    stop = threading.Event()
    thread = threading.Thread(target=e.scanner.start, args=(stop,))
    thread.start()
    try:
        time.sleep(0.5)
        other = tmp_path / "other"
        other.mkdir()
        lib2 = e.db.add_library("Other", str(other), "high", False)
        e.scanner.refresh_watches()
        assert len(e.scanner._watches) == 2
        e.db.remove_library(lib2.id)
        e.scanner.refresh_watches()
        assert len(e.scanner._watches) == 1
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_offline_root_marks_library_and_keeps_rows(env):
    env.remove_root()
    env.scanner.scan_library(env.lib.id)
    assert env.db.list_libraries()[0].status == "offline"
    assert env.db.get_file(env.f.id).status == "queued"


def test_root_returning_recovers(env):
    env.remove_root()
    env.scanner.scan_library(env.lib.id)
    env.restore_root()
    new = env.bloated("later.mkv")
    env.scanner.scan_library(env.lib.id)
    assert env.library().status == "ok"
    assert env.status(new) == "queued"
    assert env.db.get_file(env.f.id).status == "queued"


def test_deleted_file_is_missing_when_root_is_present(env):
    os.remove(env.f.path)
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "missing"


def test_missing_file_returning_is_queued_again(env):
    os.remove(env.f.path)
    env.scanner.scan_library(env.lib.id)
    env.bloated("seed.mkv")
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "queued"


@pytest.mark.parametrize("status", ["working", "waiting_in_use"])
def test_files_owned_by_the_worker_are_left_alone(env, status):
    env.db.set_file_status(env.f.id, status, "busy")
    os.remove(env.f.path)
    env.scanner.scan_library(env.lib.id)
    row = env.db.get_file(env.f.id)
    assert (row.status, row.reason) == (status, "busy")


def test_file_in_a_swap_is_left_alone(env):
    run = env.db.start_run(env.f.id, env.f.size, "H.264", None, "why")
    env.db.update_run(run.id, state="swapping")
    env.db.set_file_status(env.f.id, "failed", "swap kept")
    os.remove(env.f.path)
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "failed"
    make_video(env.f.path, seconds=3, size=SIZE, crf=1)  # changed, but still owned
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "failed"


def test_converted_file_is_not_requeued(env):
    path = env.root / "movie.mp4"  # a new path and extension, as after a conversion
    make_video(path, seconds=2, size=SIZE, crf=1)
    st = os.stat(path)
    env.db.record_converted(env.f.id, str(path), st.st_size, st.st_mtime, "hevc", "mp4",
                            SIZE)
    os.remove(env.f.path) if os.path.exists(env.f.path) else None
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "converted"
    # Even if the file looks bloated and changes, x265 is never converted again.
    make_video(path, seconds=3, size=SIZE, vcodec="libx265", crf=1)
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "converted"
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(env.f.id).status == "converted"


def test_older_format_copy_replacing_a_converted_file_is_queued(env):
    path = env.efficient("movie.mkv")
    st = os.stat(path)
    row = env.db.upsert_file(env.lib.id, str(path), st.st_size, st.st_mtime, "converted")
    env.bloated("movie.mkv")  # another app puts a new copy at the same path
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(row.id).status == "queued"


def test_unchanged_row_is_not_probed_again(env, monkeypatch):
    from shelfsaver import scanner as scanner_module
    calls = []
    monkeypatch.setattr(scanner_module, "probe", lambda *a: calls.append(a))
    env.scanner.scan_library(env.lib.id)
    assert calls == []


def test_level_change_reevaluates_skipped_not_converted(env):
    mid = env.root / "mid.mkv"
    make_video(mid, seconds=2, size=SIZE, crf=30)  # fairly small for its size
    env.scanner.scan_library(env.lib.id)
    assert env.status(mid) == "skipped"
    assert env.reason(mid) == "Already small"
    done = env.root / "done.mkv"
    make_video(done, seconds=2, size=SIZE, crf=30)
    st = os.stat(done)
    row = env.db.upsert_file(env.lib.id, str(done), st.st_size, st.st_mtime, "converted")
    env.db.update_library(env.lib.id, level="low")
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(row.id).status == "converted"


def ignored(env, path):
    st = os.stat(path)
    return env.db.upsert_file(env.lib.id, str(path), st.st_size, st.st_mtime, "skipped",
                              IGNORED_REASON)


def test_an_ignored_file_stays_ignored_through_a_scan_and_a_settings_change(env):
    row = ignored(env, env.bloated())
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(row.id).reason == IGNORED_REASON
    env.scanner.reevaluate(env.lib.id)
    after = env.db.get_file(row.id)
    assert after.status == "skipped" and after.reason == IGNORED_REASON


def test_a_new_copy_replacing_an_ignored_file_is_looked_at_again(env):
    path = env.efficient("movie.mkv")
    row = ignored(env, path)
    env.bloated("movie.mkv")
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(row.id).status == "queued"


def test_reevaluate_requeues_a_skipped_file_that_is_now_worth_converting(env):
    big = env.root / "x.mkv"
    make_video(big, seconds=2, size=SIZE, crf=1)
    st = os.stat(big)
    env.db.upsert_file(env.lib.id, str(big), st.st_size, st.st_mtime, "skipped",
                       "Already small")
    env.scanner.reevaluate(env.lib.id)
    assert env.status(big) == "queued"


def test_english_only_strips_converted_file_with_extra_audio(env):
    path = env.root / "multi.mkv"
    make_video(path, seconds=2, size=SIZE, vcodec="libx265", crf=28,
               audio=(("eng", "English"), ("spa", "Spanish")))
    st = os.stat(path)
    row = env.db.upsert_file(env.lib.id, str(path), st.st_size, st.st_mtime, "converted")
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(row.id).status == "converted"  # English-only is off
    env.db.update_library(env.lib.id, english_only=True)
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(row.id).status == "queued"


def test_the_scanner_keeps_the_default_language_from_settings(env):
    path = env.root / "multi.mkv"
    make_video(path, seconds=2, size=SIZE, vcodec="libx265", crf=28,
               audio=(("spa", "Spanish"), ("eng", "English")))
    env.db.update_library(env.lib.id, english_only=True)
    env.scanner.scan_library(env.lib.id)
    assert env.status(path) == "skipped"  # Spanish comes first, so English can't be kept alone
    env.db.set_setting("default_language", "spa")
    env.scanner.reevaluate(env.lib.id)
    assert env.status(path) == "queued"


def test_hardlinked_file_is_skipped(env):
    a = env.bloated("a.mkv")
    b = env.root / "b.mkv"
    os.link(a, b)
    env.scanner.scan_library(env.lib.id)
    assert env.reason(a) == "Linked to another copy"
    assert env.reason(b) == "Linked to another copy"


def test_unreadable_file_fails(env):
    bad = env.root / "bad.mkv"
    _write_junk(bad)
    env.scanner.scan_library(env.lib.id)
    row = env.row(bad)
    assert (row.status, row.reason) == ("failed", UNREADABLE)
    env.scanner.scan_library(env.lib.id)
    assert env.status(bad) == "failed"


@pytest.mark.skipif(os.name != "nt", reason="needs Windows exclusive file locking")
def test_locked_file_is_tried_again_not_failed(env):
    from tests.test_worker import exclusive_lock
    path = env.bloated("busy.mkv")
    with exclusive_lock(path):
        env.scanner.scan_library(env.lib.id)
        assert env.row(path) is None
    env.scanner.check_pending()
    assert env.status(path) == "queued"


def test_one_bad_file_does_not_stop_the_scan(env, monkeypatch):
    from shelfsaver import scanner as scanner_module
    real = scanner_module.probe

    def flaky(path, ffprobe):
        if "boom" in str(path):
            raise RuntimeError("unexpected")
        return real(path, ffprobe)

    monkeypatch.setattr(scanner_module, "probe", flaky)
    env.bloated("boom.mkv")
    ok = env.bloated("zzz.mkv")
    env.scanner.scan_library(env.lib.id)
    assert env.status(ok) == "queued"
    assert env.status(env.root / "boom.mkv") is None


def test_library_folder_named_like_a_disc_folder_is_still_scanned(tmp_path, monkeypatch):
    # VIDEO_TS and BDMV only exclude folders inside the library, not above it.
    e = Env(tmp_path, monkeypatch)
    inner = tmp_path / "BDMV" / "lib"
    inner.mkdir(parents=True)
    lib = e.db.add_library("Odd", str(inner), "high", False)
    clip = make_video(inner / "clip.mkv", seconds=2, size=SIZE, crf=1)
    e.scanner.scan_library(lib.id)
    assert e.status(clip) == "queued"


def test_settings_changed_while_offline_apply_when_it_returns(env):
    big = env.root / "x.mkv"
    make_video(big, seconds=2, size=SIZE, crf=1)
    st = os.stat(big)
    env.db.upsert_file(env.lib.id, str(big), st.st_size, st.st_mtime, "skipped", "Already small")
    env.remove_root()
    env.scanner.scan_library(env.lib.id)
    env.db.update_library(env.lib.id, level="low")
    env.scanner.reevaluate(env.lib.id)  # silently does nothing while offline
    assert env.status(big) == "skipped"
    env.restore_root()
    env.scanner.scan_library(env.lib.id)
    assert env.status(big) == "queued"


# ---- the wait for a file to settle, on a full scan

def age(path, seconds=3600):
    then = time.time() - seconds
    os.utime(path, (then, then))
    return path


def test_full_scan_does_not_wait_for_a_file_untouched_for_a_while(clock_env):
    big = age(clock_env.bloated())
    clock_env.scanner.scan_library(clock_env.lib.id)
    assert clock_env.status(big) == "queued"
    assert clock_env.scanner.status() == {"scanning": False, "pending": 0}


def test_full_scan_still_waits_for_a_recently_changed_file(clock_env):
    big = clock_env.bloated()
    clock_env.scanner.scan_library(clock_env.lib.id)
    assert clock_env.status(big) is None
    assert clock_env.scanner.status()["pending"] == 1
    clock_env.clock.advance(61)
    clock_env.scanner.check_pending()
    assert clock_env.status(big) == "queued"
    assert clock_env.scanner.status()["pending"] == 0


def test_dropped_file_with_an_old_time_still_waits(clock_env):
    big = age(clock_env.bloated())
    clock_env.scanner._add_pending(str(big))  # what a watcher event does
    clock_env.scanner.check_pending()
    assert clock_env.status(big) is None
    clock_env.scanner.scan_library(clock_env.lib.id)  # a full scan must not cut the wait short
    assert clock_env.status(big) is None
    clock_env.clock.advance(61)
    clock_env.scanner.check_pending()
    assert clock_env.status(big) == "queued"


def test_old_file_that_cant_be_read_waits_before_it_is_failed(clock_env):
    bad = clock_env.root / "x.mkv"
    bad.write_bytes(b"still being copied")
    age(bad)
    clock_env.scanner.scan_library(clock_env.lib.id)
    assert clock_env.status(bad) is None
    assert clock_env.scanner.status()["pending"] == 1
    clock_env.clock.advance(61)
    clock_env.scanner.check_pending()
    assert clock_env.status(bad) == "failed" and clock_env.reason(bad) == UNREADABLE


def test_status_counts_the_files_a_scan_still_has_to_check(clock_env, monkeypatch):
    import shelfsaver.scanner as scanner_module
    age(clock_env.bloated("a.mkv"))
    age(clock_env.efficient("b.mkv"))
    seen = []
    real_probe = scanner_module.probe

    def watching_probe(*args):
        seen.append(clock_env.scanner.status())
        return real_probe(*args)

    monkeypatch.setattr(scanner_module, "probe", watching_probe)
    clock_env.scanner.scan_library(clock_env.lib.id)
    assert seen == [{"scanning": True, "pending": 2}, {"scanning": True, "pending": 1}]
    assert clock_env.scanner.status() == {"scanning": False, "pending": 0}


def test_status_does_not_count_files_already_up_to_date(env, monkeypatch):
    seen = []
    real_consider = env.scanner._consider

    def watching(*args, **kwargs):
        seen.append(env.scanner.status())
        return real_consider(*args, **kwargs)

    monkeypatch.setattr(env.scanner, "_consider", watching)
    env.scanner.scan_library(env.lib.id)
    assert seen == [{"scanning": True, "pending": 0}]


def test_a_stopped_scan_leaves_nothing_counted(clock_env, monkeypatch):
    age(clock_env.bloated("a.mkv"))
    age(clock_env.bloated("b.mkv"))
    stop = threading.Event()
    clock_env.scanner._stop = stop
    real_consider = clock_env.scanner._consider

    def stop_after_one(*args, **kwargs):
        stop.set()
        return real_consider(*args, **kwargs)

    monkeypatch.setattr(clock_env.scanner, "_consider", stop_after_one)
    clock_env.scanner.scan_library(clock_env.lib.id)
    assert clock_env.scanner.status() == {"scanning": False, "pending": 0}


# ---- renamed files

def renamed(env, old, new="renamed.mkv"):
    target = env.root / new
    os.rename(old, target)
    return target


def scanned_file(env, status, reason=None):
    """A real file with a row in this state, as if it had been through the app."""
    path = env.efficient("known.mkv")
    st = os.stat(path)
    return path, env.db.upsert_file(env.lib.id, str(path), st.st_size, st.st_mtime, status, reason)


@pytest.mark.parametrize("status,reason", [
    ("converted", None),
    ("skipped", IGNORED_REASON),
    ("failed", "New file failed the playback check"),
    ("queued", None),
])
def test_a_rename_keeps_the_files_record(env, status, reason):
    path, row = scanned_file(env, status, reason)
    new = renamed(env, path)
    env.scanner.scan_library(env.lib.id)
    moved = env.db.get_file(row.id)
    assert (moved.path, moved.status, moved.reason) == (str(new), status, reason)
    assert env.row(path) is None


@pytest.mark.parametrize("status,reason", [
    ("skipped", IGNORED_REASON),
    ("failed", "New file failed the playback check"),
])
def test_a_file_already_marked_missing_gets_its_status_back(env, status, reason):
    path, row = scanned_file(env, status, reason)
    new = renamed(env, path)
    env.scanner._process(env.lib, str(path))  # the watcher can reach the old name first
    assert env.db.get_file(row.id).status == "missing"
    env.scanner._process(env.lib, str(new))
    moved = env.db.get_file(row.id)
    assert (moved.path, moved.status, moved.reason) == (str(new), status, reason)


def test_a_rename_found_by_a_full_scan_is_not_marked_missing_afterwards(env):
    new = renamed(env, env.f.path)
    env.scanner.scan_library(env.lib.id)
    moved = env.db.get_file(env.f.id)
    assert (moved.path, moved.status) == (str(new), "queued")


def test_a_recheck_working_from_the_old_name_does_not_mark_a_renamed_file_missing(env):
    path, row = scanned_file(env, "converted")
    new = renamed(env, path)
    env.scanner._process(env.lib, str(new))
    env.scanner._process(env.lib, str(path), env.db.get_file(row.id), force=True)
    assert env.db.get_file(row.id).status == "converted"


def test_a_file_missing_from_before_this_was_tracked_is_decided_again(env):
    env.db.set_file_status(env.f.id, "missing")
    new = renamed(env, env.f.path)
    env.scanner.scan_library(env.lib.id)
    moved = env.db.get_file(env.f.id)
    assert (moved.path, moved.status) == (str(new), "queued")


def test_a_rename_brings_the_files_history_along(env):
    path, row = scanned_file(env, "converted")
    run = env.db.start_run(row.id, 10, "H.264", None, "why")
    env.db.update_run(run.id, state="done", now_path=str(path))
    new = renamed(env, path)
    env.scanner.scan_library(env.lib.id)
    record = env.db.run_record(run.id)
    assert record["file_id"] == row.id
    assert (record["file_path"], record["now_path"]) == (str(new), str(new))


def test_changing_only_the_case_of_a_name_keeps_one_record(env):
    path, row = scanned_file(env, "failed", "Why")
    new = renamed(env, path, "KNOWN.mkv")
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(row.id).path == str(new)
    assert len(env.db.files_in_library(env.lib.id)) == 2  # this file and the seed


def test_a_copy_is_a_new_file_while_the_original_is_still_there(env):
    path, row = scanned_file(env, "failed", "Why")
    copy = env.root / "copy.mkv"
    shutil.copy2(path, copy)
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(row.id).path == str(path)
    assert env.row(copy).id != row.id


def test_two_possible_matches_mean_a_new_file(env):
    first, row1 = scanned_file(env, "failed", "One")
    st = os.stat(first)
    second = env.root / "second.mkv"
    shutil.copy2(first, second)
    row2 = env.db.upsert_file(env.lib.id, str(second), st.st_size, st.st_mtime, "failed", "Two")
    new = renamed(env, first)
    os.remove(second)
    env.scanner.scan_library(env.lib.id)
    assert env.row(new).id not in (row1.id, row2.id)


def test_a_changed_file_under_a_new_name_is_a_new_file(env):
    path, row = scanned_file(env, "failed", "Why")
    new = env.root / "renamed.mkv"
    os.rename(path, new)
    with open(new, "ab") as extra:
        extra.write(b"more")
    env.scanner.scan_library(env.lib.id)
    assert env.row(new).id != row.id
    assert env.db.get_file(row.id).status == "missing"


def test_a_file_the_worker_owns_is_not_moved(env):
    path, row = scanned_file(env, "working")
    new = renamed(env, path)
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(row.id).path == str(path)
    assert env.row(new).id != row.id


def test_a_match_in_another_library_does_not_count(env):
    other_root = env.volume / "TV"
    other_root.mkdir()
    other = env.db.add_library("TV", str(other_root), "high", False)
    path, row = scanned_file(env, "failed", "Why")
    new = other_root / "moved.mkv"
    os.rename(path, new)
    env.scanner.scan_library(other.id)
    assert env.row(new).id != row.id


def test_a_renamed_file_still_waits_to_settle(clock_env):
    env = clock_env
    path, row = scanned_file(env, "failed", "Why")
    new = renamed(env, path)
    assert env.scanner._process(env.lib, str(new)) is False
    assert env.db.get_file(row.id).path == str(path)
    env.clock.advance(61)
    assert env.scanner._process(env.lib, str(new)) is True
    assert env.db.get_file(row.id).path == str(new)


# ---- Shrinking oversized H.265 files

def bloated_h265(env, rel="fat.mkv"):
    return make_video(env.root / rel, seconds=2, size=SIZE, vcodec="libx265", crf=1)


def test_oversized_h265_is_queued_only_where_the_library_allows_it(env):
    fat = bloated_h265(env)
    env.scanner.scan_library(env.lib.id)
    assert (env.status(fat), env.reason(fat)) == ("skipped", "Already H.265")
    env.db.update_library(env.lib.id, shrink_h265=True)
    env.scanner.reevaluate(env.lib.id)
    assert env.status(fat) == "queued"
    env.db.update_library(env.lib.id, shrink_h265=False)
    env.scanner.reevaluate(env.lib.id)
    assert (env.status(fat), env.reason(fat)) == ("skipped", "Already H.265")


def test_lean_h265_stays_skipped_with_shrinking_on(env):
    env.db.update_library(env.lib.id, shrink_h265=True)
    lean = env.efficient()
    env.scanner.scan_library(env.lib.id)
    assert (env.status(lean), env.reason(lean)) == ("skipped", "Already H.265")


@pytest.mark.parametrize("reason", ["Sample showed too little saving", "Would not save enough"])
def test_h265_that_was_already_tried_is_not_queued_again(env, reason):
    env.db.update_library(env.lib.id, shrink_h265=True)
    fat = bloated_h265(env)
    st = os.stat(fat)
    env.db.upsert_file(env.lib.id, str(fat), st.st_size, st.st_mtime, "skipped", reason)
    env.scanner.reevaluate(env.lib.id)
    assert (env.status(fat), env.reason(fat)) == ("skipped", reason)


def test_h265_the_app_made_is_never_queued_again(env):
    env.db.update_library(env.lib.id, shrink_h265=True)
    fat = bloated_h265(env)
    st = os.stat(fat)
    row = env.db.upsert_file(env.lib.id, str(fat), st.st_size, st.st_mtime, "converted")
    env.scanner.reevaluate(env.lib.id)
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(row.id).status == "converted"


# ---- commentary soundtracks

COMMENTARY_AUDIO = (("eng", "English"), ("eng", "Director's Commentary"))


def test_commentary_is_left_alone_until_the_library_asks_for_it_to_go(env):
    path = env.root / "film.mkv"
    make_video(path, seconds=2, size=SIZE, vcodec="libx265", crf=28, audio=COMMENTARY_AUDIO)
    env.scanner.scan_library(env.lib.id)
    assert env.status(path) == "skipped"  # already H.265, and nothing is set to be removed


def test_a_commentary_track_makes_an_h265_file_worth_rewriting(env):
    path = env.root / "film.mkv"
    make_video(path, seconds=2, size=SIZE, vcodec="libx265", crf=28, audio=COMMENTARY_AUDIO)
    env.db.update_library(env.lib.id, remove_commentary=True)
    env.scanner.scan_library(env.lib.id)
    assert env.status(path) == "queued"


def test_switching_commentary_removal_on_rechecks_converted_files(env):
    path = env.root / "film.mkv"
    make_video(path, seconds=2, size=SIZE, vcodec="libx265", crf=28, audio=COMMENTARY_AUDIO)
    st = os.stat(path)
    row = env.db.upsert_file(env.lib.id, str(path), st.st_size, st.st_mtime, "converted")
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(row.id).status == "converted"  # both removal options are off
    env.db.update_library(env.lib.id, remove_commentary=True)
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(row.id).status == "queued"
