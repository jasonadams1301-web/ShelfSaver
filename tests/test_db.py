import os
import sqlite3
import stat
import threading

import pytest

from shelfsaver.db import SCHEMA, Database
from shelfsaver.logs import tail_log


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "shelfsaver.db")


@pytest.fixture
def lib(db):
    return db.add_library("Movies", "D:\\Movies", "balanced", True)


def test_next_queued_returns_largest_first(db, lib):
    db.upsert_file(lib.id, "a.mkv", 100, 1.0, "queued")
    db.upsert_file(lib.id, "b.mkv", 900, 1.0, "queued")
    assert db.next_queued().path == "b.mkv"


def test_every_connection_commits_durably(tmp_path):
    first = Database(tmp_path / "a.db")
    first.close()
    second = Database(tmp_path / "a.db")
    assert second._query("PRAGMA synchronous")[0][0] == 2  # FULL
    readings = []
    thread = threading.Thread(
        target=lambda: readings.append(second._query("PRAGMA synchronous")[0][0]))
    thread.start()
    thread.join()
    assert readings == [2]


def test_run_record_includes_the_starting_path(db, lib):
    f = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "queued")
    run = db.start_run(f.id, 100, "H.264", None, "why")
    assert db.run_record(run.id)["file_path"] == "a.mkv"
    assert db.run_record(run.id + 1) is None


def test_next_queued_can_skip_libraries(db, lib):
    other = db.add_library("TV", "D:\\TV", "balanced", True)
    db.upsert_file(lib.id, "a.mkv", 100, 1.0, "queued")
    db.upsert_file(other.id, "b.mkv", 900, 1.0, "queued")
    assert db.next_queued([other.id]).path == "a.mkv"
    assert db.next_queued([lib.id, other.id]) is None


def test_files_with_status(db, lib):
    a = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "working")
    db.upsert_file(lib.id, "b.mkv", 100, 1.0, "queued")
    assert [f.id for f in db.files_with_status("working")] == [a.id]


def test_record_converted_moves_the_row_and_replaces_any_row_at_the_new_path(db, lib):
    f = db.upsert_file(lib.id, "a.avi", 100, 1.0, "working", codec="h264")
    stale = db.upsert_file(lib.id, "a.mkv", 5, 1.0, "missing")
    db.record_converted(f.id, "a.mkv", 40, 2.0, "hevc", "mkv", "640x360")
    row = db.get_file(f.id)
    assert (row.path, row.size, row.mtime, row.status) == ("a.mkv", 40, 2.0, "converted")
    assert (row.codec, row.container, row.resolution) == ("hevc", "mkv", "640x360")
    assert db.get_file(stale.id) is None and db.get_file_by_path("a.avi") is None


def test_stats_survive_library_removal(db, lib):
    f = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "converted")
    r = db.start_run(f.id, 100, "H.264 MKV", 8_000_000, "why")
    db.update_run(r.id, state="done", saved_bytes=60)
    db.remove_library(lib.id)
    assert db.stats()["saved_bytes"] == 60
    assert db.get_file_by_path("a.mkv") is None
    assert len(db.history()) == 1


def test_stats_count_every_file_still_in_the_library(db, lib):
    other = db.add_library("Other", "F:\\Other", "medium", False)
    for name, status in (("a.mkv", "queued"), ("b.mkv", "skipped"), ("c.mkv", "converted"),
                         ("d.mkv", "failed"), ("gone.mkv", "missing")):
        db.upsert_file(lib.id, name, 100, 1.0, status)
    db.upsert_file(other.id, "e.mkv", 100, 1.0, "skipped")
    assert db.stats(lib.id)["total"] == 4
    assert db.stats(other.id)["total"] == 1
    assert db.stats()["total"] == 5


