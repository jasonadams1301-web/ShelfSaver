"""The conversion worker. Every file stays under tmp_path, including the fake library volume."""
from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from shelfsaver import hostos, swap, worker as worker_module
from shelfsaver.config import Config
from shelfsaver.db import Database
from shelfsaver.encode import EncodeResult
from shelfsaver.models import VerifyResult
from shelfsaver.probe import probe
from shelfsaver.scanner import Scanner
from shelfsaver.swap import FileInUse, TargetExists, UnexpectedSwapState
from shelfsaver.worker import GB, IN_USE_RETRY_SECONDS, TARGET_TAKEN, WAITING_IN_USE, Worker
from tests.helpers import convert, eng_spa, info, make_unreadable_subtitle_video, make_video

# Windows refuses to open a file another program holds without sharing; Linux has no such
# lock, so a replace simply succeeds. Tests of that wait-and-retry path can only run on Windows.
windows_only = pytest.mark.skipif(not hostos.IS_WINDOWS, reason="Windows file locking")
if hostos.IS_WINDOWS:
    import msvcrt

SIZE = "640x360"
LOTS_OF_ROOM = 10_000 * GB
BREAKER_MESSAGE = "Paused: conversions keep failing. Check the graphics card and logs."


class SimulatedCrash(Exception):
    """Not an OSError, so nothing can mistake it for an ordinary disk failure."""


def temp_name(run, ext=".mkv"):
    """The temp file name of a run, from either kind of record the database returns."""
    fields = run if isinstance(run, dict) else vars(run)
    return worker_module._temp_stem(fields["id"], fields["started_at"]) + ext


def source_video(path, **kw):
    """Big enough for its resolution that `decide` always wants to convert it."""
    kw.setdefault("seconds", 2)
    kw.setdefault("size", SIZE)
    kw.setdefault("crf", 1)
    return make_video(path, **kw)


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.volume = tmp_path / "vol"
        self.lib = self.volume / "Media" / "Movies"
        self.lib.mkdir(parents=True)
        self.scratch = tmp_path / "scratch"
        self.scratch.mkdir()
        monkeypatch.setattr(swap, "_volume_root", lambda path: str(self.volume))

        self.db = Database(tmp_path / "data" / "test.db")
        self.db.set_setting("scratch_dir", str(self.scratch))
        self.library = self.db.add_library("Movies", str(self.lib), "high", False)
        self.config = Config(data_dir=tmp_path / "data")
        self.worker = Worker(self.db, self.config)

        self.free = {"scratch": LOTS_OF_ROOM, "library": LOTS_OF_ROOM}
        monkeypatch.setattr(worker_module, "free_bytes", self._free_bytes)
        self.encodes = 0
        self.encode_output = None  # kwargs for make_video; None means a good smaller file
        monkeypatch.setattr(worker_module, "run_ffmpeg", self._fake_encode)
        self.card_works = True
        self.card_checks = 0
        monkeypatch.setattr(worker_module, "encoder_works", self._check_card)
        self.ids = []
        self.lib_file = None
        self.original_bytes = None

    def _free_bytes(self, path):
        inside_scratch = Path(path).is_relative_to(self.scratch)
        return self.free["scratch" if inside_scratch else "library"]

    def _check_card(self, ffmpeg, encoder=None):
        self.card_checks += 1
        return self.card_works

    def _fake_encode(self, cmd, duration, on_progress, should_stop):
        self.encodes += 1
        kw = self.encode_output or dict(vcodec="libx265", crf=35, seconds=2, size=SIZE)
        make_video(Path(cmd[-1]), **kw)
        on_progress(1.0)
        return EncodeResult(0, False, "encoder said hello")

    def use_encode(self, fake):
        self.monkeypatch.setattr(worker_module, "run_ffmpeg", fake)

    def use_good_encode(self):
        self.use_encode(self._fake_encode)

    def fail_encodes(self):
        def failing(cmd, duration, on_progress, should_stop):
            self.encodes += 1
            return EncodeResult(1, False, "hardware says no")
        self.use_encode(failing)

    def queue(self, path, row_size=None):
        st = os.stat(path)
        row = self.db.upsert_file(self.library.id, str(path), row_size or st.st_size,
                                  st.st_mtime, "queued")
        self.ids.append(row.id)
        self.lib_file = Path(path)
        self.original_bytes = Path(path).read_bytes()
        return row

    def queue_video(self, name="movie.mkv", **kw):
        return self.queue(source_video(self.lib / name, **kw))

    def status(self, file_id=None):
        return self.db.get_file(file_id or self.ids[0]).status

    def file(self, file_id=None):
        return self.db.get_file(file_id or self.ids[0])

    def run_row(self, file_id=None):
        return self.db.file_details(file_id or self.ids[0])["runs"][0]

    def held(self, name):
        return self.volume / "ShelfSaver" / "holding" / "Movies" / name

    @property
    def incoming(self):
        return self.volume / "ShelfSaver" / "incoming"

    def incoming_files(self):
        return sorted(p.name for p in self.incoming.iterdir()) if self.incoming.exists() else []

    def scratch_runs(self):
        folder = self.scratch / "ShelfSaver"
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def restarted_worker(self):
        """A fresh worker, as after the app restarts."""
        self.worker = Worker(self.db, self.config)
        return self.worker

    def crash_before_swap(self):
        """Run a file up to the moment the swap would start, then die."""
        def crash(plan):
            raise SimulatedCrash("power cut")
        self.monkeypatch.setattr(worker_module, "execute_swap", crash)
        self.worker.process_one()
        self.monkeypatch.setattr(worker_module, "execute_swap", swap.execute_swap)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ---- A converted file changing later ----

def test_new_copy_at_a_converted_path_is_converted_and_both_originals_are_held(env):
    f = env.queue_video()
    env.worker.process_one()
    assert env.status() == "converted"
    first_original = env.original_bytes

    replacement = source_video(env.lib / "movie.mkv", seconds=3)
    replacement_bytes = replacement.read_bytes()
    env.encode_output = dict(vcodec="libx265", crf=35, seconds=3, size=SIZE)
    Scanner(env.db, env.config, stable_seconds=0).scan_library(env.library.id)
    assert env.status() == "queued"

    env.worker.process_one()
    assert env.status() == "converted"
    assert probe(env.file().path, "ffprobe").video_codec == "hevc"
    assert env.held("movie.mkv").read_bytes() == first_original
    assert env.held("movie (1).mkv").read_bytes() == replacement_bytes
    assert len(env.db.file_details(f.id)["runs"]) == 2


def test_original_put_back_from_holding_is_not_converted_again(env):
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "converted"

    shutil.copy2(env.held("movie.mkv"), env.lib / "movie.mkv")
    Scanner(env.db, env.config, stable_seconds=0).scan_library(env.library.id)
    assert env.status() == "converted"
    env.worker.process_one()
    assert env.encodes == 1


# ---- End to end on the real graphics card ----

def real_encoder(monkeypatch):
    from shelfsaver.encode import run_ffmpeg
    monkeypatch.setattr(worker_module, "run_ffmpeg", run_ffmpeg)


@pytest.mark.gpu
def test_end_to_end_converts_and_holds_original(env, monkeypatch):
    real_encoder(monkeypatch)
    f = env.queue(make_video(env.lib / "movie [2019] 'ü'.mkv"))
    env.worker.process_one()
    row = env.db.get_file(f.id)
    assert row.status == "converted" and probe(row.path, "ffprobe").video_codec == "hevc"
    assert env.held("movie [2019] 'ü'.mkv").read_bytes() == env.original_bytes
    assert env.db.stats()["saved_bytes"] > 0
    assert env.scratch_runs() == [] and env.incoming_files() == []


@pytest.mark.gpu
def test_end_to_end_strip_only_removes_spanish(env, monkeypatch):
    real_encoder(monkeypatch)
    env.db.update_library(env.library.id, english_only=True)
    f = env.queue(make_video(env.lib / "two.mkv", seconds=2, size="320x240", vcodec="libx265",
                             crf=40, audio=(("eng", "English"), ("spa", "Spanish"))))
    env.worker.process_one()
    row = env.db.get_file(f.id)
    assert row.status == "converted"
    info = probe(row.path, "ffprobe")
    assert [s.language for s in info.streams if s.kind == "audio"] == ["eng"]
    tracks = json.loads(env.run_row()["tracks_json"])
    assert [t["language"] for t in tracks["removed"]] == ["spa"]
    assert "eng" in [t["language"] for t in tracks["kept"]]


def test_the_worker_keeps_the_default_language_from_settings(env):
    env.db.update_library(env.library.id, english_only=True)
    env.db.set_setting("default_language", "spa")
    env.encode_output = dict(vcodec="libx265", crf=40, seconds=2, size=SIZE,
                             audio=(("spa", "Spanish"),))
    f = env.queue(make_video(env.lib / "two.mkv", seconds=2, size=SIZE, vcodec="libx265", crf=20,
                             audio=(("spa", "Spanish"), ("eng", "English"))))
    env.worker.process_one()
    assert env.db.get_file(f.id).status == "converted"
    tracks = json.loads(env.run_row()["tracks_json"])
    assert [t["language"] for t in tracks["removed"]] == ["eng"]


@pytest.mark.gpu
def test_end_to_end_avi_becomes_mkv(env, monkeypatch):
    real_encoder(monkeypatch)
    f = env.queue(make_video(env.lib / "old.avi", seconds=2))
    env.worker.process_one()
    row = env.db.get_file(f.id)
    assert row.status == "converted", row.reason
    assert row.path == str(env.lib / "old.mkv") and Path(row.path).exists()
    assert not (env.lib / "old.avi").exists()
    assert env.held("old.avi").read_bytes() == env.original_bytes
    assert row.size == Path(row.path).stat().st_size
    assert row.mtime == Path(row.path).stat().st_mtime


# ---- The pipeline with a fake encoder and real files ----

