"""The web API. Fake worker and scanner for route tests, real ones for one integration test."""
from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shelfsaver import hostos, swap, worker as worker_module
from shelfsaver.__main__ import acquire_instance_lock
from shelfsaver.config import Config
from shelfsaver.db import Database, ensure_mcp_token
from shelfsaver.scanner import Scanner
from shelfsaver.web import app as app_module, folders
from shelfsaver.web.app import SESSION_COOKIE, create_app
from shelfsaver.worker import Worker, _dump_swap
from tests.helpers import make_video

REAL_VOLUME_ROOT = swap._volume_root
PASSWORD = "correct horse battery"


def make_config(tmp_path) -> Config:
    """A config whose media and scratch folders may be anywhere under tmp_path.

    Linux refuses folders until media roots are configured (see web/folders.py), so the tests
    configure the temp folder as the root. Windows keeps its normal drive-letter mode, which
    is the behaviour these tests were written against.
    """
    roots = [] if hostos.IS_WINDOWS else [str(tmp_path)]
    return Config(data_dir=tmp_path / "data", media_roots=list(roots), scratch_roots=list(roots))


class FakeWorker:
    def __init__(self):
        self.calls = []
        self.paused = False
        self._holding_lock = threading.Lock()
        self._parked = None
        self.resolve_result = ("resolved", None)

    def status(self):
        if self.paused:
            return {"state": "paused", "message": None, "file": None, "step": None,
                    "percent": None, "eta_seconds": None}
        return {"state": "idle", "message": None, "file": None, "step": None,
                "percent": None, "eta_seconds": None}

    def pause(self, reason=None):
        self.paused = True
        self.calls.append("pause")

    def resume(self):
        self.paused = False
        self.calls.append("resume")

    def retry(self, file_id):
        self.calls.append(("retry", file_id))

    def resolve_swap(self, file_id):
        self.calls.append(("resolve", file_id))
        return self.resolve_result

    clear_holding_when_idle = Worker.clear_holding_when_idle


class FakeScanner:
    def __init__(self):
        self.calls = []
        self.happened = threading.Event()
        self.state = {"scanning": False, "pending": 0}

    def status(self):
        return self.state

    def _note(self, *call):
        self.calls.append(call)
        self.happened.set()

    def scan_library(self, library_id):
        self._note("scan", library_id)

    def refresh_watches(self):
        self.calls.append(("watches",))

    def reevaluate(self, library_id):
        self._note("reevaluate", library_id)

    def wait(self, timeout=5):
        assert self.happened.wait(timeout), "the scanner was never called"
        self.happened.clear()


@pytest.fixture
def parts(tmp_path):
    db = Database(tmp_path / "data" / "shelfsaver.db")
    worker = FakeWorker()
    worker.db = db
    return db, worker, FakeScanner(), make_config(tmp_path)


@pytest.fixture(autouse=True)
def fake_volume(tmp_path, monkeypatch):
    """Keep every ShelfSaver work folder under tmp_path, never on the real drive."""
    volume = tmp_path / "volume"
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(volume) + os.sep)
    return volume


@pytest.fixture
def client(parts):
    db, worker, scanner, config = parts
    with TestClient(create_app(db, worker, scanner, config)) as c:
        yield c


@pytest.fixture
def authed(client):
    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    return client


def make_folder(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def add_library(authed, folder, name="Movies", **extra):
    return authed.post("/api/libraries", json={
        "name": name, "path": str(folder), "level": "medium", **extra})


# ---- sign-in

def test_everything_requires_sign_in(client):
    assert client.get("/api/status").status_code == 401
    for method, url in [("get", "/api/history"), ("get", "/api/libraries"), ("get", "/api/queue"),
                        ("get", "/api/settings"), ("get", "/api/browse"),
                        ("post", "/api/pause"), ("post", "/api/holding/clear"),
                        ("post", "/api/files/1/retry"), ("post", "/api/files/1/resolve"),
                        ("post", "/api/files/1/delete"), ("post", "/api/files/1/ignore")]:
        assert getattr(client, method)(url).status_code == 401, url


def test_queue_shows_waiting_files_next_first(authed, parts):
    db, *_ = parts
    lib = db.add_library("TV", "F:\\TV", "medium", False)
    db.upsert_file(lib.id, "F:\\TV\\small.mkv", 100, 1.0, "queued", resolution="640x480")
    db.upsert_file(lib.id, "F:\\TV\\big.mkv", 300, 1.0, "queued", resolution="1920x1080")
    db.upsert_file(lib.id, "F:\\TV\\done.mkv", 900, 1.0, "converted")
    body = authed.get("/api/queue").json()
    assert body["total_files"] == 2 and body["total_bytes"] == 400
    assert [r["name"] for r in body["items"]] == ["big.mkv", "small.mkv"]
    assert body["items"][0] == {"id": body["items"][0]["id"], "name": "big.mkv", "library": "TV",
                                "size": 300, "resolution": "1920x1080"}


def test_queue_pages_through_a_long_line(authed, parts):
    db, *_ = parts
    lib = db.add_library("TV", "F:\\TV", "medium", False)
    for size in (100, 200, 300):
        db.upsert_file(lib.id, f"F:\\TV\\{size}.mkv", size, 1.0, "queued")
    body = authed.get("/api/queue", params={"limit": 1, "offset": 1}).json()
    assert [r["size"] for r in body["items"]] == [200] and body["total_files"] == 3
    assert authed.get("/api/queue", params={"limit": 0}).status_code == 400
    assert authed.get("/api/queue", params={"offset": -1}).status_code == 400


def test_every_page_links_to_the_queue(client):
    for page in ("/", "/library", "/settings", "/queue"):
        assert 'href="/queue"' in client.get(page).text, page


def test_first_visit_sets_password_once(client):
    assert client.get("/api/auth/state").json() == {"has_password": False, "signed_in": False}
    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    state = client.get("/api/auth/state").json()
    assert state == {"has_password": True, "signed_in": True}
    again = client.post("/api/auth/setup", json={"password": "something else entirely"})
    assert again.status_code == 409
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"password": "something else entirely"}
                       ).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


def test_short_password_refused(client):
    r = client.post("/api/auth/setup", json={"password": "short"})
    assert r.status_code == 400
    assert "8" in r.json()["detail"]
    assert client.get("/api/auth/state").json()["has_password"] is False


def test_session_cookie_flags_and_logout(client, parts):
    r = client.post("/api/auth/setup", json={"password": PASSWORD})
    header = r.headers["set-cookie"].lower()
    assert SESSION_COOKIE in header and "httponly" in header and "samesite=strict" in header
    assert client.get("/api/status").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/status").status_code == 401


def test_sessions_survive_restart_and_expire(parts, tmp_path):
    db, worker, scanner, config = parts
    with TestClient(create_app(db, worker, scanner, config)) as first:
        first.post("/api/auth/setup", json={"password": PASSWORD})
        token = first.cookies.get(SESSION_COOKIE)
    db2 = Database(db.path)
    with TestClient(create_app(db2, worker, scanner, config)) as second:
        second.cookies.set(SESSION_COOKIE, token)
        assert second.get("/api/status").status_code == 200
    # Expire every session in the database.
    db2._exec("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    with TestClient(create_app(db2, worker, scanner, config)) as third:
        third.cookies.set(SESSION_COOKIE, token)
        assert third.get("/api/status").status_code == 401


def test_wrong_password_rejected_and_rate_limited(client):
    client.post("/api/auth/setup", json={"password": PASSWORD})
    client.post("/api/auth/logout")
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "nope nope nope"}
                           ).status_code == 401
    blocked = client.post("/api/auth/login", json={"password": "nope nope nope"})
    assert blocked.status_code == 429
    # Even the right password waits out the minute.
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


def test_password_and_tokens_never_appear_in_responses(authed, parts):
    db, *_ = parts
    stored = db.get_setting("password_hash")
    token = authed.cookies.get(SESSION_COOKIE)
    bodies = [authed.get(u).text for u in (
        "/api/auth/state", "/api/status", "/api/settings", "/api/libraries", "/api/history")]
    for body in bodies:
        assert stored not in body and PASSWORD not in body and token not in body
    assert "password_hash" not in authed.get("/api/settings").json()


# ---- libraries