def test_queue_lists_files_in_the_order_they_will_be_converted(db, lib):
    for name, size in (("small.mkv", 10), ("big.mkv", 30), ("tie-a.mkv", 20), ("tie-b.mkv", 20)):
        db.upsert_file(lib.id, name, size, 1.0, "queued")
    db.upsert_file(lib.id, "skipped.mkv", 99, 1.0, "skipped")
    assert [f.path for f in db.queued_files(10)] == ["big.mkv", "tie-a.mkv", "tie-b.mkv", "small.mkv"]
    assert [f.path for f in db.queued_files(2, offset=1)] == ["tie-a.mkv", "tie-b.mkv"]
    assert db.next_queued().path == db.queued_files(1)[0].path
    assert db.queue_totals() == {"files": 4, "bytes": 80}


def test_settings_round_trip_and_default(db):
    assert db.get_setting("scratch_dir") is None
    db.set_setting("scratch_dir", "E:\\work")
    assert db.get_setting("scratch_dir") == "E:\\work"


def test_tail_log_filters_by_text(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "shelfsaver.log").write_text(
        "... start ...\n... boom ...\n... done ...\n", encoding="utf-8"
    )
    assert tail_log(tmp_path, 10, contains="boom") == ["... boom ..."]
    assert tail_log(tmp_path, 2) == ["... boom ...", "... done ..."]


def test_upsert_file_updates_existing_path(db, lib):
    a = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "queued")
    b = db.upsert_file(lib.id, "a.mkv", 200, 2.0, "skipped", reason="small")
    assert a.id == b.id
    assert (b.size, b.status, b.reason) == (200, "skipped", "small")


def test_requeue_and_status(db, lib):
    f = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "queued")
    db.set_file_status(f.id, "failed", "boom")
    assert db.get_file(f.id).status == "failed"
    assert db.next_queued() is None
    db.requeue([f.id])
    assert db.get_file(f.id).status == "queued"
    assert db.get_file(f.id).reason is None


def test_active_runs_and_details(db, lib):
    f = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "working")
    r = db.start_run(f.id, 100, "H.264 MKV", 1000, "why")
    assert [x.id for x in db.active_runs(["running"])] == [r.id]
    db.update_run(r.id, state="done", saved_bytes=5)
    assert db.active_runs(["running", "swapping"]) == []
    details = db.file_details(f.id)
    assert details["file"]["path"] == "a.mkv"
    assert details["runs"][0]["saved_bytes"] == 5


def test_library_update_and_list(db, lib):
    db.update_library(lib.id, status="offline", english_only=False)
    got = db.list_libraries()[0]
    assert got.status == "offline" and got.english_only is False


def test_config_env_and_overrides(tmp_path, monkeypatch):
    from shelfsaver.config import load_config

    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    assert load_config().ffmpeg == "ffmpeg"
    (tmp_path / "config.json").write_text('{"ffmpeg": "x/ff.exe"}', encoding="utf-8")
    cfg = load_config()
    assert cfg.data_dir == tmp_path and cfg.ffmpeg == "x/ff.exe" and cfg.ffprobe == "ffprobe"


def test_holding_enabled_defaults_on_until_set(db):
    assert db.get_setting("holding_enabled") == "1"
    db.set_setting("holding_enabled", "0")
    assert db.get_setting("holding_enabled") == "0"
    assert db.get_setting("scratch_dir") is None


def test_upsert_unless_owned_leaves_a_working_row_alone(tmp_path):
    db = Database(tmp_path / "t.db")
    lib = db.add_library("L", str(tmp_path), "high", False)
    row = db.upsert_file(lib.id, "a.mkv", 1, 1.0, "working", "busy")
    assert not db.upsert_file_unless_owned(lib.id, "a.mkv", 2, 2.0, "queued")
    got = db.get_file(row.id)
    assert (got.status, got.reason, got.size) == ("working", "busy", 1)
    assert db.upsert_file_unless_owned(lib.id, "new.mkv", 2, 2.0, "queued")


def test_upsert_unless_owned_leaves_a_swapping_run_alone(tmp_path):
    db = Database(tmp_path / "t.db")
    lib = db.add_library("L", str(tmp_path), "high", False)
    row = db.upsert_file(lib.id, "a.mkv", 1, 1.0, "failed", "kept")
    run = db.start_run(row.id, 1, "H.264", None, "why")
    db.update_run(run.id, state="swapping")
    assert not db.upsert_file_unless_owned(lib.id, "a.mkv", 2, 2.0, "queued")
    assert not db.set_missing_unless_owned(row.id)
    assert db.get_file(row.id).status == "failed"