def test_success_records_the_outcome(env):
    f = env.queue_video()
    assert env.worker.process_one() is True
    row, run = env.file(), env.run_row()
    new_size = Path(row.path).stat().st_size
    assert row.status == "converted" and row.reason is None
    assert (row.size, row.mtime) == (new_size, Path(row.path).stat().st_mtime)
    assert row.codec == "hevc"
    assert run["state"] == "done" and run["finished_at"]
    assert run["was_size"] == len(env.original_bytes) and run["now_size"] == new_size
    assert run["saved_bytes"] == run["was_size"] - run["now_size"] > 0
    assert (run["was_format"], run["now_format"]) == ("H.264", "H.265")
    assert run["now_path"] == row.path
    assert json.loads(run["checks_json"]) == {
        "length": True, "tracks": True, "smaller": True, "plays": True}
    assert [t["kind"] for t in json.loads(run["tracks_json"])["kept"]] == ["video", "audio"]
    assert env.held("movie.mkv").read_bytes() == env.original_bytes
    assert env.scratch_runs() == [] and env.incoming_files() == []
    assert env.worker.status()["state"] == "idle"
    assert f.id == row.id


def test_nothing_queued_returns_false_and_is_idle(env):
    assert env.worker.process_one() is False
    assert env.worker.status() == {"state": "idle", "file": None, "step": None, "percent": None,
                                   "eta_seconds": None, "message": None, "blackout_until": None}


def test_holding_off_replaces_without_keeping_a_copy(env):
    env.db.set_setting("holding_enabled", "0")
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "converted"
    assert not env.held("movie.mkv").exists()
    assert probe(env.lib_file, "ffprobe").video_codec == "hevc"


def test_decide_skip_records_reason_and_moves_on(env):
    f = env.queue(make_video(env.lib / "x265.mkv", seconds=2, size=SIZE, vcodec="libx265", crf=35))
    assert env.worker.process_one() is True
    row = env.db.get_file(f.id)
    assert (row.status, row.reason) == ("skipped", "Already H.265")
    assert env.encodes == 0
    assert env.db.file_details(f.id)["runs"] == []
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.worker.status()["state"] == "idle"


def test_ffmpeg_gets_only_the_fixed_scratch_names(env):
    seen = []

    def spy(cmd, duration, on_progress, should_stop):
        seen.append(list(cmd))
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    env.use_encode(spy)
    f = env.queue_video(name="odd [name] 'ü' & more.mkv")
    env.worker.process_one()
    run_folder = env.scratch / "ShelfSaver" / f"run-{env.run_row(f.id)['id']}"
    assert str(run_folder / "source.mkv") in seen[0]
    assert seen[0][-1] == str(run_folder / "output.mkv")
    assert not any("odd" in part for part in seen[0])


def test_verify_failure_keeps_original_and_records_reason(env):
    env.encode_output = dict(vcodec="libx265", crf=35, seconds=8, size=SIZE)
    env.queue_video()
    env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert (row.status, row.reason) == ("failed", "New file is a different length")
    assert env.lib_file.read_bytes() == env.original_bytes
    assert run["state"] == "failed" and run["finished_at"]
    assert json.loads(run["checks_json"])["length"] is False
    assert "encoder said hello" in run["log_tail"]
    assert env.scratch_runs() == [] and env.incoming_files() == []


MONDAY_NIGHT = datetime(2026, 11, 2, 19, 0)  # a Monday
EVENINGS = json.dumps([{"days": [0, 1, 2, 3, 4, 5, 6], "start": "18:00", "end": "23:00"}])


def _in_blackout(env, now=MONDAY_NIGHT):
    env.monkeypatch.setattr(worker_module, "_local_now", lambda: now)
    env.db.set_setting("blackouts", EVENINGS)


def test_no_new_file_starts_during_a_blackout(env):
    _in_blackout(env)
    f = env.queue_video()
    assert env.worker.process_one() is False
    assert env.encodes == 0 and env.db.get_file(f.id).status == "queued"
    state = env.worker.status()
    assert state["state"] == "blackout" and state["blackout_until"] == "2026-11-02T23:00"


def test_the_queue_carries_on_when_a_blackout_ends(env):
    _in_blackout(env)
    f = env.queue_video()
    env.worker.process_one()
    env.monkeypatch.setattr(worker_module, "_local_now", lambda: datetime(2026, 11, 2, 23, 0))
    assert env.worker.process_one() is True
    assert env.db.get_file(f.id).status == "converted"
    assert env.worker.status()["blackout_until"] is None


def test_a_file_already_converting_finishes_when_a_blackout_starts(env):
    env.monkeypatch.setattr(worker_module, "_local_now", lambda: MONDAY_NIGHT)
    first = env.queue_video()

    def blackout_arrives(cmd, duration, on_progress, should_stop):
        env.db.set_setting("blackouts", EVENINGS)
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    env.use_encode(blackout_arrives)
    env.worker.process_one()
    assert env.db.get_file(first.id).status == "converted"
    second = env.queue_video(name="second.mkv")
    assert env.worker.process_one() is False
    assert env.db.get_file(second.id).status == "queued"


def test_finished_conversions_can_still_be_swapped_in_during_a_blackout(env):
    _in_blackout(env)
    swaps = []
    env.monkeypatch.setattr(env.worker, "_advance_due_swaps", lambda: swaps.append(1) or True)
    assert env.worker.process_one() is True and swaps == [1]


def test_pausing_during_a_blackout_shows_as_paused_and_ending_it_does_not_resume(env):
    _in_blackout(env)
    f = env.queue_video()
    env.worker.pause()
    assert env.worker.process_one() is False
    assert env.worker.status()["state"] == "paused"
    env.monkeypatch.setattr(worker_module, "_local_now", lambda: datetime(2026, 11, 2, 23, 0))
    assert env.worker.process_one() is False
    assert env.db.get_file(f.id).status == "queued" and env.encodes == 0


def test_resuming_during_a_blackout_does_not_override_it(env):
    _in_blackout(env)
    f = env.queue_video()
    env.worker.pause()
    env.worker.resume()
    assert env.worker.process_one() is False
    assert env.db.get_file(f.id).status == "queued"
    assert env.worker.status()["state"] == "blackout"


def test_playback_errors_are_kept_after_the_encoder_log(env):
    checks = {"length": True, "tracks": True, "smaller": True, "plays": False}
    env.monkeypatch.setattr(worker_module, "verify_output", lambda *a, **k: VerifyResult(
        False, checks, "New file failed the playback check", log="Invalid data at 01:02:03"))
    env.queue_video()
    env.worker.process_one()
    log = env.run_row()["log_tail"]
    assert env.file().status == "failed"
    assert log.index("encoder said hello") < log.index("Invalid data at 01:02:03")


def test_not_worth_it_is_skipped_not_failed(env):
    # A lightly compressed source, and a "converted" file that came out far bigger.
    env.encode_output = dict(vcodec="libx265", crf=1, seconds=2, size=SIZE)
    env.queue_video(crf=20)
    env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert (row.status, row.reason) == ("skipped", "Would not save enough")
    assert run["state"] == "not_worth_it"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.scratch_runs() == []


def test_original_changed_during_convert_is_requeued_not_overwritten(env):
    def changes_original(cmd, duration, on_progress, should_stop):
        env.lib_file.write_bytes(b"newer")
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    env.use_encode(changes_original)
    env.queue_video()
    env.worker.process_one()
    assert env.lib_file.read_bytes() == b"newer" and env.status() == "queued"
    assert env.scratch_runs() == [] and env.incoming_files() == []
    assert env.run_row()["state"] == "interrupted"


def test_low_scratch_space_blocks_without_failing(env):
    f = env.queue_video()
    env.free["scratch"] = 2 * f.size + 5 * GB - 1
    assert env.worker.process_one() is False
    status = env.worker.status()
    assert status["state"] == "blocked"
    assert status["message"] == "Not enough room on the scratch disk"
    assert env.status() == "queued" and env.encodes == 0
    assert env.scratch_runs() == []

    env.free["scratch"] = 2 * f.size + 5 * GB
    assert env.worker.process_one() is True
    assert env.status() == "converted"
    assert env.worker.status()["state"] == "idle"


def test_scratch_disk_that_stops_answering_means_waiting_not_failing(env, monkeypatch):
    def unplugged(path):
        raise OSError("drive not ready")

    monkeypatch.setattr(worker_module, "free_bytes", unplugged)
    env.queue_video()
    assert env.worker.process_one() is False
    assert env.worker.status()["state"] == "no_scratch"
    assert env.status() == "queued"


def test_low_library_space_blocks_and_deletes_nothing(env):
    env.queue_video()
    env.free["library"] = 5 * GB
    assert env.worker.process_one() is False
    status = env.worker.status()
    assert status["state"] == "blocked"
    assert status["message"] == "Not enough room on the library drive"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.incoming_files() == []
    run = env.run_row()
    assert (env.scratch / "ShelfSaver" / f"run-{run['id']}" / "output.mkv").exists()
    assert env.status() != "failed"

    # Still no room: nothing changes, and no second conversion is started.
    assert env.worker.process_one() is False
    assert env.encodes == 1

    env.free["library"] = 6 * GB
    assert env.worker.process_one() is True
    assert env.status() == "converted" and env.encodes == 1
    assert env.worker.status()["state"] == "idle"


def test_sha_mismatch_fails_and_keeps_original(env, monkeypatch):
    real_hash = worker_module._sha256
    calls = []

    def damaged(path, should_stop):
        calls.append(path)
        return "damaged" if len(calls) == 2 else real_hash(path, should_stop)

    monkeypatch.setattr(worker_module, "_sha256", damaged)
    env.queue_video()
    env.worker.process_one()
    row = env.file()
    assert row.status == "failed" and "damaged" in row.reason
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.incoming_files() == [] and env.scratch_runs() == []
    assert not env.held("movie.mkv").exists()