def test_add_library_queues_a_scan(authed, parts, tmp_path):
    _, _, scanner, _ = parts
    folder = make_folder(tmp_path, "movies")
    r = add_library(authed, folder, name="  Movies  ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Movies" and body["level"] == "medium" and body["english_only"] is False
    scanner.wait()
    assert ("scan", body["id"]) in scanner.calls
    assert ("watches",) in scanner.calls
    listed = authed.get("/api/libraries").json()
    assert [l["id"] for l in listed] == [body["id"]]


@pytest.mark.parametrize("bad", ["\\\\nas\\movies", "Z:\\does\\not\\exist"])
def test_add_library_rejects_with_plain_message(authed, bad):
    r = authed.post("/api/libraries", json={"name": "X", "path": bad, "level": "low"})
    assert r.status_code == 400
    assert authed.get("/api/libraries").json() == []
    assert len(r.json()["detail"]) > 10


@pytest.mark.skipif(not hostos.IS_WINDOWS, reason="drive letters; see the volume test below")
def test_add_library_rejects_a_whole_drive(authed, tmp_path, monkeypatch):
    monkeypatch.setattr(swap, "_volume_root", REAL_VOLUME_ROOT)  # a read-only lookup
    root = os.path.splitdrive(str(tmp_path))[0] + "\\"
    r = authed.post("/api/libraries", json={"name": "All", "path": root, "level": "low"})
    assert r.status_code == 400
    assert r.json()["detail"] == "Choose a folder on the drive, not the whole drive."


@pytest.mark.skipif(hostos.IS_WINDOWS, reason="Linux twin of the whole-drive test")
def test_add_library_rejects_the_top_of_a_mounted_volume(authed, tmp_path, monkeypatch):
    # The work folder lives at the top of the volume, so a library there would contain it.
    volume = make_folder(tmp_path, "volume")
    r = authed.post("/api/libraries", json={"name": "All", "path": str(volume), "level": "low"})
    assert r.status_code == 400
    assert r.json()["detail"] == folders.WHOLE_DRIVE


def test_add_library_rejects_bad_name_level_and_file(authed, tmp_path):
    folder = make_folder(tmp_path, "movies")
    assert authed.post("/api/libraries", json={"name": "   ", "path": str(folder),
                                              "level": "low"}).status_code == 400
    assert authed.post("/api/libraries", json={"name": "A", "path": str(folder),
                                              "level": "extreme"}).status_code == 400
    afile = folder / "a.txt"
    afile.write_text("x")
    assert authed.post("/api/libraries", json={"name": "A", "path": str(afile),
                                              "level": "low"}).status_code == 400
    assert authed.post("/api/libraries", json={"name": "A"}).status_code == 400


def test_overlapping_libraries_rejected(authed, tmp_path):
    outer = make_folder(tmp_path, "movies")
    inner = make_folder(tmp_path, "movies/kids")
    assert add_library(authed, outer).status_code == 200
    assert add_library(authed, inner, name="Kids").status_code == 400
    assert add_library(authed, outer, name="Again").status_code == 400
    other = make_folder(tmp_path, "movies2")  # a sibling that shares a name prefix is fine
    assert add_library(authed, other, name="Sibling").status_code == 200
    third = make_folder(tmp_path, "shows")
    assert add_library(authed, third, name="Shows").status_code == 200
    assert add_library(authed, tmp_path, name="Parent").status_code == 400


def test_library_cannot_be_the_scratch_folder(authed, tmp_path):
    scratch = make_folder(tmp_path, "scratch")
    assert authed.patch("/api/settings", json={"scratch_dir": str(scratch)}).status_code == 200
    r = add_library(authed, scratch)
    assert r.status_code == 400 and "scratch" in r.json()["detail"].lower()


def test_scratch_inside_library_rejected(authed, tmp_path):
    library = make_folder(tmp_path, "movies")
    inside = make_folder(tmp_path, "movies/tmp")
    assert add_library(authed, library).status_code == 200
    r = authed.patch("/api/settings", json={"scratch_dir": str(inside)})
    assert r.status_code == 400
    assert authed.get("/api/settings").json()["scratch_dir"] in (None, "")
    r = authed.patch("/api/settings", json={"scratch_dir": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_scratch_must_be_a_writable_folder(authed, tmp_path):
    afile = tmp_path / "file.txt"
    afile.write_text("x")
    assert authed.patch("/api/settings", json={"scratch_dir": str(afile)}).status_code == 400
    assert authed.patch("/api/settings", json={"scratch_dir": "\\\\nas\\scratch"}
                        ).status_code == 400
    good = make_folder(tmp_path, "scratch")
    r = authed.patch("/api/settings", json={"scratch_dir": str(good), "holding_enabled": False})
    assert r.status_code == 200
    s = authed.get("/api/settings").json()
    assert s["scratch_dir"] == str(good) and s["holding_enabled"] is False
    assert list(good.iterdir()) == []  # the writable check leaves nothing behind


def test_scratch_change_refused_while_converting(authed, parts, tmp_path):
    db, *_ = parts
    lib = db.add_library("M", str(make_folder(tmp_path, "movies")), "low", True)
    f = db.upsert_file(lib.id, str(tmp_path / "movies" / "a.mkv"), 1, 1.0, "working")
    db.start_run(f.id, 1, "H.264", None, "why")
    r = authed.patch("/api/settings", json={"scratch_dir": str(make_folder(tmp_path, "s"))})
    assert r.status_code == 400


def test_level_change_triggers_reevaluate(authed, parts, tmp_path):
    _, _, scanner, _ = parts
    lib = add_library(authed, make_folder(tmp_path, "movies")).json()
    scanner.wait()
    r = authed.patch(f"/api/libraries/{lib['id']}", json={"level": "high"})
    assert r.status_code == 200 and r.json()["level"] == "high"
    scanner.wait()
    assert ("reevaluate", lib["id"]) in scanner.calls
    scanner.calls.clear()
    authed.patch(f"/api/libraries/{lib['id']}", json={"english_only": True})
    scanner.wait()
    assert ("reevaluate", lib["id"]) in scanner.calls
    scanner.calls.clear()
    authed.patch(f"/api/libraries/{lib['id']}", json={"name": "Renamed"})
    time.sleep(0.1)
    assert scanner.calls == []
    assert authed.patch(f"/api/libraries/{lib['id']}", json={"level": "x"}).status_code == 400
    assert authed.patch("/api/libraries/999", json={"level": "low"}).status_code == 404


def test_removing_library_leaves_files_alone(authed, parts, tmp_path):
    db, _, scanner, _ = parts
    folder = make_folder(tmp_path, "movies")
    video = folder / "a.mkv"
    video.write_bytes(b"data")
    lib = add_library(authed, folder).json()
    scanner.wait()
    db.upsert_file(lib["id"], str(video), 4, 1.0, "queued")
    scanner.calls.clear()
    assert authed.delete(f"/api/libraries/{lib['id']}").status_code == 200
    assert video.read_bytes() == b"data"
    assert authed.get("/api/libraries").json() == []
    assert db.get_file_by_path(str(video)) is None
    assert ("watches",) in scanner.calls
    assert authed.delete(f"/api/libraries/{lib['id']}").status_code == 404


@pytest.mark.parametrize("state", ["running", "swapping", "waiting_in_use"])
def test_cannot_remove_library_while_a_file_is_being_finished(authed, parts, tmp_path, state):
    db, _, scanner, _ = parts
    lib = add_library(authed, make_folder(tmp_path, "movies")).json()
    scanner.wait()
    f = db.upsert_file(lib["id"], str(tmp_path / "movies" / "a.mkv"), 1, 1.0, "working")
    run = db.start_run(f.id, 1, "H.264", None, "why")
    db.update_run(run.id, state=state)
    r = authed.delete(f"/api/libraries/{lib['id']}")
    assert r.status_code == 400 and "still being finished" in r.json()["detail"]
    assert len(authed.get("/api/libraries").json()) == 1


# ---- browse

def test_browse_lists_folders_not_files(authed, tmp_path):
    base = make_folder(tmp_path, "base")
    make_folder(base, "b_folder")
    make_folder(base, "A_folder")
    (base / "file.mkv").write_bytes(b"x")
    r = authed.get("/api/browse", params={"path": str(base)})
    assert r.status_code == 200
    body = r.json()
    assert [f["name"] for f in body["folders"]] == ["A_folder", "b_folder"]
    assert body["folders"][0]["path"] == str(base / "A_folder")
    assert body["parent"] == str(tmp_path)


@pytest.mark.skipif(not hostos.IS_WINDOWS, reason="drive letters")
def test_browse_without_a_path_shows_drives(authed, tmp_path):
    body = authed.get("/api/browse").json()
    drive = os.path.splitdrive(str(tmp_path))[0].upper() + "\\"
    assert drive in [f["path"].upper() for f in body["folders"]]
    assert body["path"] is None and body["parent"] is None


def test_browse_skips_folders_it_cannot_read_and_rejects_bad_paths(authed, tmp_path, monkeypatch):
    base = make_folder(tmp_path, "base")
    make_folder(base, "ok")
    make_folder(base, "locked")
    import shelfsaver.web.app as app_module

    original = folders.is_visible_folder

    def flaky(entry):
        if entry.name == "locked":
            raise PermissionError("denied")
        return original(entry)

    monkeypatch.setattr(folders, "is_visible_folder", flaky)
    names = [f["name"] for f in authed.get("/api/browse", params={"path": str(base)}
                                           ).json()["folders"]]
    assert names == ["ok"]
    assert authed.get("/api/browse", params={"path": str(tmp_path / "missing")}
                      ).status_code == 400
    assert authed.get("/api/browse", params={"path": "\\\\nas\\share"}).status_code == 400
    assert authed.get("/api/browse", params={"path": "relative"}).status_code == 400


# ---- history, files, status

def seed_run(db, tmp_path, state="done", file_status="converted", reason=None):
    lib = db.add_library("Movies", str(tmp_path / "movies"), "medium", True)
    f = db.upsert_file(lib.id, str(tmp_path / "movies" / "Film.mkv"), 500, 1.0, file_status,
                       reason)
    run = db.start_run(f.id, 1000, "H.264", 4000, "Big and old")
    db.update_run(run.id, state=state, now_size=500, now_format="H.265", saved_bytes=500,
                  swap_json='{"original": "x"}', tracks_json='[{"kind": "audio"}]',
                  checks_json='{"length": true}', error="boom" if state == "failed" else None)
    return lib, f, run


def test_history_shape_has_was_now_saved(authed, parts, tmp_path):
    db, *_ = parts
    lib, f, run = seed_run(db, tmp_path)
    rows = authed.get("/api/history").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["file_id"] == f.id and row["name"] == "Film.mkv"
    assert row["library"] == "Movies"
    assert row["was"] == {"size": 1000, "format": "H.264", "bitrate": 4000}
    assert row["now"]["size"] == 500 and row["now"]["format"] == "H.265"
    assert row["saved_bytes"] == 500 and row["status"] == "converted"
    assert "swap_json" not in row
    assert "now_path" in row
    assert authed.get("/api/history", params={"library_id": lib.id + 1}).json() == []
    assert authed.get("/api/history", params={"limit": 0}).status_code == 400


def test_history_does_not_call_interrupted_runs_failures(authed, parts, tmp_path):
    db, *_ = parts
    lib, f, run = seed_run(db, tmp_path, state="interrupted", file_status="queued")
    db.update_run(run.id, error="Interrupted by a restart, so it will start again")
    row = authed.get("/api/history").json()[0]
    assert row["state"] == "interrupted" and row["status"] == "queued"
    assert row["reason"] is None


def test_history_uses_the_files_reason(authed, parts, tmp_path):
    db, *_ = parts
    seed_run(db, tmp_path, state="failed", file_status="failed", reason="The conversion failed")
    row = authed.get("/api/history").json()[0]
    assert row["status"] == "failed" and row["reason"] == "The conversion failed"


def test_there_are_no_blackouts_by_default(authed):
    assert authed.get("/api/settings").json()["blackouts"] == []


def test_blackouts_are_saved_and_can_be_cleared(authed):
    chosen = [{"days": [4, 0], "start": "18:00", "end": "23:00"}]
    tidy = [{"days": [0, 4], "start": "18:00", "end": "23:00"}]
    r = authed.patch("/api/settings", json={"blackouts": chosen})
    assert r.status_code == 200 and r.json()["blackouts"] == tidy
    assert authed.get("/api/settings").json()["blackouts"] == tidy
    assert authed.patch("/api/settings", json={"blackouts": []}).json()["blackouts"] == []
    assert authed.get("/api/settings").json()["blackouts"] == []


def test_changing_another_setting_leaves_blackouts_alone(authed):
    chosen = [{"days": [0], "start": "18:00", "end": "23:00"}]
    authed.patch("/api/settings", json={"blackouts": chosen})
    authed.patch("/api/settings", json={"holding_enabled": True})
    assert authed.get("/api/settings").json()["blackouts"] == chosen


def test_a_blackout_that_makes_no_sense_is_refused_with_a_reason(authed):
    r = authed.patch("/api/settings", json={
        "blackouts": [{"days": [0], "start": "18:00", "end": "18:00"}]})
    assert r.status_code == 400 and "same" in r.json()["detail"]
    assert authed.get("/api/settings").json()["blackouts"] == []


def test_blackouts_need_sign_in(client):
    r = client.patch("/api/settings", json={"blackouts": []})
    assert r.status_code == 401


def test_the_default_language_starts_as_english(authed):
    body = authed.get("/api/settings").json()
    assert body["default_language"] == "eng"
    assert {"code": "eng", "name": "English"} in body["languages"]
    assert body["language_name"] == "English"


def test_the_default_language_can_be_changed(authed):
    r = authed.patch("/api/settings", json={"default_language": "spa"})
    assert r.status_code == 200
    assert r.json()["default_language"] == "spa" and r.json()["language_name"] == "Spanish"
    assert authed.get("/api/settings").json()["default_language"] == "spa"


def test_an_unlisted_default_language_is_refused_with_a_reason(authed):
    r = authed.patch("/api/settings", json={"default_language": "klingon"})
    assert r.status_code == 400 and "Choose a language" in r.json()["detail"]
    assert authed.get("/api/settings").json()["default_language"] == "eng"


def test_changing_the_language_rechecks_only_libraries_that_remove_tracks(authed, parts, tmp_path):
    _, _, scanner, _ = parts
    off = add_library(authed, make_folder(tmp_path, "movies")).json()
    scanner.wait()
    on = add_library(authed, make_folder(tmp_path, "tv"), name="TV", english_only=True).json()
    scanner.wait()
    time.sleep(0.1)
    scanner.calls.clear()
    assert authed.get("/api/settings").json()["language_libraries"] == ["TV"]
    authed.patch("/api/settings", json={"default_language": "spa"})
    scanner.wait()
    time.sleep(0.1)
    assert ("reevaluate", on["id"]) in scanner.calls
    assert ("reevaluate", off["id"]) not in scanner.calls


def test_saving_the_same_language_rechecks_nothing(authed, parts, tmp_path):
    _, _, scanner, _ = parts
    add_library(authed, make_folder(tmp_path, "tv"), name="TV", english_only=True)
    scanner.wait()
    time.sleep(0.1)
    scanner.calls.clear()
    authed.patch("/api/settings", json={"default_language": "eng"})
    time.sleep(0.1)
    assert scanner.calls == []


def test_system_use_needs_sign_in(client):
    assert client.get("/api/system").status_code == 401


def test_system_use_reports_each_number(authed, monkeypatch):
    monkeypatch.setattr(app_module.SystemUsage, "read",
                        lambda self: {"cpu": 12, "gpu": None, "ram": 34})
    assert authed.get("/api/system").json() == {"cpu": 12, "gpu": None, "ram": 34}


def test_file_details_hides_internal_fields(authed, parts, tmp_path):
    db, *_ = parts
    lib, f, run = seed_run(db, tmp_path)
    body = authed.get(f"/api/files/{f.id}").json()
    assert body["file"]["id"] == f.id and body["file"]["path"].endswith("Film.mkv")
    assert body["runs"][0]["tracks"] == [{"kind": "audio"}]
    assert body["runs"][0]["checks"] == {"length": True}
    assert "swap_json" not in body["runs"][0]
    assert authed.get("/api/files/9999").status_code == 404


def test_file_details_include_what_ffmpeg_said(authed, parts, tmp_path):
    db, *_ = parts
    lib, f, run = seed_run(db, tmp_path, state="failed", file_status="failed", reason="x")
    db.update_run(run.id, log_tail="Playback check:\nInvalid data")
    body = authed.get(f"/api/files/{f.id}").json()
    assert body["runs"][0]["log_tail"] == "Playback check:\nInvalid data"


def test_retry_goes_through_the_worker(authed, parts, tmp_path):
    db, worker, *_ = parts
    lib, f, run = seed_run(db, tmp_path, state="failed", file_status="failed", reason="x")
    assert authed.post(f"/api/files/{f.id}/retry").status_code == 200
    assert worker.calls == [("retry", f.id)]
    assert db.get_file(f.id).status == "failed"  # the worker decides, not the route
    assert authed.post("/api/files/9999/retry").status_code == 404


def test_retry_refused_for_a_file_that_is_not_stuck(authed, parts, tmp_path):
    db, worker, *_ = parts
    lib, f, run = seed_run(db, tmp_path)
    r = authed.post(f"/api/files/{f.id}/retry")
    assert r.status_code == 400 and worker.calls == []


def test_resolve_needs_confirm_and_a_kept_swap(authed, parts, tmp_path):
    db, worker, *_ = parts
    lib, f, run = seed_run(db, tmp_path, state="swapping", file_status="failed", reason="Kept")
    assert authed.post(f"/api/files/{f.id}/resolve", json={}).status_code == 400
    assert authed.post(f"/api/files/{f.id}/resolve", json={"confirm": "yes"}).status_code == 400
    assert authed.post(f"/api/files/{f.id}/resolve").status_code == 400
    assert worker.calls == []
    assert authed.post(f"/api/files/{f.id}/resolve", json={"confirm": True}).status_code == 200
    assert worker.calls == [("resolve", f.id)]
    worker.resolve_result = ("none", None)
    r = authed.post(f"/api/files/{f.id}/resolve", json={"confirm": True})
    assert r.status_code == 400 and r.json()["detail"]
    worker.resolve_result = ("held", "X:\\held\\old.mkv")
    r = authed.post(f"/api/files/{f.id}/resolve", json={"confirm": True})
    assert r.status_code == 400 and "X:\\held\\old.mkv" in r.json()["detail"]
    worker.resolve_result = ("busy", None)
    assert authed.post(f"/api/files/{f.id}/resolve", json={"confirm": True}).status_code == 409
    assert authed.post("/api/files/9999/resolve", json={"confirm": True}).status_code == 404


def test_status_reports_worker_stats_held_and_offline(authed, parts, tmp_path):
    db, worker, *_ = parts
    lib, f, run = seed_run(db, tmp_path)
    db.update_library(lib.id, status="offline")
    body = authed.get("/api/status").json()
    assert body["worker"]["state"] == "idle"
    assert body["no_scratch"] is True
    assert body["stats"]["saved_bytes"] == 500
    assert body["held_bytes"] == 0
    assert body["offline_libraries"] == [{"id": lib.id, "name": "Movies"}]
    worker.pause()
    assert authed.get("/api/status").json()["worker"]["state"] == "paused"
    scratch = make_folder(tmp_path, "scratch")
    authed.patch("/api/settings", json={"scratch_dir": str(scratch)})
    assert authed.get("/api/status").json()["no_scratch"] is False


def test_status_passes_on_the_workers_blocked_message(authed, parts):
    _, worker, *_ = parts
    worker.status = lambda: {"state": "blocked", "message": "Not enough room on the scratch disk"}
    body = authed.get("/api/status").json()
    assert body["worker"]["state"] == "blocked"
    assert body["message"] == "Not enough room on the scratch disk"


def test_pause_and_resume(authed, parts):
    _, worker, *_ = parts
    assert authed.post("/api/pause").json()["worker"]["state"] == "paused"
    assert authed.post("/api/resume").json()["worker"]["state"] == "idle"
    assert worker.calls == ["pause", "resume"]


# ---- holding

def test_clear_holding_requires_confirm(authed):
    assert authed.post("/api/holding/clear", json={}).status_code == 400
    assert authed.post("/api/holding/clear", json={"confirm": False}).status_code == 400
    assert authed.post("/api/holding/clear").status_code == 400


def hold_a_file(library_folder: Path) -> Path:
    holding = swap.work_root(library_folder) / "holding"
    holding.mkdir(parents=True, exist_ok=True)
    held = holding / "old.mkv"
    held.write_bytes(b"x" * 2048)
    return held


def test_clear_holding_frees_space_and_names_the_drive(authed, parts, tmp_path):
    folder = make_folder(tmp_path, "movies")
    add_library(authed, folder)
    held = hold_a_file(folder)
    try:
        status = authed.get("/api/status").json()
        assert status["held_bytes"] == 2048
        settings = authed.get("/api/settings").json()
        assert settings["held_bytes"] == 2048
        assert settings["held_libraries"] == ["Movies"]
        r = authed.post("/api/holding/clear", json={"confirm": True})
        assert r.status_code == 200
        body = r.json()
        assert body["freed_bytes"] == 2048 and body["libraries"] == ["Movies"]
        assert body["drives"] == [str(swap.work_root(folder).parent)]
        assert not held.exists()
    finally:
        if held.exists():
            held.unlink()


@pytest.mark.parametrize("state", ["running", "swapping", "waiting_in_use"])
def test_clear_holding_refused_while_a_run_is_active(authed, parts, tmp_path, state):
    db, *_ = parts
    folder = make_folder(tmp_path, "movies")
    lib = add_library(authed, folder).json()
    held = hold_a_file(folder)
    try:
        f = db.upsert_file(lib["id"], str(folder / "a.mkv"), 1, 1.0, "working")
        run = db.start_run(f.id, 1, "H.264", None, "why")
        db.update_run(run.id, state=state)
        r = authed.post("/api/holding/clear", json={"confirm": True})
        assert r.status_code == 400 and "only copy" in r.json()["detail"]
        assert held.exists()
    finally:
        if held.exists():
            held.unlink()


# ---- settings and the MCP token

def test_settings_include_the_mcp_token_and_command(authed, parts):
    db, *_ = parts
    body = authed.get("/api/settings").json()
    token = body["mcp_token"]
    assert token and token == ensure_mcp_token(db)
    assert authed.get("/api/settings").json()["mcp_token"] == token
    command = body["mcp_command"]
    assert command.startswith("claude mcp add")
    assert token in command and "testserver" in command
    other = authed.get("/api/settings", headers={"host": "192.168.1.5:8642"}).json()
    assert "http://192.168.1.5:8642/" in other["mcp_command"]
    assert body["holding_enabled"] is True


def test_ensure_mcp_token_is_stable(tmp_path):
    db = Database(tmp_path / "t.db")
    first = ensure_mcp_token(db)
    assert len(first) >= 40 and ensure_mcp_token(db) == first


def test_validation_errors_are_400_with_a_plain_detail(authed):
    r = authed.post("/api/libraries", content=b"not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)


# ---- the real worker and scanner

def test_real_worker_and_scanner_behind_the_api(tmp_path):
    db = Database(tmp_path / "data" / "shelfsaver.db")
    config = make_config(tmp_path)
    worker = Worker(db, config)
    scanner = Scanner(db, config, stable_seconds=0)
    folder = make_folder(tmp_path, "movies")
    make_video(folder / "Clip.mkv", seconds=2, size="1280x720", crf=1)
    with TestClient(create_app(db, worker, scanner, config)) as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        lib = add_library(c, folder).json()
        deadline = time.time() + 30
        while time.time() < deadline and c.get("/api/status").json()["stats"]["remaining"] < 1:
            time.sleep(0.1)
        assert c.get("/api/status").json()["stats"]["remaining"] == 1
        assert c.get("/api/libraries").json()[0]["stats"]["remaining"] == 1
        assert c.get("/api/libraries").json()[0]["stats"]["total"] == 1

        worker.process_one()  # no scratch disk yet
        status = c.get("/api/status").json()
        assert status["no_scratch"] is True and status["worker"]["state"] == "no_scratch"
        assert status["message"] == "Choose a scratch disk in Settings"

        assert c.patch(f"/api/libraries/{lib['id']}", json={"level": "high"}).status_code == 200
        c.post("/api/pause")
        assert c.get("/api/status").json()["worker"]["state"] == "paused"
        c.post("/api/resume")
        assert c.get("/api/status").json()["worker"]["state"] != "paused"


def test_resolve_swap_ends_a_kept_swap_without_touching_files(tmp_path):
    db = Database(tmp_path / "data" / "shelfsaver.db")
    worker = Worker(db, make_config(tmp_path))
    original = tmp_path / "movies" / "a.mkv"
    original.parent.mkdir()
    original.write_bytes(b"orig")
    lib = db.add_library("M", str(original.parent), "low", True)
    f = db.upsert_file(lib.id, str(original), 4, 1.0, "failed", "Couldn't finish")
    run = db.start_run(f.id, 4, "H.264", None, "why")
    db.update_run(run.id, state="swapping", swap_json="{}")
    assert worker.resolve_swap(f.id) == ("resolved", None)
    assert db.active_runs(("swapping", "waiting_in_use", "running")) == []
    after = db.get_file(f.id)
    assert after.status == "failed" and "Nothing was deleted" in after.reason
    assert original.read_bytes() == b"orig"
    assert worker.resolve_swap(f.id) == ("none", None)
    # A file that is not stuck is refused too.
    g = db.upsert_file(lib.id, str(tmp_path / "movies" / "b.mkv"), 1, 1.0, "queued")
    assert worker.resolve_swap(g.id) == ("none", None)


# ---- fix round 1

def kept_swap(tmp_path, original_exists, held_exists, plan_ok=True):
    """A kept swap whose original is (or is not) in place and whose copy is (or is not) held."""
    db = Database(tmp_path / "data" / "shelfsaver.db")
    worker = Worker(db, make_config(tmp_path))
    folder = make_folder(tmp_path, "movies")
    original = folder / "a.mkv"
    held = tmp_path / "volume" / "ShelfSaver" / "holding" / "M" / "a.mkv"
    held.parent.mkdir(parents=True)
    if original_exists:
        original.write_bytes(b"orig")
    if held_exists:
        held.write_bytes(b"only copy")
    lib = db.add_library("M", str(folder), "low", True)
    f = db.upsert_file(lib.id, str(original), 4, 1.0, "failed", "Couldn't finish")
    run = db.start_run(f.id, 4, "H.264", None, "why")
    plan = swap.SwapPlan(str(original), str(tmp_path / "volume" / "ShelfSaver" / "incoming"
                                           / "1-1.mkv"), str(original), str(held), 3, 1)
    db.update_run(run.id, state="swapping",
                  swap_json=_dump_swap(plan, 4, 1.0) if plan_ok else "garbage")
    return db, worker, f, run, original, held


def test_resolve_refused_when_the_only_copy_is_held(tmp_path):
    db, worker, f, run, original, held = kept_swap(tmp_path, False, True)
    outcome, where = worker.resolve_swap(f.id)
    assert outcome == "held" and where == str(held)
    assert db.get_file(f.id).reason == "Couldn't finish"
    assert db.active_runs(("swapping",))  # unchanged
    assert held.read_bytes() == b"only copy"


def test_resolve_refused_with_an_unreadable_plan_while_the_original_is_missing(tmp_path):
    db, worker, f, run, original, held = kept_swap(tmp_path, False, True, plan_ok=False)
    assert worker.resolve_swap(f.id) == ("held", None)
    original.write_bytes(b"back")
    assert worker.resolve_swap(f.id) == ("resolved", None)


def test_resolve_allowed_when_the_original_is_in_place(tmp_path):
    db, worker, f, run, original, held = kept_swap(tmp_path, True, True)
    assert worker.resolve_swap(f.id) == ("resolved", None)
    assert held.exists() and original.exists()


def test_resolve_then_clear_cannot_delete_the_only_copy(authed, parts, tmp_path):
    db, _, scanner, config = parts
    folder = make_folder(tmp_path, "movies")
    lib = add_library(authed, folder).json()
    scanner.wait()
    held = tmp_path / "volume" / "ShelfSaver" / "holding" / "M" / "a.mkv"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"only copy")
    row = db.upsert_file(lib["id"], str(folder / "a.mkv"), 4, 1.0, "failed", "Kept")
    run = db.start_run(row.id, 4, "H.264", None, "why")
    plan = swap.SwapPlan(str(folder / "a.mkv"), str(tmp_path / "t.mkv"), str(folder / "a.mkv"),
                         str(held), 3, 1)
    db.update_run(run.id, state="swapping", swap_json=_dump_swap(plan, 4, 1.0))
    with TestClient(create_app(db, Worker(db, config), scanner, config)) as c:
        c.cookies.update(authed.cookies)
        assert c.post(f"/api/files/{row.id}/resolve", json={"confirm": True}).status_code == 400
        assert c.post("/api/holding/clear", json={"confirm": True}).status_code == 400
    assert held.read_bytes() == b"only copy"


def real_worker_with_library(authed, parts, tmp_path):
    db, _, scanner, config = parts
    folder = make_folder(tmp_path, "movies")
    lib = add_library(authed, folder).json()
    scanner.wait()
    return db, Worker(db, config), folder, lib


def parked_job(parts, tmp_path, monkeypatch):
    """A real worker holding a converted file that is waiting for room on the library drive."""
    db, _, scanner, config = parts
    worker = Worker(db, config)
    folder = make_folder(tmp_path, "movies")
    original = folder / "a.mkv"
    original.write_bytes(b"ORIGINAL")
    info = os.stat(original)
    lib = db.add_library("M", str(folder), "low", True)
    row = db.upsert_file(lib.id, str(original), info.st_size, info.st_mtime, "working")
    run = db.start_run(row.id, info.st_size, "H.264", None, "why")
    scratch = make_folder(tmp_path, "scratch/run")
    output = scratch / "output.mkv"
    output.write_bytes(b"NEW")
    job = worker_module._Job(row, lib, info.st_size, info.st_mtime, None, None, run, scratch,
                             output)
    monkeypatch.setattr(worker_module, "free_bytes", lambda path: 0)
    assert worker._place_new_file(job) is False
    assert worker._parked is job and db.run_record(run.id)["state"] == "running"
    return db, worker, job, folder, original


def test_clear_blocks_the_start_of_a_swap_until_it_is_done(parts, tmp_path, monkeypatch):
    db, worker, job, folder, original = parked_job(parts, tmp_path, monkeypatch)
    release, inside = threading.Event(), threading.Event()

    def slow_clear(paths):
        inside.set()
        assert release.wait(10)
        return 7

    monkeypatch.setattr(worker_module, "clear_holding", slow_clear)
    result = []
    clearer = threading.Thread(target=lambda: result.append(
        worker.clear_holding_when_idle([str(folder)], ("running", "swapping"))))
    clearer.start()
    assert inside.wait(5)
    # Room appears, and the real step that begins the swap tries to go ahead.
    monkeypatch.setattr(worker_module, "free_bytes", lambda path: 10 ** 12)
    placing = threading.Thread(target=lambda: worker._place_new_file(job))
    placing.start()
    placing.join(1.0)
    assert placing.is_alive()  # waiting for the clear
    assert db.run_record(job.run.id)["state"] == "running"
    assert original.read_bytes() == b"ORIGINAL"
    release.set()
    clearer.join(10)
    placing.join(20)
    assert result == [7] and not placing.is_alive()
    assert db.run_record(job.run.id)["state"] == "done"
    assert original.read_bytes() == b"NEW"


def test_a_parked_run_does_not_block_the_clear_but_a_running_one_does(
        parts, tmp_path, monkeypatch):
    db, worker, job, folder, original = parked_job(parts, tmp_path, monkeypatch)
    held = hold_a_file(folder)
    paths = [str(folder)]
    assert worker.clear_holding_when_idle(paths, ("running", "swapping")) == 2048
    assert not held.exists()
    worker._parked = None  # the same run, no longer parked, is converting
    held = hold_a_file(folder)
    assert worker.clear_holding_when_idle(paths, ("running", "swapping")) is None
    assert held.exists()


def test_clear_ignores_a_parked_job_that_vanishes_mid_check(parts, tmp_path, monkeypatch):
    db, worker, job, folder, original = parked_job(parts, tmp_path, monkeypatch)
    real_active = db.active_runs

    def active(states):
        worker._parked = None  # the worker drops it between the checks
        return real_active(states)

    monkeypatch.setattr(db, "active_runs", active)
    assert worker.clear_holding_when_idle([str(folder)], ("running",)) == 0  # it read the job once, so it is still treated as parked


def attempt_that_waits(worker, db, run, finish_state=None):
    """Start an attempt on `run` that stays under way until released."""
    started, release = threading.Event(), threading.Event()

    def steps(r):
        started.set()
        assert release.wait(10)
        if finish_state:
            db.update_run(r.id, state=finish_state)

    worker._advance_swap_steps = steps
    thread = threading.Thread(target=lambda: worker._advance_swap(run))
    thread.start()
    assert started.wait(5)
    return thread, release


def test_resolve_does_nothing_while_an_attempt_is_under_way(tmp_path):
    db, worker, f, run, original, held = kept_swap(tmp_path, True, True)
    thread, release = attempt_that_waits(worker, db, db.active_runs(("swapping",))[0], "done")
    assert worker.resolve_swap(f.id) == ("busy", None)
    assert db.run_record(run.id)["state"] == "swapping"
    release.set()
    thread.join(5)
    # The attempt finished the swap; the later answer must not overwrite it.
    assert db.run_record(run.id)["state"] == "done"
    assert worker.resolve_swap(f.id) == ("none", None)
    assert db.run_record(run.id)["state"] == "done"


def test_an_attempt_cannot_start_during_a_resolve_or_after_it(tmp_path):
    db, worker, f, run, original, held = kept_swap(tmp_path, True, True)
    stale = db.active_runs(("swapping",))[0]
    attempted = []
    worker._advance_swap_steps = lambda r: attempted.append(r.id)
    inside, release = threading.Event(), threading.Event()
    real_check = worker._only_copy_may_be_held

    def slow_check(r, file):
        inside.set()
        assert release.wait(10)
        return real_check(r, file)

    worker._only_copy_may_be_held = slow_check
    outcome = []
    resolver = threading.Thread(target=lambda: outcome.append(worker.resolve_swap(f.id)))
    resolver.start()
    assert inside.wait(5)
    worker._advance_swap(stale)  # a retry that was already picked up
    assert attempted == []
    release.set()
    resolver.join(5)
    assert outcome == [("resolved", None)]
    worker._advance_swap(stale)  # and one with the old record, after the resolve
    assert attempted == []


def test_a_plan_with_a_bad_holding_field_counts_as_unreadable(tmp_path):
    db, worker, f, run, original, held = kept_swap(tmp_path, False, True)
    broken = json.loads(db.run_record(run.id)["swap_json"])
    broken["holding"] = 12345
    db.update_run(run.id, swap_json=json.dumps(broken))
    assert worker.resolve_swap(f.id) == ("held", None)
    original.write_bytes(b"back")
    assert worker.resolve_swap(f.id) == ("resolved", None)


def test_clear_ignores_runs_on_other_volumes(parts, tmp_path, monkeypatch):
    db, _, scanner, config = parts
    worker = Worker(db, config)
    a, b = make_folder(tmp_path, "vol_a/movies"), make_folder(tmp_path, "vol_b/movies")
    monkeypatch.setattr(swap, "_volume_root",
                        lambda path: str(tmp_path / ("vol_a" if "vol_a" in str(path)
                                                     else "vol_b")) + os.sep)
    db.add_library("A", str(a), "low", True)
    lb = db.add_library("B", str(b), "low", True)
    f = db.upsert_file(lb.id, str(b / "x.mkv"), 1, 1.0, "working")
    db.start_run(f.id, 1, "H.264", None, "why")
    assert worker.clear_holding_when_idle([str(a)], ("running",)) == 0
    assert worker.clear_holding_when_idle([str(b)], ("running",)) is None


def test_busy_message_suggests_pausing(authed, parts, tmp_path):
    db, *_ = parts
    lib = add_library(authed, make_folder(tmp_path, "movies")).json()
    f = db.upsert_file(lib["id"], str(tmp_path / "movies" / "a.mkv"), 1, 1.0, "working")
    db.start_run(f.id, 1, "H.264", None, "why")
    r = authed.post("/api/holding/clear", json={"confirm": True})
    assert r.status_code == 400 and "pause it" in r.json()["detail"]


def test_library_and_scratch_cannot_be_inside_the_work_folder(authed, tmp_path):
    holding = tmp_path / "volume" / "ShelfSaver" / "holding" / "M"
    holding.mkdir(parents=True)
    for target in (tmp_path / "volume" / "ShelfSaver", holding):
        r = add_library(authed, target)
        assert r.status_code == 400 and "ShelfSaver folder" in r.json()["detail"]
        r = authed.patch("/api/settings", json={"scratch_dir": str(target)})
        assert r.status_code == 400 and "ShelfSaver folder" in r.json()["detail"]
    sibling = make_folder(tmp_path, "volume/ShelfSaver2")
    assert add_library(authed, sibling).status_code == 200


def test_interrupted_run_never_shows_as_failed(authed, parts, tmp_path):
    db, *_ = parts
    lib, f, run = seed_run(db, tmp_path, state="interrupted", file_status="failed",
                           reason="The conversion failed")
    db.update_run(run.id, error="Interrupted by a restart, so it will start again")
    row = authed.get("/api/history").json()[0]
    assert row["state"] == "interrupted" and row["status"] == "interrupted"
    assert row["reason"] == "Interrupted by a restart, so it will start again"


def test_mcp_command_ignores_an_unsafe_host_header(authed):
    body = authed.get("/api/settings", headers={"host": 'x" ; rm -rf /'}).json()
    assert "rm -rf" not in body["mcp_command"] and "YOUR-PC:8642" in body["mcp_command"]
    ok = authed.get("/api/settings", headers={"host": "my-pc.local:9000"}).json()
    assert "http://my-pc.local:9000/mcp" in ok["mcp_command"]


# ---- one copy of the app at a time

def test_second_instance_is_refused_and_a_dead_one_does_not_block(tmp_path):
    data = tmp_path / "lockdata"
    first = acquire_instance_lock(data)
    assert first is not None
    assert acquire_instance_lock(data) is None
    first.close()
    again = acquire_instance_lock(data)
    assert again is not None
    again.close()
    # A process that dies while holding the lock must not block the next start.
    code = ("import sys, pathlib, time\n"
            "from shelfsaver.__main__ import acquire_instance_lock\n"
            "h = acquire_instance_lock(pathlib.Path(sys.argv[1]))\n"
            "print('locked' if h else 'refused', flush=True)\ntime.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-c", code, str(data)], stdout=subprocess.PIPE,
                             text=True, cwd=Path(__file__).resolve().parent.parent)
    try:
        assert child.stdout.readline().strip() == "locked"
        assert acquire_instance_lock(data) is None
    finally:
        child.kill()
        child.wait()
    after = None
    for _ in range(50):  # Windows lets go of a dead process's locks a moment after it exits
        after = acquire_instance_lock(data)
        if after is not None:
            break
        time.sleep(0.1)
    assert after is not None
    after.close()


# ---- web pages

STATIC = Path(__file__).resolve().parent.parent / "shelfsaver" / "web" / "static"


@pytest.mark.parametrize("route,marker", [
    ("/", "history"), ("/library", "library"), ("/settings", "Settings"), ("/login", "password")])
def test_pages_are_served(client, route, marker):
    # The pages carry no private data, so they load before sign-in; the data calls need it.
    response = client.get(route)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert marker in response.text
    assert "/static/app.js" in response.text
    for asset in ("/static/app.js", "/static/style.css"):
        assert client.get(asset).status_code == 200


def test_no_inner_html_in_app_js():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in app_js
    assert "outerHTML" not in app_js
    assert "insertAdjacentHTML" not in app_js


def test_pages_need_no_internet():
    for page in STATIC.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, page.name


def test_add_library_dialog_starts_with_english_only_off():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "englishCheckbox(false)" in app_js and "englishCheckbox(true)" not in app_js


# ---- files the scanner decided about, which never had a run

def file_only(db, lib, name, status, reason=None, when=None, codec="hevc"):
    row = db.upsert_file(lib.id, str(Path("library") / name), 2_000_000_000, 1.0, status, reason,
                         codec=codec, resolution="1920x1080")
    if when is not None:
        db._exec("UPDATE files SET updated_at=? WHERE id=?", (when, row.id))
    return row


@pytest.mark.parametrize("status,reason", [
    ("skipped", "Already H.265"), ("skipped", "Already small"), ("skipped", "Dolby Vision, left as is"),
    ("skipped", "Linked to another copy"), ("failed", "Couldn't read this file"),
    ("waiting_in_use", "Waiting, file is in use"), ("missing", None)])
def test_history_shows_files_that_never_had_a_run(authed, parts, status, reason):
    db, *_ = parts
    lib = db.add_library("Movies", "library", "medium", False)
    f = file_only(db, lib, "Film.mkv", status, reason)
    rows = authed.get("/api/history").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "file" and row["id"] == f"file-{f.id}" and row["file_id"] == f.id
    assert row["name"] == "Film.mkv" and row["library"] == "Movies"
    assert row["status"] == status and row["reason"] == reason
    assert row["was"] == {"size": 2_000_000_000, "format": "H.265", "bitrate": None}
    assert row["now"] == {"size": None, "format": None, "bitrate": None}
    assert row["state"] is None and row["saved_bytes"] == 0
    assert row["updated_at"] == db.get_file(f.id).updated_at  # shown as when it was scanned
    details = authed.get(f"/api/files/{f.id}").json()
    assert details["runs"] == [] and details["file"]["reason"] == reason


def test_a_file_with_no_known_format_shows_none_rather_than_a_made_up_one(authed, parts):
    db, *_ = parts
    lib = db.add_library("Movies", "library", "medium", False)
    file_only(db, lib, "bad.mkv", "failed", "Couldn't read this file", codec=None)
    assert authed.get("/api/history").json()[0]["was"]["format"] is None


def test_history_leaves_out_queued_and_working_files(authed, parts):
    db, *_ = parts
    lib = db.add_library("Movies", "library", "medium", False)
    file_only(db, lib, "a.mkv", "queued")
    file_only(db, lib, "b.mkv", "working")
    assert authed.get("/api/history").json() == []


def test_history_sorts_and_pages_across_both_kinds(authed, parts):
    db, *_ = parts
    lib = db.add_library("Movies", "library", "medium", False)
    other = db.add_library("TV", "other", "medium", False)
    for number, when in enumerate((400.0, 200.0)):
        done = db.upsert_file(lib.id, f"library/run{number}.mkv", 500, 1.0, "converted")
        run = db.start_run(done.id, 1000, "H.264", None, "why")
        db.update_run(run.id, state="done", started_at=when - 50, finished_at=when)
    file_only(db, lib, "newest.mkv", "skipped", "Already H.265", 500.0)
    file_only(db, lib, "middle.mkv", "failed", "Couldn't read this file", 300.0)
    file_only(db, other, "elsewhere.mkv", "skipped", "Already H.265", 100.0)

    def names(**params):
        return [r["name"] for r in authed.get("/api/history", params=params).json()]

    everything = ["newest.mkv", "run0.mkv", "middle.mkv", "run1.mkv", "elsewhere.mkv"]
    assert names() == everything
    assert names(limit=2) == everything[:2]
    assert names(limit=2, offset=2) == everything[2:4]
    assert names(limit=2, offset=4) == everything[4:]
    assert names(library_id=other.id) == ["elsewhere.mkv"]
    assert names(library_id=lib.id, limit=1, offset=1) == ["run0.mkv"]


def test_history_filters_on_the_server_so_paging_can_reach_every_match(authed, parts):
    db, *_ = parts
    lib = db.add_library("Movies", "library", "medium", False)
    file_only(db, lib, "old-failure.mkv", "failed", "Couldn't read this file", 1.0)
    for number in range(5):
        file_only(db, lib, f"s{number}.mkv", "skipped", "Already H.265", 100.0 + number)
    done = db.upsert_file(lib.id, "library/done.mkv", 500, 1.0, "converted")
    db.update_run(db.start_run(done.id, 1000, "H.264", None, "why").id, state="done",
                  finished_at=50.0)
    stopped = db.upsert_file(lib.id, "library/stopped.mkv", 500, 1.0, "failed", "x")
    db.update_run(db.start_run(stopped.id, 1000, "H.264", None, "why").id, state="interrupted",
                  error="Interrupted", finished_at=60.0)

    def names(show, **params):
        rows = authed.get("/api/history", params={"show": show, **params}).json()
        return [r["name"] for r in rows]

    assert names("conversions") == ["stopped.mkv", "done.mkv"]
    assert names("scans", limit=2, offset=1) == ["s3.mkv", "s2.mkv"]
    assert names("scans")[-1] == "old-failure.mkv"
    assert len(names("all")) == 8
    assert authed.get("/api/history", params={"show": "nonsense"}).status_code == 400


def test_home_and_library_pages_have_room_for_the_history_tabs(client):
    for page in ("/", "/library"):
        assert 'id="filters"' in client.get(page).text, page
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "Conversion history" in app_js and "Scan history" in app_js


# ---- deleting a failed file

def failed_file(db, folder, name="bad.mkv", status="failed", content=b"damaged"):
    library = db.add_library("TV", str(folder), "medium", False)
    path = folder / name
    path.write_bytes(content)
    st = path.stat()
    row = db.upsert_file(library.id, str(path), st.st_size, st.st_mtime, status, "Couldn't read this file")
    return path, row


def delete_file(authed, row, **body):
    return authed.post(f"/api/files/{row.id}/delete", json={"confirm": True, **body})


def test_deleting_a_failed_file_removes_it_from_disk_and_the_list(authed, parts, tmp_path, caplog):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"), content=b"")
    with caplog.at_level(logging.INFO):
        response = delete_file(authed, row)
    assert response.status_code == 200
    assert not path.exists() and db.get_file(row.id) is None
    assert any(str(path) in r.getMessage() for r in caplog.records)


def test_deleting_needs_confirmation(authed, parts, tmp_path):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"))
    assert authed.post(f"/api/files/{row.id}/delete").status_code == 400
    assert authed.post(f"/api/files/{row.id}/delete", json={"confirm": False}).status_code == 400
    assert path.exists() and db.get_file(row.id) is not None


def test_only_failed_files_can_be_deleted(authed, parts, tmp_path):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"), status="queued")
    assert delete_file(authed, row).status_code == 400
    assert path.exists() and db.get_file(row.id).status == "queued"


def test_deleting_an_unknown_file_says_so(authed):
    assert authed.post("/api/files/999/delete", json={"confirm": True}).status_code == 404


@pytest.mark.parametrize("state", ["running", "swapping", "waiting_in_use"])
def test_a_file_with_unfinished_work_is_not_deleted(authed, parts, tmp_path, state):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"))
    db.update_run(db.start_run(row.id, 7, "H.264", None, "why").id, state=state)
    assert delete_file(authed, row).status_code == 409
    assert path.exists() and db.get_file(row.id) is not None


def test_a_file_that_changed_since_it_failed_is_not_deleted(authed, parts, tmp_path):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"))
    path.write_bytes(b"a fresh good copy of the episode")
    assert delete_file(authed, row).status_code == 409
    assert path.read_bytes() == b"a fresh good copy of the episode"
    assert db.get_file(row.id) is not None


@pytest.mark.skipif(not hostos.IS_WINDOWS,
                    reason="Linux lets you delete an open file; only Windows refuses")
def test_a_file_open_in_another_program_is_not_deleted(authed, parts, tmp_path):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"))
    with open(path, "rb"):
        assert delete_file(authed, row).status_code == 409
    assert path.exists() and db.get_file(row.id) is not None


def test_a_file_outside_its_library_is_not_deleted(authed, parts, tmp_path):
    db, *_ = parts
    folder = make_folder(tmp_path, "tv")
    library = db.add_library("TV", str(folder), "medium", False)
    for outside in (tmp_path / "other.mkv", folder / ".." / "sneaky.mkv"):
        outside.write_bytes(b"not in the library")
        st = outside.stat()
        row = db.upsert_file(library.id, str(outside), st.st_size, st.st_mtime, "failed", "x")
        assert delete_file(authed, row).status_code == 400
        assert outside.exists() and db.get_file(row.id) is not None


def test_a_folder_at_the_files_path_is_not_deleted(authed, parts, tmp_path):
    db, *_ = parts
    folder = make_folder(tmp_path, "tv")
    library = db.add_library("TV", str(folder), "medium", False)
    inner = make_folder(folder, "looks-like-a-file.mkv")
    st = inner.stat()
    row = db.upsert_file(library.id, str(inner), st.st_size, st.st_mtime, "failed", "x")
    assert delete_file(authed, row).status_code == 400
    assert inner.is_dir() and db.get_file(row.id) is not None


def test_an_offline_library_keeps_its_row(authed, parts, tmp_path):
    db, *_ = parts
    folder = make_folder(tmp_path, "tv")
    path, row = failed_file(db, folder)
    path.unlink()
    folder.rmdir()
    assert delete_file(authed, row).status_code == 400
    assert db.get_file(row.id) is not None


def test_a_file_already_gone_just_leaves_the_list(authed, parts, tmp_path):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"))
    path.unlink()
    assert delete_file(authed, row).status_code == 200
    assert db.get_file(row.id) is None


def test_past_attempts_stay_in_conversion_history_after_deleting(authed, parts, tmp_path):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"))
    run = db.start_run(row.id, 7, "H.264", None, "why")
    db.update_run(run.id, state="failed", error="The conversion failed", finished_at=5.0)
    assert delete_file(authed, row).status_code == 200
    rows = authed.get("/api/history", params={"show": "conversions"}).json()
    assert [r["name"] for r in rows] == ["bad.mkv"]
    assert authed.get("/api/history", params={"show": "scans"}).json() == []


# ---- ignoring a failed file

IGNORED = "Ignored, left as is"


def ignore_file(authed, row):
    return authed.post(f"/api/files/{row.id}/ignore")


def test_ignoring_a_failed_file_keeps_it_and_skips_it(authed, parts, tmp_path):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"))
    assert ignore_file(authed, row).status_code == 200
    after = db.get_file(row.id)
    assert path.read_bytes() == b"damaged"
    assert after.status == "skipped" and after.reason == IGNORED


def test_only_failed_files_can_be_ignored(authed, parts, tmp_path):
    db, *_ = parts
    _, row = failed_file(db, make_folder(tmp_path, "tv"), status="queued")
    assert ignore_file(authed, row).status_code == 400
    assert db.get_file(row.id).status == "queued"
    assert authed.post("/api/files/999/ignore").status_code == 404


def test_a_file_with_unfinished_work_is_not_ignored(authed, parts, tmp_path):
    db, *_ = parts
    _, row = failed_file(db, make_folder(tmp_path, "tv"))
    db.update_run(db.start_run(row.id, 7, "H.264", None, "why").id, state="swapping")
    assert ignore_file(authed, row).status_code == 409
    assert db.get_file(row.id).status == "failed"


def test_a_new_copy_is_not_ignored_by_mistake(authed, parts, tmp_path):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"))
    path.write_bytes(b"a fresh good copy of the episode")
    assert ignore_file(authed, row).status_code == 409
    assert db.get_file(row.id).status == "failed"


def test_a_file_that_is_gone_is_not_ignored(authed, parts, tmp_path):
    db, *_ = parts
    path, row = failed_file(db, make_folder(tmp_path, "tv"))
    path.unlink()
    assert ignore_file(authed, row).status_code == 400
    assert db.get_file(row.id).status == "failed"


def test_an_ignored_file_can_be_brought_back(authed, parts, tmp_path):
    db, worker, *_ = parts
    _, row = failed_file(db, make_folder(tmp_path, "tv"))
    assert authed.post(f"/api/files/{row.id}/retry").status_code == 200
    db.set_file_status(row.id, "skipped", IGNORED)
    assert authed.post(f"/api/files/{row.id}/retry").status_code == 200
    db.set_file_status(row.id, "skipped", "Already H.265")
    assert authed.post(f"/api/files/{row.id}/retry").status_code == 400


def test_the_page_offers_ignore_and_a_way_back(client):
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/ignore" in app_js and "Stop ignoring" in app_js


def test_the_page_warns_before_deleting(client):
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/delete" in app_js and "no Recycle Bin" in app_js


def test_try_again_is_offered_for_a_scan_failure_but_not_a_skip(authed, parts):
    db, worker, *_ = parts
    lib = db.add_library("Movies", "library", "medium", False)
    bad = file_only(db, lib, "bad.mkv", "failed", "Couldn't read this file", codec=None)
    assert authed.post(f"/api/files/{bad.id}/retry").status_code == 200
    assert worker.calls == [("retry", bad.id)]
    skipped = file_only(db, lib, "fine.mkv", "skipped", "Already H.265")
    assert authed.post(f"/api/files/{skipped.id}/retry").status_code == 400


# ---- English-only starts off

def test_english_only_is_off_unless_asked_for(authed, tmp_path):
    body = add_library(authed, make_folder(tmp_path, "movies")).json()
    assert body["english_only"] is False
    body = add_library(authed, make_folder(tmp_path, "tv"), name="TV", english_only=True).json()
    assert body["english_only"] is True


# ---- held size on the status poll

def counted_held_size(monkeypatch, sizes):
    calls = []

    def fake(paths):
        calls.append(list(paths))
        return sizes[0]

    monkeypatch.setattr(swap, "held_size", fake)
    return calls


def test_status_does_not_measure_holding_on_every_poll(authed, monkeypatch):
    calls = counted_held_size(monkeypatch, [111])
    for _ in range(4):
        assert authed.get("/api/status").json()["held_bytes"] == 111
    assert len(calls) == 1


def test_held_size_is_measured_again_after_the_cache_runs_out(authed, monkeypatch):
    sizes = [111]
    calls = counted_held_size(monkeypatch, sizes)
    assert authed.get("/api/status").json()["held_bytes"] == 111
    sizes[0] = 222
    assert authed.get("/api/status").json()["held_bytes"] == 111
    monkeypatch.setattr(app_module, "HELD_CACHE_SECONDS", 0.0)
    assert authed.get("/api/status").json()["held_bytes"] == 222
    assert len(calls) == 2


def test_clearing_holding_shows_at_once_on_the_next_poll(authed, tmp_path):
    folder = make_folder(tmp_path, "movies")
    add_library(authed, folder)
    hold_a_file(folder)
    assert authed.get("/api/status").json()["held_bytes"] == 2048
    assert authed.post("/api/holding/clear", json={"confirm": True}).status_code == 200
    assert authed.get("/api/status").json()["held_bytes"] == 0


def test_adding_a_library_is_measured_at_once(authed, tmp_path):
    assert authed.get("/api/status").json()["held_bytes"] == 0
    folder = make_folder(tmp_path, "movies")
    hold_a_file(folder)
    add_library(authed, folder)
    assert authed.get("/api/status").json()["held_bytes"] == 2048


def test_settings_always_measure_holding_afresh(authed, tmp_path):
    folder = make_folder(tmp_path, "movies")
    add_library(authed, folder)
    assert authed.get("/api/status").json()["held_bytes"] == 0
    hold_a_file(folder)
    assert authed.get("/api/settings").json()["held_bytes"] == 2048
    assert authed.get("/api/status").json()["held_bytes"] == 2048


# ---- held files stay visible and clearable after their library is removed

def two_volumes(tmp_path, monkeypatch):
    a, b = make_folder(tmp_path, "vol_a/movies"), make_folder(tmp_path, "vol_b/movies")
    monkeypatch.setattr(swap, "_volume_root",
                        lambda path: str(tmp_path / ("vol_a" if "vol_a" in str(path)
                                                     else "vol_b")) + os.sep)
    return a, b


def test_removing_a_library_keeps_its_held_files_visible_and_clearable(
        authed, tmp_path, monkeypatch):
    a, b = two_volumes(tmp_path, monkeypatch)
    lib = add_library(authed, a).json()
    add_library(authed, b, name="Other")
    held = hold_a_file(a)
    assert authed.delete(f"/api/libraries/{lib['id']}").status_code == 200
    settings = authed.get("/api/settings").json()
    assert settings["held_bytes"] == 2048
    assert str(tmp_path / "vol_a") in settings["held_drives"]
    assert authed.get("/api/status").json()["held_bytes"] == 2048
    body = authed.post("/api/holding/clear", json={"confirm": True}).json()
    assert body["freed_bytes"] == 2048 and not held.exists()
    assert str(tmp_path / "vol_a") in body["drives"]
    assert body["held_bytes"] == 0


def test_a_library_from_before_this_list_existed_is_remembered_on_removal(
        authed, parts, tmp_path, monkeypatch):
    db, *_ = parts
    a, _ = two_volumes(tmp_path, monkeypatch)
    lib = db.add_library("Old", str(a), "low", False)  # never went through the add route
    assert db.held_volumes() == []
    hold_a_file(a)
    assert authed.delete(f"/api/libraries/{lib.id}").status_code == 200
    assert authed.get("/api/settings").json()["held_bytes"] == 2048


def test_clear_is_refused_when_a_library_on_a_remembered_volume_is_busy(
        authed, parts, tmp_path, monkeypatch):
    db, *_ = parts
    a, _ = two_volumes(tmp_path, monkeypatch)
    first = add_library(authed, a).json()
    held = hold_a_file(a)
    second_folder = make_folder(tmp_path, "vol_a/tv")
    second = add_library(authed, second_folder, name="TV").json()
    assert authed.delete(f"/api/libraries/{first['id']}").status_code == 200
    f = db.upsert_file(second["id"], str(second_folder / "x.mkv"), 1, 1.0, "working")
    db.start_run(f.id, 1, "H.264", None, "why")
    r = authed.post("/api/holding/clear", json={"confirm": True})
    assert r.status_code == 400 and held.exists()


def test_clear_is_refused_while_a_run_from_a_removed_library_is_active(
        authed, parts, tmp_path, monkeypatch):
    db, *_ = parts
    a, _ = two_volumes(tmp_path, monkeypatch)
    lib = add_library(authed, a).json()
    held = hold_a_file(a)
    f = db.upsert_file(lib["id"], str(a / "x.mkv"), 1, 1.0, "failed")
    run = db.start_run(f.id, 1, "H.264", None, "why")
    db.update_run(run.id, state="swapping")
    db.remove_library(lib["id"])  # as if it slipped past the busy check
    r = authed.post("/api/holding/clear", json={"confirm": True})
    assert r.status_code == 400 and held.exists()


# ---- the scanner on the status poll

def test_status_passes_on_the_scanners_progress(authed, parts):
    _, _, scanner, _ = parts
    assert authed.get("/api/status").json()["scanner"] == {"scanning": False, "pending": 0}
    scanner.state = {"scanning": True, "pending": 1204}
    assert authed.get("/api/status").json()["scanner"] == {"scanning": True, "pending": 1204}


# ---- wording on the pages

def test_pause_says_the_current_file_will_start_over():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "will start over when you resume" in app_js
    assert "will start over when you resume" in index


def test_home_says_when_it_is_looking_through_the_libraries():
    assert "Looking through your libraries" in (STATIC / "app.js").read_text(encoding="utf-8")


def test_conversion_time_comes_from_the_recorded_encode_time():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "run.convert_seconds" in app_js and "finished_at - run.started_at" not in app_js


def test_offline_notice_says_the_library_drive_is_offline():
    assert "library drive is offline" in (STATIC / "app.js").read_text(encoding="utf-8")


# ---- shrinking oversized H.265 files, per library

def test_shrink_h265_is_off_unless_asked_for(authed, tmp_path):
    assert add_library(authed, make_folder(tmp_path, "movies")).json()["shrink_h265"] is False
    body = add_library(authed, make_folder(tmp_path, "tv"), name="TV", shrink_h265=True).json()
    assert body["shrink_h265"] is True


def test_turning_shrink_h265_on_re_evaluates_the_library(authed, parts, tmp_path):
    _, _, scanner, _ = parts
    lib = add_library(authed, make_folder(tmp_path, "movies")).json()
    scanner.wait()
    scanner.calls.clear()
    r = authed.patch(f"/api/libraries/{lib['id']}", json={"shrink_h265": True})
    assert r.status_code == 200 and r.json()["shrink_h265"] is True
    scanner.wait()
    assert ("reevaluate", lib["id"]) in scanner.calls


def test_a_level_change_lets_tried_h265_files_be_sampled_again(authed, parts, tmp_path):
    db, _, scanner, _ = parts
    lib = add_library(authed, make_folder(tmp_path, "movies"), shrink_h265=True).json()
    scanner.wait()
    tried = db.upsert_file(lib["id"], str(tmp_path / "movies" / "a.mkv"), 1, 1.0, "skipped",
                           "Sample showed too little saving", codec="hevc")
    authed.patch(f"/api/libraries/{lib['id']}", json={"name": "Renamed"})
    assert db.get_file(tried.id).reason == "Sample showed too little saving"
    authed.patch(f"/api/libraries/{lib['id']}", json={"level": "high"})
    scanner.wait()
    assert db.get_file(tried.id).reason == "Already H.265"


def test_library_dialogs_offer_shrinking_h265_switched_off_for_new_libraries():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "shrinkCheckbox(false)" in app_js and "shrinkCheckbox(lib.shrink_h265)" in app_js
    assert "Shrink oversized H.265 files" in app_js
    assert "shrink_h265: shrink.input.checked" in app_js


# ---- Commentary removal starts off

def test_remove_commentary_is_off_unless_asked_for(authed, tmp_path):
    body = add_library(authed, make_folder(tmp_path, "movies")).json()
    assert body["remove_commentary"] is False
    body = add_library(authed, make_folder(tmp_path, "tv"), name="TV", remove_commentary=True).json()
    assert body["remove_commentary"] is True


def test_changing_commentary_removal_rechecks_the_library(authed, parts, tmp_path):
    _, _, scanner, _ = parts
    lib = add_library(authed, make_folder(tmp_path, "movies")).json()
    scanner.wait()
    time.sleep(0.1)
    scanner.calls.clear()
    r = authed.patch(f"/api/libraries/{lib['id']}", json={"remove_commentary": True})
    assert r.status_code == 200 and r.json()["remove_commentary"] is True
    scanner.wait()
    assert ("reevaluate", lib["id"]) in scanner.calls
    assert authed.get("/api/libraries").json()[0]["remove_commentary"] is True


def test_commentary_removal_only_accepts_a_real_yes_or_no(authed, tmp_path):
    lib = add_library(authed, make_folder(tmp_path, "movies")).json()
    assert authed.patch(f"/api/libraries/{lib['id']}", json={"remove_commentary": "yes"}).status_code == 400


def test_add_library_dialog_starts_with_commentary_removal_off():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "commentaryCheckbox(false)" in app_js and "commentaryCheckbox(true)" not in app_js


def test_the_commentary_option_says_what_it_does_and_that_it_cannot_be_undone():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "Remove commentary soundtracks" in app_js
    assert "The main soundtrack is never removed" in app_js
    assert "can't be recovered once the original is replaced" in app_js


def test_both_library_dialogs_send_the_commentary_setting():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert app_js.count("remove_commentary: commentary.input.checked") == 2