def test_set_missing_unless_owned(tmp_path):
    db = Database(tmp_path / "t.db")
    lib = db.add_library("L", str(tmp_path), "high", False)
    busy = db.upsert_file(lib.id, "a.mkv", 1, 1.0, "working")
    idle = db.upsert_file(lib.id, "b.mkv", 1, 1.0, "queued")
    assert not db.set_missing_unless_owned(busy.id)
    assert db.get_file(busy.id).status == "working"
    assert db.set_missing_unless_owned(idle.id)
    assert db.get_file(idle.id).status == "missing"


def test_claim_file_needs_a_queued_unchanged_row(tmp_path):
    db = Database(tmp_path / "t.db")
    lib = db.add_library("L", str(tmp_path), "high", False)
    row = db.upsert_file(lib.id, "a.mkv", 10, 1.0, "queued")
    assert not db.claim_file(row.id, 11, 1.0, 10, 1.0)
    assert not db.claim_file(row.id, 10, 2.0, 10, 1.0)
    assert db.get_file(row.id).status == "queued"
    assert db.claim_file(row.id, 10, 1.0, 12, 3.0, codec="h264")
    got = db.get_file(row.id)
    assert (got.status, got.size, got.mtime, got.codec) == ("working", 12, 3.0, "h264")
    assert not db.claim_file(row.id, 12, 3.0, 12, 3.0)


def test_english_only_column_defaults_off_and_old_rows_keep_their_value(tmp_path):
    db = Database(tmp_path / "defaults.db")
    kept = db.add_library("Kept", "kept", "low", True)
    db._exec("INSERT INTO libraries(name, path, level) VALUES ('New', 'new', 'low')")
    db.close()
    again = Database(tmp_path / "defaults.db")
    by_name = {l.name: l for l in again.list_libraries()}
    assert by_name["Kept"].id == kept.id and by_name["Kept"].english_only is True
    assert by_name["New"].english_only is False


def test_an_older_database_gains_the_conversion_time_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.replace("convert_seconds REAL,", ""))
    conn.execute("INSERT INTO runs(file_id, state, started_at) VALUES (1, 'done', 1.0)")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.history()[0]["convert_seconds"] is None
    db.update_run(1, convert_seconds=12.5)
    assert db.run_record(1)["convert_seconds"] == 12.5
    db.close()
    assert Database(path).run_record(1)["convert_seconds"] == 12.5


@pytest.mark.skipif(os.name != "nt", reason="drive letters are case-insensitive on Windows only")
def test_held_volumes_are_remembered_once_and_survive_a_restart(tmp_path):
    db = Database(tmp_path / "held.db")
    assert db.held_volumes() == []
    db.remember_held_volume("X:\\")
    db.remember_held_volume("x:\\")
    db.remember_held_volume("Y:\\")
    db.close()
    assert Database(tmp_path / "held.db").held_volumes() == ["X:\\", "Y:\\"]


@pytest.mark.skipif(os.name == "nt", reason="Linux twin of the drive-letter test")
def test_held_volumes_are_case_sensitive_on_linux(tmp_path):
    # Linux paths are case-sensitive, so these are three different folders, and each is kept.
    db = Database(tmp_path / "held.db")
    for volume in ("/mnt/a", "/mnt/A", "/mnt/a", "/mnt/b"):
        db.remember_held_volume(volume)
    db.close()
    assert sorted(Database(tmp_path / "held.db").held_volumes()) == ["/mnt/A", "/mnt/a", "/mnt/b"]


def test_a_damaged_held_volume_list_is_replaced_not_fatal(tmp_path):
    db = Database(tmp_path / "held.db")
    db.set_setting("held_volumes", "not json")
    assert db.held_volumes() == []
    db.remember_held_volume("X:\\")
    assert db.held_volumes() == ["X:\\"]