def test_library_at_a_volume_root_fails_before_any_work(env):
    drive_root = env.db.add_library("Whole", str(env.volume), "high", False)
    path = source_video(env.volume / "at-root.mkv")
    st = path.stat()
    f = env.db.upsert_file(drive_root.id, str(path), st.st_size, st.st_mtime, "queued")
    assert env.worker.process_one() is True
    row = env.db.get_file(f.id)
    assert row.status == "failed" and "whole drive" in row.reason
    assert env.encodes == 0 and env.db.file_details(f.id)["runs"] == []


def test_make_plan_refusal_fails_the_run_and_cleans_up(env, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValueError("not allowed")

    monkeypatch.setattr(worker_module, "make_plan", refuse)
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "failed"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.incoming_files() == [] and env.scratch_runs() == []


def test_missing_file_becomes_missing_when_the_library_is_there(env):
    f = env.queue_video()
    env.lib_file.unlink()
    assert env.worker.process_one() is True
    assert env.status(f.id) == "missing"


def test_a_file_the_worker_finds_gone_can_still_be_followed_to_its_new_name(env):
    f = env.queue_video()
    env.lib_file.unlink()
    env.worker.process_one()
    assert env.db.follow_rename(f.id, str(env.lib / "new name.mkv")).status == "queued"


def test_unreadable_file_fails_with_a_plain_reason(env):
    path = env.lib / "broken.mkv"
    path.write_bytes(b"not a video")
    f = env.queue(path)
    assert env.worker.process_one() is True
    row = env.db.get_file(f.id)
    assert (row.status, row.reason) == ("failed", "Couldn't read this file")
    assert path.read_bytes() == b"not a video"


def test_offline_library_is_left_alone_and_other_libraries_continue(env):
    other_root = env.volume / "Media" / "Gone"
    other_root.mkdir()
    gone = env.db.add_library("Gone", str(other_root), "high", False)
    path = source_video(other_root / "away.mkv")
    st = path.stat()
    away = env.db.upsert_file(gone.id, str(path), st.st_size * 100, st.st_mtime, "queued")
    shutil.rmtree(other_root)

    f = env.queue_video()
    assert env.worker.process_one() is True
    assert env.status(f.id) == "converted"
    assert env.db.get_file(away.id).status == "queued"
    assert env.worker.process_one() is False
    assert env.db.get_file(away.id).status == "queued"


# ---- Ignored files ----

def test_try_again_brings_back_an_ignored_file_but_not_another_skip(env):
    row = env.queue_video()
    env.db.set_file_status(row.id, "skipped", "Ignored, left as is")
    env.worker.retry(row.id)
    assert env.status(row.id) == "queued"
    env.db.set_file_status(row.id, "skipped", "Already H.265")
    env.worker.retry(row.id)
    assert env.status(row.id) == "skipped"


# ---- The three-failure breaker ----

def test_three_failures_with_a_working_card_fail_only_those_files(env):
    for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv"):
        env.queue_video(name=name)
    env.fail_encodes()
    [env.worker.process_one() for _ in range(3)]
    assert env.worker.status()["state"] == "idle"
    assert env.card_checks == 1
    assert [env.db.get_file(i).status for i in env.ids] == ["failed", "failed", "failed", "queued"]
    env.use_good_encode()
    assert env.worker.process_one() is True
    assert env.db.get_file(env.ids[3]).status == "converted"


def test_three_more_failures_check_the_card_again(env):
    for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv", "e.mkv", "f.mkv"):
        env.queue_video(name=name)
    env.fail_encodes()
    [env.worker.process_one() for _ in range(5)]
    assert env.card_checks == 1
    env.worker.process_one()
    assert env.card_checks == 2


def test_unreadable_subtitles_skip_a_queued_file_without_touching_it(env):
    path = make_unreadable_subtitle_video(env.lib / "odd.mkv", seconds=2, size=SIZE)
    original = path.read_bytes()
    row = env.queue(path)
    env.worker.process_one()
    after = env.db.get_file(row.id)
    assert after.status == "skipped" and after.reason == "Unreadable subtitles, left as is"
    assert env.encodes == 0 and path.read_bytes() == original
    assert env.worker.status()["state"] == "idle"


def test_three_encoder_failures_pause_and_requeue(env):
    env.card_works = False
    for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv"):
        env.queue_video(name=name)
    env.fail_encodes()
    [env.worker.process_one() for _ in range(3)]
    status = env.worker.status()
    assert status["state"] == "paused" and status["message"] == BREAKER_MESSAGE
    assert all(env.db.get_file(i).status == "queued" for i in env.ids)
    assert env.encodes == 3
    assert env.worker.process_one() is False and env.encodes == 3
    assert env.scratch_runs() == []
    assert any("hardware says no" in r["log_tail"] for r in env.db.history())


def test_clean_exit_with_an_unreadable_result_counts_as_an_encoder_failure(env):
    def garbage(cmd, duration, on_progress, should_stop):
        Path(cmd[-1]).write_bytes(b"not a video")
        return EncodeResult(0, False, "looked fine")

    env.card_works = False
    env.use_encode(garbage)
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        env.queue_video(name=name)
    for _ in range(3):
        env.worker.process_one()
    assert env.worker.status()["state"] == "paused"
    assert env.worker.status()["message"] == BREAKER_MESSAGE
    assert all(env.db.get_file(i).status == "queued" for i in env.ids)


def test_unreadable_result_is_not_logged_as_a_bad_exit(env, caplog):
    def garbage(cmd, duration, on_progress, should_stop):
        Path(cmd[-1]).write_bytes(b"not a video")
        return EncodeResult(0, False, "looked fine")

    env.use_encode(garbage)
    env.queue_video()
    with caplog.at_level(logging.ERROR, logger="shelfsaver.worker"):
        env.worker.process_one()
    messages = [r.getMessage() for r in caplog.records]
    assert any("can't be read" in m and "looked fine" in m for m in messages)
    assert not any("exited with" in m for m in messages)


def test_an_encoder_failure_alone_fails_just_that_file(env):
    env.queue_video()
    env.fail_encodes()
    env.worker.process_one()
    row = env.file()
    assert row.status == "failed" and row.reason
    assert "hardware" not in row.reason
    assert env.worker.status()["state"] == "idle"
    assert env.lib_file.read_bytes() == env.original_bytes


def test_a_successful_encode_resets_the_breaker(env):
    for name in ("a.mkv", "b.mkv", "good.mkv", "c.mkv", "d.mkv"):
        env.queue_video(name=name)
    env.fail_encodes()
    env.worker.process_one()
    env.worker.process_one()
    env.use_good_encode()
    env.worker.process_one()
    env.fail_encodes()
    env.worker.process_one()
    env.worker.process_one()
    assert env.worker.status()["state"] == "idle"


def test_bad_file_failures_do_not_trip_the_breaker(env):
    env.encode_output = dict(vcodec="libx265", crf=35, seconds=8, size=SIZE)
    for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv"):
        env.queue_video(name=name)
    for _ in range(4):
        env.worker.process_one()
    assert env.worker.status()["state"] == "idle"
    assert all(env.db.get_file(i).status == "failed" for i in env.ids)


def test_a_stop_is_not_an_encoder_failure(env):
    def stopped(cmd, duration, on_progress, should_stop):
        return EncodeResult(255, True, "")

    env.use_encode(stopped)
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        env.queue_video(name=name)
    for _ in range(3):
        env.worker.process_one()
    assert env.worker.status()["state"] == "idle"
    assert env.db.get_file(env.ids[0]).status == "queued"


def test_ffmpeg_that_will_not_start_counts_as_an_encoder_failure(env):
    def missing(cmd, duration, on_progress, should_stop):
        raise FileNotFoundError("ffmpeg")

    env.card_works = False  # the card check needs FFmpeg too
    env.use_encode(missing)
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        env.queue_video(name=name)
    for _ in range(3):
        env.worker.process_one()
    assert env.worker.status()["state"] == "paused"


def test_resume_gives_the_breaker_a_fresh_count(env):
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        env.queue_video(name=name)
    env.fail_encodes()
    for _ in range(3):
        env.worker.process_one()
    env.worker.resume()
    assert env.worker.status()["state"] == "idle"
    assert env.worker.status()["message"] is None
    env.worker.process_one()
    assert env.worker.status()["state"] == "idle"


# ---- A file in use ----

@contextmanager
def in_use(env, path):
    """While active, the swap behaves as if another program has `path` open.

    The worker's wait-and-retry handling is the same on every OS; only how a file ends up
    "in use" differs. On Windows a plain open() really blocks the swap's rename, so that is
    used (it is the most faithful). Linux has no such lock: a rename of an open file just
    works. There the swap functions raise FileInUse, the error swap.py raises on Windows when
    a rename hits a PermissionError, so the worker's response to it is still exercised.
    """
    if hostos.IS_WINDOWS:
        with open(path, "rb"):
            yield
        return

    mine = os.path.normcase(os.path.abspath(path))

    def only_for_this_file(real):
        # A real lock affects just the one file, so every other swap must carry on normally.
        def guarded(plan):
            if os.path.normcase(os.path.abspath(plan.original)) == mine:
                raise FileInUse("another program has the file open")
            return real(plan)
        return guarded

    with env.monkeypatch.context() as patch:
        patch.setattr(worker_module, "execute_swap", only_for_this_file(worker_module.execute_swap))
        patch.setattr(worker_module, "recover_swap", only_for_this_file(worker_module.recover_swap))
        yield

def test_in_use_waits_then_finishes_on_retry(env, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(worker_module, "_now", lambda: clock.now)
    f = env.queue_video()
    # Python opens files without delete sharing, so a real rename fails while this is open.
    with in_use(env, env.lib_file):
        env.worker.process_one()
        row, run = env.file(), env.run_row()
        assert (row.status, row.reason) == ("waiting_in_use", "Waiting, file is in use")
        assert run["state"] == "waiting_in_use"
        assert env.incoming_files() == [temp_name(run)]
        assert env.scratch_runs() == []
        assert env.lib_file.read_bytes() == env.original_bytes

        clock.now += IN_USE_RETRY_SECONDS
        env.worker.process_one()
        assert env.status() == "waiting_in_use"

    clock.now += IN_USE_RETRY_SECONDS - 1
    assert env.worker.process_one() is False
    assert env.status() == "waiting_in_use"
    clock.now += 1
    assert env.worker.process_one() is True
    assert env.status(f.id) == "converted"
    assert env.run_row()["state"] == "done"
    assert env.incoming_files() == []
    assert env.encodes == 1
    assert IN_USE_RETRY_SECONDS == 600


def test_waiting_clears_an_earlier_error(env, monkeypatch):
    def trouble(plan):
        raise OSError("disk trouble")

    monkeypatch.setattr(worker_module, "execute_swap", trouble)
    f = env.queue_video()
    env.worker.process_one()
    assert env.run_row()["error"]
    monkeypatch.setattr(worker_module, "execute_swap", swap.execute_swap)
    with in_use(env, env.lib_file):
        env.worker.retry(f.id)
        env.worker.process_one()
    assert env.run_row()["state"] == "waiting_in_use" and env.run_row()["error"] is None


def test_owner_retry_tries_a_waiting_file_right_away(env):
    f = env.queue_video()
    with in_use(env, env.lib_file):
        env.worker.process_one()
    env.worker.retry(f.id)
    assert env.status() == "waiting_in_use"  # the worker thread does the swap, not the caller
    assert env.worker.process_one() is True
    assert env.status() == "converted"


def test_other_work_continues_while_a_file_is_in_use(env):
    busy = env.queue_video(name="busy.mkv")
    other = env.queue(source_video(env.lib / "other.mkv"), row_size=1)
    with in_use(env, env.lib / "busy.mkv"):
        env.worker.process_one()
        assert env.status(busy.id) == "waiting_in_use"
        env.worker.process_one()
        assert env.status(other.id) == "converted"
        assert env.status(busy.id) == "waiting_in_use"


def test_original_changed_while_waiting_is_requeued_and_temp_dropped(env):
    f = env.queue_video()
    with in_use(env, env.lib_file):
        env.worker.process_one()
    env.lib_file.write_bytes(b"newer")
    env.worker.retry(f.id)
    env.worker.process_one()
    assert env.lib_file.read_bytes() == b"newer"
    assert env.incoming_files() == []
    assert env.run_row()["state"] == "interrupted"
    # Requeued in the same pass, which then finds the new content isn't a video.
    assert env.file().reason == "Couldn't read this file"


@contextmanager
def exclusive_lock(path):
    """Hold a real handle that shares nothing, as an editor or a copy tool can."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, no_sharing, open_existing, normal = 0x80000000, 0, 3, 0x80
    handle = kernel32.CreateFileW(str(path), generic_read, no_sharing, None, open_existing,
                                  normal, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


@windows_only
def test_locked_original_waits_and_other_files_keep_going(env, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(worker_module, "_now", lambda: clock.now)
    locked = env.queue_video(name="locked.mkv")
    other = env.queue(source_video(env.lib / "other.mkv"), row_size=1)
    with exclusive_lock(env.lib / "locked.mkv"):
        assert env.worker.process_one() is True
        row = env.db.get_file(locked.id)
        assert (row.status, row.reason) == ("waiting_in_use", WAITING_IN_USE)
        assert env.db.file_details(locked.id)["runs"] == []

        env.worker.process_one()
        assert env.status(other.id) == "converted"
        assert env.status(locked.id) == "waiting_in_use"

        # Not due: nothing is tried, so this can't become a tight loop.
        clock.now += IN_USE_RETRY_SECONDS - 1
        assert env.worker.process_one() is False
        assert env.status(locked.id) == "waiting_in_use"

        clock.now += 1
        assert env.worker.process_one() is True  # due, and still locked
        assert env.status(locked.id) == "waiting_in_use"

    clock.now += IN_USE_RETRY_SECONDS
    assert env.worker.process_one() is True
    assert env.status(locked.id) == "converted"


@windows_only
def test_original_locked_just_before_the_copy_waits_and_cleans_up(env, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(worker_module, "_now", lambda: clock.now)
    real_copy = worker_module._copy_file
    held = []

    def lock_then_copy(source, target, on_fraction, should_stop):
        if not held:
            held.append(exclusive_lock(source))
            held[0].__enter__()
        return real_copy(source, target, on_fraction, should_stop)

    monkeypatch.setattr(worker_module, "_copy_file", lock_then_copy)
    f = env.queue_video()
    env.worker.process_one()
    held[0].__exit__(None, None, None)
    row, run = env.file(), env.run_row()
    assert (row.status, row.reason) == ("waiting_in_use", WAITING_IN_USE)
    assert run["state"] == "interrupted"
    assert env.encodes == 0 and env.scratch_runs() == []
    assert env.worker.status()["state"] == "idle"

    assert env.worker.process_one() is False  # not due yet
    clock.now += IN_USE_RETRY_SECONDS
    assert env.worker.process_one() is True
    assert env.status(f.id) == "converted"


@windows_only
def test_owner_retry_tries_a_locked_file_right_away(env):
    f = env.queue_video()
    with exclusive_lock(env.lib_file):
        env.worker.process_one()
    assert env.status() == "waiting_in_use"
    env.worker.retry(f.id)
    assert env.worker.process_one() is True
    assert env.status() == "converted"


@windows_only
def test_locked_file_is_tried_after_a_restart(env):
    f = env.queue_video()
    with exclusive_lock(env.lib_file):
        env.worker.process_one()
    worker = env.restarted_worker()
    worker.recover()
    assert worker.process_one() is True
    assert env.status(f.id) == "converted"


def test_requeued_file_with_a_swap_pending_never_starts_a_second_run(env):
    f = env.queue_video()
    with in_use(env, env.lib_file):
        env.worker.process_one()
        env.db.requeue([f.id])  # what a rescan could do
        env.worker.process_one()
        assert env.status() == "waiting_in_use"
    assert env.encodes == 1
    assert len(env.db.file_details(f.id)["runs"]) == 1


# ---- Swap failures: temp can be the only copy ----

@pytest.mark.parametrize("error", [UnexpectedSwapState("no match"), FileNotFoundError("gone"),
                                   OSError("disk trouble")])
def test_swap_trouble_keeps_temp_and_can_finish_on_retry(env, monkeypatch, caplog, error):
    def trouble(plan):
        raise error

    monkeypatch.setattr(worker_module, "execute_swap", trouble)
    f = env.queue_video()
    with caplog.at_level(logging.ERROR, logger="shelfsaver.worker"):
        env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert run["state"] == "swapping"
    assert env.incoming_files() == [temp_name(run)]
    assert row.status == "failed" and row.reason and "no match" not in row.reason
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert env.lib_file.read_bytes() == env.original_bytes

    # The queue carries on, and the owner's Retry finishes the same run without converting again.
    monkeypatch.setattr(worker_module, "execute_swap", swap.execute_swap)
    assert env.worker.process_one() is False
    assert env.run_row()["state"] == "swapping"
    env.worker.retry(f.id)
    assert env.worker.process_one() is True
    assert env.status(f.id) == "converted" and env.encodes == 1
    assert env.run_row()["state"] == "done"
    assert env.scratch_runs() == []


def test_target_exists_fails_the_run_when_the_original_is_intact(env, monkeypatch):
    def taken(plan):
        raise TargetExists("in the way")

    monkeypatch.setattr(worker_module, "execute_swap", taken)
    env.queue_video()
    env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert row.status == "failed" and "in the way" not in row.reason
    assert run["state"] == "failed"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.incoming_files() == [] and env.scratch_runs() == []


def test_file_already_at_the_new_path_is_refused_before_converting(env):
    avi = env.queue(source_video(env.lib / "film.avi"))
    (env.lib / "film.mkv").write_bytes(b"someone else's file")
    assert env.worker.process_one() is True
    row = env.db.get_file(avi.id)
    assert (row.status, row.reason) == ("failed", TARGET_TAKEN)
    assert env.encodes == 0 and env.db.file_details(avi.id)["runs"] == []
    assert env.scratch_runs() == [] and env.incoming_files() == []
    assert (env.lib / "film.mkv").read_bytes() == b"someone else's file"
    assert (env.lib / "film.avi").read_bytes() == env.original_bytes


def test_a_file_named_like_the_original_is_no_conflict_when_the_name_stays(env):
    env.queue_video(name="same.mkv")
    env.worker.process_one()
    assert env.status() == "converted"


def test_file_appearing_at_the_new_path_during_convert_fails_at_swap_time(env):
    def someone_else_writes(cmd, duration, on_progress, should_stop):
        (env.lib / "film.mkv").write_bytes(b"someone else's file")
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    # AVI keeps no track languages, so this output passes every check and reaches the swap.
    env.encode_output = dict(vcodec="libx265", crf=35, seconds=2, size=SIZE, audio=(("und", ""),))
    env.use_encode(someone_else_writes)
    avi = env.queue(source_video(env.lib / "film.avi"))
    env.worker.process_one()
    row, run = env.db.get_file(avi.id), env.run_row(avi.id)
    assert all(json.loads(run["checks_json"]).values())
    assert run["swap_json"]
    assert (row.status, row.reason) == ("failed", TARGET_TAKEN)
    assert (run["state"], run["error"]) == ("failed", TARGET_TAKEN)
    assert env.incoming_files() == [] and env.scratch_runs() == []
    assert (env.lib / "film.mkv").read_bytes() == b"someone else's file"
    assert (env.lib / "film.avi").read_bytes() == env.original_bytes


def test_target_exists_keeps_temp_when_the_original_is_not_where_it_was(env, monkeypatch):
    def taken_after_moving(plan):
        os.replace(plan.original, plan.holding if plan.holding else plan.original + ".x")
        raise TargetExists("in the way")

    monkeypatch.setattr(worker_module, "execute_swap", taken_after_moving)
    os.makedirs(env.held("x").parent)
    env.queue_video()
    env.worker.process_one()
    run = env.run_row()
    assert run["state"] == "swapping"
    assert env.incoming_files() == [temp_name(run)]


def test_unexpected_crash_during_swap_keeps_everything_for_recovery(env, monkeypatch):
    def crash(plan):
        raise SimulatedCrash("boom")

    monkeypatch.setattr(swap, "_last_step", crash)
    env.queue_video()
    env.worker.process_one()  # must not raise
    assert env.run_row()["state"] == "swapping"
    assert len(env.incoming_files()) == 1
    assert env.held("movie.mkv").read_bytes() == env.original_bytes


def test_unexpected_error_before_the_swap_fails_the_file_not_the_worker(env, monkeypatch):
    def broken(*args, **kwargs):
        raise SimulatedCrash("bug")

    monkeypatch.setattr(worker_module, "build_command", broken)
    env.queue_video()
    assert env.worker.process_one() is True
    assert env.status() == "failed"
    assert env.run_row()["state"] == "failed"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.scratch_runs() == []


# ---- Pause, resume and stop ----

def waits_for_stop(started):
    def fake(cmd, duration, on_progress, should_stop):
        started.set()
        while not should_stop():
            time.sleep(0.01)
        return EncodeResult(255, True, "")
    return fake


def test_pause_stops_current_and_requeues(env):
    started = threading.Event()
    env.use_encode(waits_for_stop(started))
    env.queue_video()
    thread = threading.Thread(target=env.worker.process_one)
    thread.start()
    assert started.wait(30)
    assert env.worker.status()["state"] == "working"
    assert env.worker.status()["step"] == "Converting"
    env.worker.pause("Taking a break")
    thread.join(30)
    assert not thread.is_alive()

    status = env.worker.status()
    assert status["state"] == "paused" and status["message"] == "Taking a break"
    assert status["file"] is None
    assert env.status() == "queued"
    assert env.scratch_runs() == [] and env.incoming_files() == []
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.run_row()["state"] == "interrupted"

    assert env.worker.process_one() is False
    env.worker.resume()
    env.use_good_encode()
    assert env.worker.process_one() is True
    assert env.status() == "converted"


def test_pause_during_checking_requeues_instead_of_failing(env):
    env.queue_video()
    real_verify = worker_module.verify_output

    def verify_with_pause(*args, **kwargs):
        env.worker.pause()
        return real_verify(*args, **kwargs)  # the real checks see the stop right away

    env.monkeypatch.setattr(worker_module, "verify_output", verify_with_pause)
    env.worker.process_one()
    assert env.status() == "queued"
    assert env.scratch_runs() == []


def test_pause_while_copying_to_the_library_removes_the_partial_temp(env, monkeypatch):
    real_copy = worker_module._copy_file

    def copy_then_pause(source, target, on_fraction, should_stop):
        real_copy(source, target, on_fraction, should_stop)
        if Path(target).parent == env.incoming:
            env.worker.pause()

    monkeypatch.setattr(worker_module, "_copy_file", copy_then_pause)
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "queued"
    assert env.incoming_files() == [] and env.scratch_runs() == []


def test_nonzero_exit_during_a_pause_is_treated_as_the_pause(env):
    def dies_as_paused(cmd, duration, on_progress, should_stop):
        env.worker.pause()
        return EncodeResult(1, False, "terminated")  # exited before the stop was noticed

    env.use_encode(dies_as_paused)
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "queued"


def test_stop_event_also_stops_current_work(env):
    started = threading.Event()
    env.use_encode(waits_for_stop(started))
    env.queue_video()
    stop = threading.Event()
    thread = threading.Thread(target=env.worker.run_forever, args=(stop,))
    thread.start()
    assert started.wait(30)
    stop.set()
    thread.join(30)
    assert not thread.is_alive()
    assert env.status() == "queued"
    assert env.scratch_runs() == []


def test_run_forever_sleeps_when_idle_and_stops_promptly(env):
    calls = []
    real = env.worker.process_one

    def counted():
        calls.append(1)
        return real()

    env.worker.process_one = counted
    stop = threading.Event()
    thread = threading.Thread(target=env.worker.run_forever, args=(stop,))
    thread.start()
    time.sleep(0.5)
    began = time.monotonic()
    stop.set()
    thread.join(10)
    assert not thread.is_alive()
    assert time.monotonic() - began < 1
    assert 1 <= len(calls) <= 5


def test_run_forever_survives_an_unexpected_error(env):
    calls = []

    def broken():
        calls.append(1)
        raise SimulatedCrash("bug")

    env.worker.process_one = broken
    env.monkeypatch.setattr(worker_module, "IDLE_WAIT_SECONDS", 0.01)
    stop = threading.Event()
    thread = threading.Thread(target=env.worker.run_forever, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 10
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(10)
    assert len(calls) >= 2


def test_status_can_be_read_while_working_and_shows_progress(env):
    seen = []

    def reports(cmd, duration, on_progress, should_stop):
        on_progress(0.5)
        seen.append(env.worker.status())
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    env.use_encode(reports)
    f = env.queue_video()
    env.worker.process_one()
    assert seen[0]["state"] == "working" and seen[0]["step"] == "Converting"
    assert seen[0]["file"] == f.path and seen[0]["percent"] == 50
    assert seen[0]["eta_seconds"] is not None and seen[0]["eta_seconds"] >= 0


# ---- Retry ----

def test_retry_requeues_a_failed_file(env):
    f = env.queue_video()
    env.fail_encodes()
    env.worker.process_one()
    assert env.status() == "failed"
    env.worker.retry(f.id)
    assert env.status() == "queued" and env.file().reason is None


def test_retry_leaves_other_files_alone(env):
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "converted"
    env.worker.retry(env.ids[0])
    assert env.status() == "converted"
    env.worker.retry(99999)  # no such file


# ---- Startup recovery ----

def test_recover_finishes_interrupted_swap(env, monkeypatch):
    def crash(plan):
        raise SimulatedCrash("power cut between the two renames")

    monkeypatch.setattr(swap, "_last_step", crash)
    f = env.queue_video()
    env.worker.process_one()
    assert env.held("movie.mkv").exists() and not env.lib_file.exists()
    monkeypatch.undo()
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(env.volume))

    worker = Worker(env.db, env.config)
    worker.recover()
    row = env.db.get_file(f.id)
    assert row.status == "converted"
    assert row.size == env.lib_file.stat().st_size
    assert probe(env.lib_file, "ffprobe").video_codec == "hevc"
    assert env.held("movie.mkv").read_bytes() == env.original_bytes
    assert env.run_row()["state"] == "done" and env.db.stats()["saved_bytes"] > 0
    assert env.incoming_files() == [] and env.scratch_runs() == []


def test_recover_carries_out_a_swap_that_had_not_started(env):
    env.queue_video()
    env.crash_before_swap()
    assert env.run_row()["state"] == "swapping"
    env.restarted_worker().recover()
    assert env.status() == "converted"
    assert env.held("movie.mkv").read_bytes() == env.original_bytes


def test_recover_requeues_when_original_changed_while_the_app_was_down(env):
    env.queue_video()
    env.crash_before_swap()
    env.lib_file.write_bytes(b"newer")
    env.restarted_worker().recover()
    assert env.status() == "queued"
    assert env.lib_file.read_bytes() == b"newer"
    assert env.incoming_files() == []
    assert env.run_row()["state"] == "interrupted"


def test_recover_waiting_run_still_in_use_keeps_waiting(env):
    env.queue_video()
    env.crash_before_swap()
    with in_use(env, env.lib_file):
        env.restarted_worker().recover()
        assert env.status() == "waiting_in_use"
        assert env.run_row()["state"] == "waiting_in_use"
        assert len(env.incoming_files()) == 1


def test_recover_unexpected_state_keeps_temp_and_later_retry_can_finish(env, monkeypatch, caplog):
    f = env.queue_video()
    env.crash_before_swap()
    temp_name = env.incoming_files()[0]
    away = env.tmp_path / "away.mkv"
    os.replace(env.lib_file, away)  # looks like an absent original

    with caplog.at_level(logging.ERROR, logger="shelfsaver.worker"):
        env.restarted_worker().recover()
    assert env.run_row()["state"] == "swapping"
    assert env.incoming_files() == [temp_name]
    assert env.status(f.id) == "failed" and env.file().reason
    assert any(r.levelno == logging.ERROR for r in caplog.records)

    # The drive comes back and the owner retries.
    os.replace(away, env.lib_file)
    env.worker.retry(f.id)
    assert env.worker.process_one() is True
    assert env.status(f.id) == "converted"


@pytest.mark.parametrize("error", [FileInUse("x"), TargetExists("x"), OSError("x")])
def test_recover_survives_every_error_from_recover_swap(env, monkeypatch, error):
    env.queue_video()
    env.crash_before_swap()
    temp_name = env.incoming_files()[0]

    def raising(plan):
        raise error

    monkeypatch.setattr(worker_module, "recover_swap", raising)
    env.restarted_worker().recover()
    assert env.lib_file.read_bytes() == env.original_bytes
    if not isinstance(error, TargetExists):
        assert env.incoming_files() == [temp_name]


@pytest.mark.parametrize("bad", ["not json", "[]", "{}", '{"original": 1}'])
def test_recover_survives_a_bad_plan(env, bad):
    first = env.queue_video(name="first.mkv")
    env.crash_before_swap()
    env.db.update_run(env.run_row(first.id)["id"], swap_json=bad)
    second = env.queue_video(name="second.mkv")
    env.crash_before_swap()

    env.restarted_worker().recover()
    assert env.run_row(first.id)["state"] == "swapping"
    assert env.status(first.id) == "failed"
    assert (env.lib / "first.mkv").exists()
    assert len(env.incoming_files()) == 1  # the bad plan's temp is kept; the good one finished
    assert env.status(second.id) == "converted"


# ---- saved swap plans: the fingerprint field was added later, so older plans lack it

def _saved(plan, **changes):
    """A plan as the database holds it, with any keys removed or changed."""
    data = json.loads(worker_module._dump_swap(plan, 100, 1.0))
    for key, value in changes.items():
        if value is KeyError:
            data.pop(key)
        else:
            data[key] = value
    return json.dumps(data)


def _plan(**kwargs):
    return swap.SwapPlan("a.mkv", "t.mkv", "a.mkv", None, 3, 4, **kwargs)


def test_a_saved_plan_keeps_its_fingerprint():
    plan = _plan(temp_fingerprint="ab" * 32)
    loaded, size, mtime = worker_module._load_swap(_saved(plan))
    assert loaded == plan and (size, mtime) == (100, 1.0)


def test_a_plan_saved_before_fingerprints_existed_still_loads():
    loaded, _, _ = worker_module._load_swap(_saved(_plan(), temp_fingerprint=KeyError))
    assert loaded.temp_fingerprint is None and loaded.expected_size == 3


@pytest.mark.parametrize("bad", [5, ["x"], {"a": 1}, True])
def test_a_plan_with_a_fingerprint_of_the_wrong_type_is_damaged(bad):
    with pytest.raises(ValueError):
        worker_module._load_swap(_saved(_plan(), temp_fingerprint=bad))


@pytest.mark.parametrize("field", ["original", "temp", "final", "holding", "expected_size", "temp_file_id"])
def test_a_plan_missing_any_other_field_is_still_damaged(field):
    # Only the new, optional field may be absent. Dropping anything else must not load "successfully".
    with pytest.raises(KeyError):
        worker_module._load_swap(_saved(_plan(), **{field: KeyError}))


def test_recover_finishes_a_swap_planned_before_fingerprints_existed(env):
    f = env.queue_video()
    env.crash_before_swap()
    run = env.run_row(f.id)
    data = json.loads(run["swap_json"])
    assert data["temp_fingerprint"]  # new plans do carry one
    data.pop("temp_fingerprint")      # what an earlier version would have saved
    env.db.update_run(run["id"], swap_json=json.dumps(data))
    env.restarted_worker().recover()
    assert env.status(f.id) == "converted"


def test_recover_does_not_finish_a_swap_whose_new_file_was_rewritten(env):
    f = env.queue_video()
    env.crash_before_swap()
    temp = next(iter(env.incoming.iterdir()))
    with open(temp, "r+b") as handle:  # same file ID and size, different bytes
        handle.write(b"\0" * 16)
    env.restarted_worker().recover()
    assert env.status(f.id) != "converted"
    assert env.lib_file.read_bytes() == env.original_bytes  # the original is never given up


def test_recover_requeues_working_and_cleans_only_run_folders(env):
    unrelated = env.scratch / "family-photos.zip"
    unrelated.write_bytes(b"keep")
    (env.scratch / "ShelfSaver").mkdir()
    also_unrelated = env.scratch / "ShelfSaver" / "notes.txt"
    also_unrelated.write_bytes(b"keep")
    (env.scratch / "ShelfSaver" / "run-41").mkdir()
    (env.scratch / "ShelfSaver" / "run-41" / "output.mkv").write_bytes(b"leftover")
    (env.scratch / "ShelfSaver" / "other-folder").mkdir()

    f = env.queue_video()
    run = env.db.start_run(f.id, f.size, "H.264", None, "why")
    env.db.set_file_status(f.id, "working")
    rowless = env.queue(source_video(env.lib / "no-run.mkv"))
    env.db.set_file_status(rowless.id, "working")

    env.incoming.mkdir(parents=True)
    partial = env.incoming / temp_name(run)
    partial.write_bytes(b"half copied")
    orphan = env.incoming / "999.mkv"
    orphan.write_bytes(b"orphan")

    env.worker.recover()
    assert env.status(f.id) == "queued" and env.status(rowless.id) == "queued"
    assert env.run_row(f.id)["state"] == "interrupted"
    assert unrelated.read_bytes() == b"keep"
    assert also_unrelated.read_bytes() == b"keep"
    assert (env.scratch / "ShelfSaver" / "other-folder").is_dir()
    assert not (env.scratch / "ShelfSaver" / "run-41").exists()
    assert not partial.exists()  # its run is known, and that run's original is still there
    assert orphan.read_bytes() == b"orphan"  # no run is known, so it can't be proven spare
    assert env.lib_file.exists()


def test_recover_keeps_incoming_files_of_runs_still_in_progress(env):
    env.queue_video()
    env.crash_before_swap()
    env.db.update_run(env.run_row()["id"], swap_json="not json")
    keep = env.incoming_files()
    env.restarted_worker().recover()
    assert env.incoming_files() == keep


def test_power_cut_that_loses_the_swap_record_deletes_nothing(env, monkeypatch, caplog):
    """The move happened, but the database fell back to before `swapping` was committed."""
    def crash(plan):
        raise SimulatedCrash("power cut between the two renames")

    monkeypatch.setattr(swap, "_last_step", crash)
    f = env.queue_video()
    env.worker.process_one()
    monkeypatch.undo()
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(env.volume))
    run = env.run_row()
    env.db.update_run(run["id"], state="running", swap_json=None, now_path=None, error=None)
    env.db.set_file_status(f.id, "working")
    temp = env.incoming / temp_name(run)
    new_bytes = temp.read_bytes()
    assert not env.lib_file.exists()

    with caplog.at_level(logging.ERROR, logger="shelfsaver.worker"):
        worker = env.restarted_worker()
        worker.recover()
        assert worker.process_one() is False
        worker.retry(f.id)
        worker.process_one()
        env.restarted_worker().recover()

    assert temp.read_bytes() == new_bytes
    assert env.held("movie.mkv").read_bytes() == env.original_bytes
    row, after = env.file(), env.run_row()
    assert row.status == "failed" and "Nothing was deleted" in row.reason
    assert after["state"] == "swapping"  # also what stops the holding folder being cleared
    assert after["saved_bytes"] == 0 and env.db.stats()["converted"] == 0
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def write_incoming(env, name):
    env.incoming.mkdir(parents=True, exist_ok=True)
    path = env.incoming / name
    path.write_bytes(b"temp")
    return path


def ended_run(env, f, state="interrupted", **fields):
    run = env.db.start_run(f.id, f.size, "H.264", None, "why")
    env.db.update_run(run.id, state=state, **fields)
    return run


def test_recover_removes_a_temp_of_an_ended_run_whose_original_is_there(env):
    f = env.queue_video()
    temp = write_incoming(env, temp_name(ended_run(env, f)))
    env.worker.recover()
    assert not temp.exists()


def test_recover_keeps_a_temp_of_an_ended_run_whose_original_is_gone(env, caplog):
    f = env.queue_video()
    temp = write_incoming(env, temp_name(ended_run(env, f)))
    env.lib_file.unlink()
    with caplog.at_level(logging.ERROR, logger="shelfsaver.worker"):
        env.worker.recover()
    assert temp.exists()
    assert any("Keeping" in r.getMessage() for r in caplog.records)


def test_recover_keeps_a_temp_with_no_known_run(env, caplog):
    temp = write_incoming(env, "999.mkv")
    other = write_incoming(env, "notes.txt")
    with caplog.at_level(logging.ERROR, logger="shelfsaver.worker"):
        env.worker.recover()
    assert temp.exists() and other.exists()
    assert sum("Keeping" in r.getMessage() for r in caplog.records) == 2


def test_recover_keeps_a_temp_that_any_plan_names_by_file_id(env):
    f = env.queue_video()
    temp = write_incoming(env, "renamed.mkv")
    plan = dict(original=str(env.lib_file), temp=str(temp), final=str(env.lib_file), holding=None,
                expected_size=4, temp_file_id=os.lstat(temp).st_ino,
                original_size=f.size, original_mtime=f.mtime)
    run = ended_run(env, f, state="failed", swap_json=json.dumps(plan))
    numbered = env.incoming / temp_name(run)
    os.replace(temp, numbered)  # the run is known and its original is there, yet a plan names it
    env.worker.recover()
    assert numbered.exists()


def test_delete_temp_refuses_anything_outside_incoming(env):
    precious = env.lib / "precious.mkv"
    precious.write_bytes(b"keep")
    env.worker._delete_temp(precious)
    assert precious.read_bytes() == b"keep"


def test_delete_temp_refuses_an_incoming_folder_outside_shelfsaver(env):
    folder = env.lib / "incoming"
    folder.mkdir()
    precious = folder / "precious.mkv"
    precious.write_bytes(b"keep")
    env.worker._delete_temp(precious)
    assert precious.read_bytes() == b"keep"


def strand_a_temp(env, monkeypatch):
    """Same name with holding: the original is held, and temp is the only new copy."""
    real_last_step = swap._last_step

    def crash(plan):
        raise SimulatedCrash("power cut between the two renames")

    monkeypatch.setattr(swap, "_last_step", crash)
    env.queue_video()
    env.worker.process_one()
    monkeypatch.setattr(swap, "_last_step", real_last_step)
    temp = env.incoming / temp_name(env.run_row())
    assert temp.exists() and not env.lib_file.exists()
    return temp, temp.read_bytes()


def reset_database(env):
    """The database was replaced, so run IDs start again from 1."""
    env.db = Database(env.tmp_path / "data" / "fresh.db")
    env.db.set_setting("scratch_dir", str(env.scratch))
    env.library = env.db.add_library("Movies", str(env.lib), "high", False)
    env.worker = Worker(env.db, env.config)
    env.ids = []


def test_new_run_after_a_database_reset_leaves_a_kept_temp_alone(env, monkeypatch):
    temp, new_bytes = strand_a_temp(env, monkeypatch)
    old_run_id = env.run_row()["id"]
    reset_database(env)
    env.worker.recover()
    assert temp.read_bytes() == new_bytes

    env.queue(source_video(env.lib / "other.mkv"))
    env.worker.process_one()
    assert env.run_row()["id"] == old_run_id  # the same ID really did come round again
    assert env.status() == "converted"
    assert temp.read_bytes() == new_bytes
    assert env.held("movie.mkv").read_bytes() != b""


def test_a_name_collision_fails_the_run_and_overwrites_nothing(env):
    def someone_took_the_name(cmd, duration, on_progress, should_stop):
        env.incoming.mkdir(parents=True, exist_ok=True)
        (env.incoming / temp_name(env.run_row())).write_bytes(b"kept")
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    env.use_encode(someone_took_the_name)
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "failed" and env.run_row()["state"] == "failed"
    assert (env.incoming / temp_name(env.run_row())).read_bytes() == b"kept"
    assert env.lib_file.read_bytes() == env.original_bytes


def test_reused_run_id_does_not_make_a_kept_temp_an_orphan(env, monkeypatch):
    temp, new_bytes = strand_a_temp(env, monkeypatch)
    old_run_id = env.run_row()["id"]
    reset_database(env)
    other = env.queue(source_video(env.lib / "other.mkv"))
    run = env.db.start_run(other.id, other.size, "H.264", None, "why")
    env.db.update_run(run.id, state="interrupted")
    assert run.id == old_run_id
    env.worker.recover()
    assert temp.read_bytes() == new_bytes


def test_a_temp_named_with_only_the_run_id_is_kept(env):
    f = env.queue_video()
    temp = write_incoming(env, f"{ended_run(env, f).id}.mkv")
    env.worker.recover()
    assert temp.exists()


@pytest.mark.parametrize("name", [
    "9" * 30 + ".mkv", "9" * 30 + "-" + "9" * 30 + ".mkv", "-1-2.mkv", "1-.mkv", "1-2-3.mkv",
    "\u0663-\u0663.mkv", ".mkv",
])
def test_recover_keeps_oddly_named_incoming_files(env, name):
    temp = write_incoming(env, name)
    env.worker.recover()
    assert temp.exists()


def lose_the_swap_record(env):
    run = env.run_row()
    env.db.update_run(run["id"], state="running", swap_json=None, now_path=None, error=None)
    env.db.set_file_status(env.ids[0], "working")


def test_disk_error_listing_incoming_keeps_the_run_as_it_is(env, monkeypatch, caplog):
    temp, new_bytes = strand_a_temp(env, monkeypatch)
    lose_the_swap_record(env)
    real_list = worker_module._list_folder

    def not_ready(path):
        raise OSError(21, "device not ready")

    monkeypatch.setattr(worker_module, "_list_folder", not_ready)
    with caplog.at_level(logging.ERROR, logger="shelfsaver.worker"):
        env.restarted_worker().recover()
    assert env.run_row()["state"] == "running" and env.status() == "working"
    assert temp.read_bytes() == new_bytes
    assert any("can't say" in r.getMessage() for r in caplog.records)
    assert env.worker.process_one() is False

    monkeypatch.setattr(worker_module, "_list_folder", real_list)
    env.restarted_worker().recover()
    assert env.run_row()["state"] == "swapping" and env.status() == "failed"
    assert temp.read_bytes() == new_bytes


def test_disk_error_checking_the_original_keeps_the_run_as_it_is(env, monkeypatch):
    temp, new_bytes = strand_a_temp(env, monkeypatch)
    lose_the_swap_record(env)

    def not_ready(path):
        raise OSError(21, "device not ready")

    monkeypatch.setattr(worker_module, "_present", not_ready)
    env.restarted_worker().recover()
    assert env.run_row()["state"] == "running" and env.status() == "working"
    assert temp.read_bytes() == new_bytes


def test_volume_that_is_gone_keeps_the_run_as_it_is(env, monkeypatch):
    env.queue_video()
    env.crash_before_swap()
    lose_the_swap_record(env)
    env.lib_file.unlink()
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(env.tmp_path / "unplugged"))
    env.restarted_worker().recover()
    assert env.run_row()["state"] == "running" and env.status() == "working"


def test_no_incoming_folder_means_an_ordinary_interruption(env):
    f = env.queue_video()
    env.db.start_run(f.id, f.size, "H.264", None, "why")
    env.db.set_file_status(f.id, "working")
    env.lib_file.unlink()
    env.worker.recover()
    assert env.run_row()["state"] == "interrupted" and env.status() == "queued"


@windows_only
def test_part_of_the_original_locked_waits_instead_of_failing(env):
    f = env.queue_video()
    with open(env.lib_file, "r+b") as holder:
        msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 4096)
        try:
            env.worker.process_one()
            assert env.status(f.id) == "waiting_in_use"
        finally:
            holder.seek(0)
            msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 4096)


def test_recover_with_no_scratch_set_still_works(env):
    env.db.set_setting("scratch_dir", "")
    f = env.queue_video()
    env.db.set_file_status(f.id, "working")
    env.worker.recover()
    assert env.status(f.id) == "queued"


def test_recover_survives_a_library_on_a_drive_that_is_gone(env):
    env.db.add_library("Gone", str(env.tmp_path / "nowhere" / "lib"), "high", False)
    env.monkeypatch.setattr(worker_module, "_list_folder", _raising_list_folder)
    env.worker.recover()


def _raising_list_folder(path):
    raise OSError("drive not ready")


def test_recover_does_not_follow_a_link_named_like_a_run_folder(env):
    precious = env.tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_bytes(b"keep")
    (env.scratch / "ShelfSaver").mkdir()
    link = env.scratch / "ShelfSaver" / "run-7"
    try:
        os.symlink(precious, link, target_is_directory=True)
    except OSError:
        pytest.skip("this account can't create links")
    env.worker.recover()
    assert (precious / "keep.txt").read_bytes() == b"keep"


# ---- No scratch disk ----

def test_no_scratch_set_means_waiting_not_failing(env):
    env.db.set_setting("scratch_dir", "")
    env.queue_video()
    assert env.worker.process_one() is False
    assert env.worker.status()["state"] == "no_scratch"
    assert env.status() == "queued" and env.encodes == 0

    env.db.set_setting("scratch_dir", str(env.scratch))
    assert env.worker.process_one() is True
    assert env.status() == "converted"


def test_scratch_folder_gone_means_waiting_not_failing(env):
    env.db.set_setting("scratch_dir", str(env.tmp_path / "unplugged"))
    env.queue_video()
    assert env.worker.process_one() is False
    assert env.worker.status()["state"] == "no_scratch"
    assert env.status() == "queued"


def test_claim_takes_nothing_when_the_row_changed_after_it_was_picked(env, monkeypatch):
    env.use_good_encode()
    row = env.queue_video()
    real_probe = worker_module.probe

    def probe_then_scanner_rewrites_row(*args):
        result = real_probe(*args)
        env.db.upsert_file(env.library.id, row.path, row.size + 1, row.mtime, "queued")
        return result

    monkeypatch.setattr(worker_module, "probe", probe_then_scanner_rewrites_row)
    env.worker.process_one()
    assert env.status() == "queued"
    assert env.encodes == 0
    assert env.db.file_details(row.id)["runs"] == []


def test_tracks_record_whether_the_picture_is_hdr():
    hdr = json.loads(worker_module._tracks_json(
        info(color_transfer="smpte2084", streams=eng_spa()), convert()))
    sdr = json.loads(worker_module._tracks_json(info(streams=eng_spa()), convert()))
    assert hdr["hdr"] is True
    assert sdr["hdr"] is False


# ---- how long the conversion itself took

def test_conversion_time_leaves_out_waiting_for_room(env, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(worker_module, "_now", lambda: clock["now"])

    def slow_encode(cmd, duration, on_progress, should_stop):
        clock["now"] += 90.0
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    env.use_encode(slow_encode)
    env.queue_video()
    env.free["library"] = 0
    assert env.worker.process_one() is False  # converted, and now waiting for room
    clock["now"] += 5000.0
    env.free["library"] = LOTS_OF_ROOM
    assert env.worker.process_one() is True
    run = env.run_row()
    assert run["state"] == "done" and run["convert_seconds"] == 90.0


# ---- a library drive that goes away

def add_other_library(env):
    other = env.volume / "Media" / "Other"
    other.mkdir()
    library = env.db.add_library("Other", str(other), "high", False)
    return library, source_video(other / "other.mkv", seconds=1)


def queue_in(env, library, path):
    st = os.stat(path)
    return env.db.upsert_file(library.id, str(path), st.st_size, st.st_mtime, "queued")


def test_parked_job_on_a_disconnected_drive_lets_other_libraries_go_on(env):
    env.queue_video()
    env.free["library"] = 0
    assert env.worker.process_one() is False
    assert env.worker.status()["message"] == "Not enough room on the library drive"
    run = env.run_row()
    other_library, other_path = add_other_library(env)
    other = queue_in(env, other_library, other_path)

    gone = env.tmp_path / "unplugged"
    shutil.move(str(env.lib), str(gone))
    assert env.worker.process_one() is True
    assert env.worker._parked is None
    assert env.worker.status()["message"] is None
    assert env.status() == "queued"  # kept in the queue for when the drive is back
    assert env.db.run_record(run["id"])["state"] == "interrupted"
    assert "offline" in env.db.run_record(run["id"])["error"]
    assert not (env.scratch / "ShelfSaver" / f"run-{run['id']}").exists()
    statuses = {l.name: l.status for l in env.db.list_libraries()}
    assert statuses == {"Movies": "offline", "Other": "ok"}

    env.free["library"] = LOTS_OF_ROOM
    assert env.worker.process_one() is True
    assert env.status(other.id) == "converted"
    assert env.status() == "queued"  # still not picked while its drive is gone

    shutil.move(str(gone), str(env.lib))
    assert env.worker.process_one() is True
    assert env.status() == "converted"
    assert env.held("movie.mkv").read_bytes() == env.original_bytes


def test_library_drive_that_stops_answering_counts_as_offline(env, monkeypatch):
    env.queue_video()
    real = env._free_bytes

    def not_ready(path):
        if Path(path).is_relative_to(env.scratch):
            return real(path)
        raise OSError("drive not ready")

    monkeypatch.setattr(worker_module, "free_bytes", not_ready)
    assert env.worker.process_one() is True
    assert env.worker._parked is None and env.status() == "queued"
    assert env.db.list_libraries()[0].status == "offline"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.incoming_files() == []


def test_no_room_on_a_connected_drive_still_waits(env):
    env.queue_video()
    env.free["library"] = 0
    assert env.worker.process_one() is False
    assert env.worker.process_one() is False
    assert env.worker._parked is not None and env.encodes == 1
    assert env.db.list_libraries()[0].status == "ok"


# ---- remembering the drives that held a file

def test_moving_a_file_to_holding_remembers_its_volume(env):
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "converted"
    assert env.db.held_volumes() == [str(env.volume)]


def test_replacing_without_holding_remembers_nothing(env):
    env.db.set_setting("holding_enabled", "0")
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "converted" and env.db.held_volumes() == []


# ---- Try again, for a file that never had a run

def test_try_again_works_for_a_file_that_failed_at_scan_time(env):
    bad = env.lib / "bad.mkv"
    bad.write_bytes(b"not a video")
    st = os.stat(bad)
    f = env.db.upsert_file(env.library.id, str(bad), st.st_size, st.st_mtime, "failed",
                           "Couldn't read this file")
    assert env.db.file_details(f.id)["runs"] == []
    env.worker.retry(f.id)
    assert env.db.get_file(f.id).status == "queued"
    assert env.worker.process_one() is True
    row = env.db.get_file(f.id)
    assert row.status == "failed" and row.reason == "Couldn't read this file"
    # Once the file has been put right, it goes all the way through.
    source_video(bad)
    st = os.stat(bad)
    env.db.upsert_file(env.library.id, str(bad), st.st_size, st.st_mtime, "failed",
                       "Couldn't read this file")
    env.worker.retry(f.id)
    assert env.worker.process_one() is True
    assert env.db.get_file(f.id).status == "converted"


# ---- Shrinking oversized H.265 files ----

SAMPLE_SHORT = "Sample showed too little saving"


def h265_library(env, english_only=False):
    env.db.update_library(env.library.id, shrink_h265=True, english_only=english_only)


def predicted(env, saving):
    """Fake the sample test; records the files it was asked about."""
    asked = []

    def fake(info, level, source, folder, ffmpeg, ffprobe, should_stop, encoder=None):
        asked.append(Path(source))
        if isinstance(saving, Exception):
            raise saving
        return saving
    env.monkeypatch.setattr(worker_module, "predict_saving", fake)
    return asked


def test_a_sample_that_saves_enough_leads_to_a_full_conversion(env):
    h265_library(env)
    asked = predicted(env, 0.40)
    env.queue_video(vcodec="libx265")
    env.worker.process_one()
    assert env.status() == "converted" and env.encodes == 1
    assert asked == [env.lib_file]  # sampled from the library file, before any copying
    assert "Sample: about 40% smaller" in env.run_row()["log_tail"]


def test_a_sample_that_saves_too_little_leaves_the_file_untouched(env):
    h265_library(env)
    predicted(env, 0.15)
    env.queue_video(vcodec="libx265")
    env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert (row.status, row.reason) == ("skipped", SAMPLE_SHORT)
    assert (run["state"], run["error"]) == ("not_worth_it", SAMPLE_SHORT)
    assert "Sample: about 15% smaller" in run["log_tail"]
    assert env.encodes == 0 and env.scratch_runs() == []
    assert env.lib_file.read_bytes() == env.original_bytes


def test_a_short_sample_still_removes_extra_audio(env):
    h265_library(env, english_only=True)
    predicted(env, 0.10)
    env.encode_output = dict(vcodec="libx265", crf=35, seconds=2, size=SIZE)
    f = env.queue_video(vcodec="libx265", audio=(("eng", "English"), ("spa", "Spanish")))
    env.worker.process_one()
    run = env.run_row()
    assert env.status() == "converted"
    assert run["why"] == "Only the extra audio tracks can be removed"
    assert [t["language"] for t in json.loads(run["tracks_json"])["removed"]] == ["spa"]
    assert env.db.get_file(f.id).reason is None


def test_a_sample_that_fails_skips_the_file_with_a_reason(env):
    from shelfsaver.sample import SampleError
    h265_library(env)
    predicted(env, SampleError("Could not cut a section: broken"))
    env.queue_video(vcodec="libx265")
    env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert (row.status, row.reason) == ("skipped", "Sample test failed")
    assert run["state"] == "not_worth_it" and "Could not cut a section" in run["log_tail"]
    assert env.lib_file.read_bytes() == env.original_bytes


def test_stopping_during_the_sample_starts_the_file_again_later(env):
    h265_library(env)
    predicted(env, None)
    env.queue_video(vcodec="libx265")
    env.worker.process_one()
    assert env.status() == "queued" and env.run_row()["state"] == "interrupted"
    assert env.scratch_runs() == []


def test_a_full_conversion_that_misses_a_quarter_falls_back_to_removing_audio(env):
    h265_library(env, english_only=True)
    predicted(env, 0.40)
    outputs = [dict(vcodec="libx265", crf=1, seconds=2, size=SIZE),   # barely smaller
               dict(vcodec="libx265", crf=35, seconds=2, size=SIZE)]  # the audio-only pass

    def encode(cmd, duration, on_progress, should_stop):
        env.encode_output = outputs.pop(0)
        return env._fake_encode(cmd, duration, on_progress, should_stop)
    env.use_encode(encode)
    env.queue_video(vcodec="libx265", crf=0, audio=(("eng", "English"), ("spa", "Spanish")))
    env.worker.process_one()
    assert env.status() == "converted" and outputs == []
    assert env.run_row()["why"] == "Only the extra audio tracks can be removed"


def test_a_sample_that_would_grow_says_so_plainly(env):
    h265_library(env)
    predicted(env, -0.8)
    env.queue_video(vcodec="libx265")
    env.worker.process_one()
    assert "Sample: about 80% bigger" in env.run_row()["log_tail"]
    assert env.file().reason == SAMPLE_SHORT


@pytest.mark.skipif(hostos.IS_WINDOWS, reason="Windows files take their access rules from the folder")
def test_a_converted_file_keeps_the_originals_permissions(env):
    # The end-to-end case behind the owner/permissions fix: whatever mode the library's file had,
    # the converted file that takes its place has the same.
    f = env.queue_video()
    os.chmod(env.lib_file, 0o640)
    env.worker.process_one()
    assert env.status(f.id) == "converted"
    assert os.stat(env.lib_file).st_mode & 0o777 == 0o640


@pytest.mark.skipif(hostos.IS_WINDOWS, reason="Windows folders take their access rules from the parent")
def test_the_work_folders_take_the_library_folders_permissions(env):
    # End to end: ShelfSaver/ and ShelfSaver/incoming are created during the first conversion,
    # and must match the library folder (here a non-default 0o750), not whatever the process makes.
    os.chmod(env.lib, 0o750)
    f = env.queue_video()
    env.worker.process_one()
    assert env.status(f.id) == "converted"
    work = env.volume / "ShelfSaver"
    assert os.stat(work).st_mode & 0o777 == 0o750
    assert os.stat(work / "incoming").st_mode & 0o777 == 0o750


@pytest.mark.skipif(hostos.IS_WINDOWS, reason="Linux-only behaviour")
def test_a_linked_work_folder_fails_the_run_without_building_through_the_link(env, tmp_path):
    """The `ShelfSaver` folder on the library's drive is replaced by a link: the converted file must
    not be copied into the link's target, and no folder may be created there."""
    elsewhere = tmp_path / "Elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, env.volume / "ShelfSaver")
    f = env.queue_video()
    env.worker.process_one()
    assert env.status(f.id) != "converted"
    assert list(elsewhere.iterdir()) == []
    assert env.lib_file.read_bytes() == env.original_bytes   # the original is untouched


def test_the_worker_removes_a_commentary_track_when_the_library_asks(env):
    env.db.update_library(env.library.id, remove_commentary=True)
    env.encode_output = dict(vcodec="libx265", crf=40, seconds=2, size=SIZE,
                             audio=(("eng", "English"),))
    f = env.queue(make_video(env.lib / "two.mkv", seconds=2, size=SIZE, vcodec="libx265", crf=20,
                             audio=(("eng", "English"), ("eng", "Director's Commentary"))))
    env.worker.process_one()
    assert env.db.get_file(f.id).status == "converted"
    tracks = json.loads(env.run_row()["tracks_json"])
    assert [t["title"] for t in tracks["removed"]] == ["Director's Commentary"]
    assert "English" in [t["title"] for t in tracks["kept"]]


@pytest.mark.gpu
def test_end_to_end_strip_only_removes_a_commentary_track(env, monkeypatch):
    real_encoder(monkeypatch)
    env.db.update_library(env.library.id, remove_commentary=True)
    f = env.queue(make_video(env.lib / "two.mkv", seconds=2, size="320x240", vcodec="libx265",
                             crf=40, audio=(("eng", "English"), ("eng", "Director's Commentary"))))
    env.worker.process_one()
    row = env.db.get_file(f.id)
    assert row.status == "converted"
    info = probe(row.path, "ffprobe")
    assert [s.title for s in info.streams if s.kind == "audio"] == ["English"]
    tracks = json.loads(env.run_row()["tracks_json"])
    assert [t["title"] for t in tracks["removed"]] == ["Director's Commentary"]