def test_timeline_merges_runs_and_runless_files_newest_first(db, lib):
    def at(file_id, when):
        db._exec("UPDATE files SET updated_at=? WHERE id=?", (when, file_id))

    skipped = db.upsert_file(lib.id, "s.mkv", 1, 1.0, "skipped", "Already small")
    failed = db.upsert_file(lib.id, "f.mkv", 1, 1.0, "failed", "Couldn't read this file")
    db.upsert_file(lib.id, "q.mkv", 1, 1.0, "queued")
    done = db.upsert_file(lib.id, "d.mkv", 1, 1.0, "converted")
    run = db.start_run(done.id, 1, "H.264", None, "why")
    db.update_run(run.id, state="done", started_at=100.0, finished_at=200.0)
    at(skipped.id, 300.0)
    at(failed.id, 150.0)
    at(done.id, 999.0)  # a run is placed by its own time, not its file's
    keys = [(i["kind"], i["id"]) for i in db.timeline()]
    assert keys == [("file", skipped.id), ("run", run.id), ("file", failed.id)]
    assert [(i["kind"], i["id"]) for i in db.timeline(limit=1, offset=1)] == [("run", run.id)]
    assert [i["id"] for i in db.timeline(kind="run")] == [run.id]
    assert [i["id"] for i in db.timeline(kind="file")] == [skipped.id, failed.id]
    assert db.timeline(library_id=lib.id + 1) == []


def test_timeline_shows_a_file_with_a_run_only_as_its_run(db, lib):
    f = db.upsert_file(lib.id, "a.mkv", 1, 1.0, "skipped", "Would not save enough")
    run = db.start_run(f.id, 1, "H.264", None, "why")
    assert [(i["kind"], i["id"], i["status"]) for i in db.timeline()] == [
        ("run", run.id, "skipped")]


def test_going_missing_remembers_the_status_for_a_rename(db):
    lib = db.add_library("Movies", "D:\\Movies", "medium", False)
    row = db.upsert_file(lib.id, "D:\\Movies\\a.mkv", 5, 1.5, "failed", "Why")
    assert db.set_missing_unless_owned(row.id)
    assert (db.get_file(row.id).status, db.get_file(row.id).reason) == ("missing", None)
    moved = db.follow_rename(row.id, "D:\\Movies\\b.mkv")
    assert (moved.path, moved.status, moved.reason) == ("D:\\Movies\\b.mkv", "failed", "Why")


def test_a_rename_of_a_file_that_is_not_missing_leaves_its_status(db):
    lib = db.add_library("Movies", "D:\\Movies", "medium", False)
    row = db.upsert_file(lib.id, "D:\\Movies\\a.mkv", 5, 1.5, "converted")
    moved = db.follow_rename(row.id, "D:\\Movies\\b.mkv")
    assert (moved.path, moved.status) == ("D:\\Movies\\b.mkv", "converted")


def test_a_rename_is_refused_while_the_worker_owns_the_file(db):
    lib = db.add_library("Movies", "D:\\Movies", "medium", False)
    row = db.upsert_file(lib.id, "D:\\Movies\\a.mkv", 5, 1.5, "working")
    assert db.follow_rename(row.id, "D:\\Movies\\b.mkv") is None
    assert db.get_file(row.id).path == "D:\\Movies\\a.mkv"


def test_files_are_found_by_size_and_time_within_one_library(db):
    one = db.add_library("One", "D:\\One", "medium", False)
    two = db.add_library("Two", "D:\\Two", "medium", False)
    wanted = db.upsert_file(one.id, "D:\\One\\a.mkv", 5, 1.5, "queued")
    db.upsert_file(one.id, "D:\\One\\b.mkv", 6, 1.5, "queued")
    db.upsert_file(one.id, "D:\\One\\c.mkv", 5, 2.5, "queued")
    db.upsert_file(two.id, "D:\\Two\\a.mkv", 5, 1.5, "queued")
    assert [r.id for r in db.files_with_size_and_time(one.id, 5, 1.5)] == [wanted.id]


def test_an_older_database_gains_the_remembered_status_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.replace("was_status TEXT,", "").replace("was_reason TEXT,", ""))
    conn.execute("INSERT INTO files(library_id, path, size, mtime, status, updated_at)"
                 " VALUES (1, 'D:\\a.mkv', 5, 1.5, 'missing', 1.0)")
    conn.commit()
    conn.close()
    db = Database(path)
    moved = db.follow_rename(1, "D:\\b.mkv")
    assert (moved.path, moved.status) == ("D:\\b.mkv", "missing")
    db.close()
    assert Database(path).get_file(1).path == "D:\\b.mkv"


def test_an_older_database_gains_the_shrink_h265_column_switched_off(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.replace("    shrink_h265 INTEGER NOT NULL DEFAULT 0,\n", ""))
    conn.execute("INSERT INTO libraries(name, path, level) VALUES ('Old', 'old', 'low')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.list_libraries()[0].shrink_h265 is False
    db.update_library(1, shrink_h265=True)
    assert db.list_libraries()[0].shrink_h265 is True
    db.close()


def test_shrink_h265_is_saved_with_a_new_library(db):
    assert db.add_library("TV", r"D:\TV", "medium", False, shrink_h265=True).shrink_h265 is True
    assert db.add_library("Kids", r"D:\Kids", "medium", False).shrink_h265 is False


def test_forgetting_saving_results_only_touches_h265_files_that_were_tried(db, lib):
    def row(name, reason, codec="hevc", status="skipped"):
        return db.upsert_file(lib.id, rf"D:\Movies\{name}.mkv", 1, 1.0, status, reason,
                              codec=codec).id
    sampled = row("a", "Sample showed too little saving")
    final = row("b", "Would not save enough")
    h264 = row("c", "Would not save enough", codec="h264")
    other = row("d", "Read-only file")
    db.forget_saving_results(lib.id)
    reasons = {i: db.get_file(i).reason for i in (sampled, final, h264, other)}
    assert reasons == {sampled: "Already H.265", final: "Already H.265",
                       h264: "Would not save enough", other: "Read-only file"}


@pytest.mark.skipif(os.name == "nt", reason="Linux-only behaviour")
def test_the_database_files_are_private_to_their_owner(tmp_path):
    """The database holds the MCP token in plain text, so a permissive umask must not expose it."""
    old = os.umask(0o022)
    try:
        db = Database(tmp_path / "data" / "shelfsaver.db")
        db.set_setting("mcp_token", "secret")      # makes the -wal side file appear
    finally:
        os.umask(old)
    for suffix in ("", "-wal", "-shm"):
        side = tmp_path / "data" / f"shelfsaver.db{suffix}"
        if side.exists():
            assert stat.S_IMODE(side.stat().st_mode) == 0o600, side.name


@pytest.mark.skipif(os.name == "nt", reason="Linux-only behaviour")
def test_a_share_that_refuses_chmod_does_not_stop_startup(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("not allowed")

    monkeypatch.setattr(os, "chmod", refuse)
    Database(tmp_path / "data" / "shelfsaver.db").set_setting("a", "b")


def test_an_older_database_gains_the_remove_commentary_column_switched_off(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.replace("    remove_commentary INTEGER NOT NULL DEFAULT 0,\n", ""))
    conn.execute("INSERT INTO libraries(name, path, level, english_only) VALUES ('TV', 'tv', 'low', 1)")
    conn.commit()
    conn.close()
    db = Database(path)
    kept = db.list_libraries()[0]
    assert kept.remove_commentary is False and kept.english_only is True  # nothing else changed
    db.update_library(kept.id, remove_commentary=True)
    assert db.list_libraries()[0].remove_commentary is True
    db.close()
    assert Database(path).list_libraries()[0].remove_commentary is True


def test_remove_commentary_is_saved_with_a_new_library_and_starts_off(db):
    assert db.add_library("TV", r"D:\TV", "medium", False, remove_commentary=True).remove_commentary is True
    assert db.add_library("Kids", r"D:\Kids", "medium", False).remove_commentary is False
