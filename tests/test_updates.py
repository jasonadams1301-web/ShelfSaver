"""Checking GitHub for a newer release, and updating an installed Windows copy."""
from __future__ import annotations

import pytest

from shelfsaver.updates import check_latest, how_to_update, install_kind, parse_version


# ---- versions and the check

def test_versions_parse_and_compare():
    assert parse_version("v0.7.3") == (0, 7, 3) and parse_version("0.10.0") == (0, 10, 0)
    assert parse_version("v0.7") is None and parse_version("latest") is None
    assert parse_version("") is None
    assert parse_version("v0.10.0") > parse_version("v0.9.9")


def test_check_finds_a_newer_release():
    r = check_latest("0.7.1", lambda url: {"tag_name": "v0.7.2", "body": "Notes",
                                           "html_url": "https://x/v0.7.2"})
    assert (r.status, r.version, r.notes, r.url) == ("newer", "0.7.2", "Notes", "https://x/v0.7.2")


def test_check_keeps_notes_empty_when_github_has_none():
    r = check_latest("0.7.1", lambda url: {"tag_name": "v0.7.2", "body": None})
    assert r.status == "newer" and r.notes == ""


@pytest.mark.parametrize("tag", ["v0.7.1", "v0.7.0"])
def test_check_says_current_for_same_or_older(tag):
    r = check_latest("0.7.1", lambda url: {"tag_name": tag})
    assert r.status == "current" and r.version == "0.7.1"


@pytest.mark.parametrize("answer", [OSError("down"), TimeoutError(), ValueError("not json"),
                                    {"message": "API rate limit exceeded"}, {"tag_name": "nightly"},
                                    {"tag_name": 7}, []])
def test_check_handles_github_errors(answer):
    def fetch(url):
        if isinstance(answer, BaseException):
            raise answer
        return answer
    r = check_latest("0.7.1", fetch)
    assert r.status == "failed" and r.message.startswith("Couldn't check GitHub")


def test_check_asks_the_releases_latest_address():
    seen = []
    check_latest("0.7.1", lambda url: seen.append(url) or {"tag_name": "v0.7.1"})
    assert seen == ["https://api.github.com/repos/jasonadams1301-web/ShelfSaver/releases/latest"]


# ---- what kind of copy this is

PF = r"C:\Program Files"


@pytest.mark.parametrize("kw, kind", [
    (dict(is_windows=True, prefix=PF + r"\ShelfSaver\venv", program_files=PF, env={}), "windows"),
    (dict(is_windows=True, prefix=r"c:\program files\shelfsaver\VENV", program_files=PF, env={}),
     "windows"),
    (dict(is_windows=True, prefix=r"H:\VideoCompress\.venv", program_files=PF, env={}), "dev"),
    (dict(is_windows=True, prefix=PF + r"\ShelfSaver\venv", program_files="", env={}), "dev"),
    (dict(is_windows=False, prefix="/opt/venv", program_files="", env={"SHELFSAVER_DOCKER": "1"}),
     "docker"),
    (dict(is_windows=False, prefix="/opt/shelfsaver/venv", program_files="", env={}), "linux"),
])
def test_install_kind(kw, kind):
    assert install_kind(**kw) == kind


def test_how_to_update_per_kind():
    assert how_to_update("windows", "0.7.2", "u") is None
    assert "ghcr.io/jasonadams1301-web/shelfsaver:0.7.2" in how_to_update("docker", "0.7.2", "u")
    assert "install.sh" in how_to_update("linux", "0.7.2", "u")
    assert "wasn't installed by setup" in how_to_update("dev", "0.7.2", "u")


# ---- download and unpack

import io  # noqa: E402
import zipfile  # noqa: E402

from shelfsaver.updates import UpdateError, download, extract, prepare  # noqa: E402

INIT = '"""ShelfSaver."""\n\n__version__ = "{}"\n'


def make_release_zip(version: str, inner_version: str = None, extra: dict = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        top = f"ShelfSaver-{version}/"
        z.writestr(top + "shelfsaver/__init__.py", INIT.format(inner_version or version))
        z.writestr(top + "scripts/install.ps1", "# installer\n")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buffer.getvalue()


def serving(data: bytes, seen: list = None):
    def fetch_stream(url):
        if seen is not None:
            seen.append(url)
        return io.BytesIO(data)
    return fetch_stream


def test_prepare_unpacks_and_checks_the_version(tmp_path):
    seen = []
    root = prepare("0.7.2", tmp_path / "update", serving(make_release_zip("0.7.2"), seen))
    assert (root / "scripts" / "install.ps1").is_file()
    assert root.parent == tmp_path / "update" / "v0.7.2"
    assert seen == ["https://github.com/jasonadams1301-web/ShelfSaver/archive/refs/tags/v0.7.2.zip"]


def test_prepare_clears_an_older_download(tmp_path):
    old = tmp_path / "update" / "v0.7.1"
    old.mkdir(parents=True)
    (old / "leftover.txt").write_text("x")
    prepare("0.7.2", tmp_path / "update", serving(make_release_zip("0.7.2")))
    assert not old.exists()


def test_prepare_refuses_a_version_mismatch(tmp_path):
    with pytest.raises(UpdateError, match="0.7.2"):
        prepare("0.7.2", tmp_path / "update", serving(make_release_zip("0.7.2", inner_version="0.7.1")))


@pytest.mark.parametrize("bad", ["../evil.txt", "ShelfSaver-0.7.2/../../evil.txt", "C:/evil.txt",
                                 "/evil.txt"])
def test_extract_refuses_paths_outside_the_folder(tmp_path, bad):
    archive = tmp_path / "r.zip"
    archive.write_bytes(make_release_zip("0.7.2", extra={bad: "boom"}))
    dest = tmp_path / "deep" / "dest"
    with pytest.raises(UpdateError):
        extract(archive, dest)
    assert not list(tmp_path.rglob("evil.txt"))


def test_extract_needs_exactly_one_top_folder(tmp_path):
    archive = tmp_path / "r.zip"
    archive.write_bytes(make_release_zip("0.7.2", extra={"Other/readme.txt": "x"}))
    with pytest.raises(UpdateError):
        extract(archive, tmp_path / "dest")


def test_download_stops_at_the_size_cap(tmp_path):
    dest = tmp_path / "r.zip"
    with pytest.raises(UpdateError, match="bigger than expected"):
        download("u", dest, serving(b"x" * 2048), max_bytes=1024)
    assert not dest.exists()


def test_download_reports_network_errors(tmp_path):
    def broken(url):
        raise OSError("no network")
    with pytest.raises(UpdateError, match="Couldn't download the update."):
        download("u", tmp_path / "r.zip", broken)


# ---- The updater

import json as _json  # noqa: E402

from shelfsaver.updates import STALE_SECONDS, Updater  # noqa: E402

BREAKER = "Paused: conversions keep failing. Check the graphics card and logs."


class FakeWorker:
    def __init__(self, paused=False, message=None, ready=None):
        self.paused, self.message = paused, message
        self.ready = ready
        self.calls = []

    def pause_state(self):
        return self.paused, (self.message if self.paused else None)

    def ready_for_update(self):
        return self.paused if self.ready is None else self.ready

    def pause_after_file(self):
        self.calls.append("pause_after_file")
        self.paused = True

    def resume(self):
        self.calls.append("resume")
        self.paused, self.message = False, None

    def restore_pause(self, message):
        self.calls.append(("restore_pause", message))
        self.paused, self.message = True, message


def newer(url):
    return {"tag_name": "v0.7.2", "body": "Notes", "html_url": "https://x/v0.7.2"}


def make_updater(tmp_path, worker=None, kind="windows", current="0.7.1", fetch_json=newer,
                 stream=None, clock=lambda: 1000.0):
    runs, removed = [], []
    updater = Updater(worker or FakeWorker(), tmp_path / "data", current, kind,
                      fetch_json=fetch_json,
                      fetch_stream=stream or serving(make_release_zip("0.7.2")),
                      run_task=lambda script, folder: runs.append((script, folder)),
                      remove_task=lambda: removed.append(True), clock=clock)
    return updater, runs, removed


def state_file(tmp_path):
    return tmp_path / "data" / "update" / "state.json"


def test_check_reports_a_newer_release_that_can_be_installed(tmp_path):
    updater, _, _ = make_updater(tmp_path)
    result = updater.check()
    assert result["status"] == "newer" and result["version"] == "0.7.2"
    assert result["can_update"] is True and result["how"] is None
    assert result["notes"] == "Notes" and result["checked_at"] == 1000.0


def test_start_refuses_without_a_newer_check(tmp_path):
    updater, runs, _ = make_updater(tmp_path)
    assert updater.start()["state"] == "idle"
    updater2, runs2, _ = make_updater(tmp_path, fetch_json=lambda url: {"tag_name": "v0.7.1"})
    updater2.check()
    state = updater2.start()
    assert state["state"] == "idle" and state["message"]
    updater.tick()
    updater2.tick()
    assert runs == [] and runs2 == []


@pytest.mark.parametrize("kind", ["docker", "linux", "dev"])
def test_start_refuses_on_docker_linux_and_dev(tmp_path, kind):
    worker = FakeWorker()
    updater, runs, _ = make_updater(tmp_path, worker=worker, kind=kind)
    result = updater.check()
    assert result["can_update"] is False and result["how"]
    assert updater.start()["state"] == "idle"
    updater.tick()
    assert runs == [] and worker.calls == []


def test_start_waits_for_the_file_then_installs(tmp_path):
    worker = FakeWorker(ready=False)
    updater, runs, _ = make_updater(tmp_path, worker=worker)
    updater.check()
    assert updater.start()["state"] == "pending"
    assert worker.calls == ["pause_after_file"]
    updater.tick()
    assert runs == []
    worker.ready = True
    updater.tick()
    assert len(runs) == 1
    script, folder = runs[0]
    assert script.name == "install.ps1" and script.parent.name == "scripts"
    assert "v0.7.2" in script.parts and folder == tmp_path / "data" / "update"
    assert updater.status()["state"] == "installing"
    saved = _json.loads(state_file(tmp_path).read_text())
    assert saved["version"] == "0.7.2" and saved["was_paused"] is False
    assert saved["pause_message"] is None and saved["started_at"] == 1000.0


def test_start_twice_runs_one_install(tmp_path):
    updater, runs, _ = make_updater(tmp_path)
    updater.check()
    updater.start()
    updater.start()
    updater.tick()
    updater.tick()
    assert len(runs) == 1


def test_an_earlier_pause_is_kept_through_a_failed_download(tmp_path):
    def broken(url):
        raise OSError("no network")
    worker = FakeWorker(paused=True, message=BREAKER)
    updater, runs, _ = make_updater(tmp_path, worker=worker, stream=broken)
    updater.check()
    updater.start()
    assert "pause_after_file" not in worker.calls
    updater.tick()
    state = updater.status()
    assert state["state"] == "idle" and state["message"] == "Couldn't download the update."
    assert "resume" not in worker.calls and runs == []
    assert worker.pause_state() == (True, BREAKER)


def test_an_earlier_pause_is_recorded_for_after_the_restart(tmp_path):
    worker = FakeWorker(paused=True, message=BREAKER)
    updater, runs, _ = make_updater(tmp_path, worker=worker)
    updater.check()
    updater.start()
    updater.tick()
    saved = _json.loads(state_file(tmp_path).read_text())
    assert saved["was_paused"] is True and saved["pause_message"] == BREAKER


def test_a_failed_download_resumes_only_the_updates_own_pause(tmp_path):
    def broken(url):
        raise OSError("no network")
    worker = FakeWorker()
    updater, _, _ = make_updater(tmp_path, worker=worker, stream=broken)
    updater.check()
    updater.start()
    updater.tick()
    assert worker.calls == ["pause_after_file", "resume"]


def test_a_failed_hand_off_is_reported_and_resumes(tmp_path):
    worker = FakeWorker()
    updater, _, _ = make_updater(tmp_path, worker=worker)

    def refuse(script, folder):
        raise UpdateError("Couldn't start the installer.")
    updater._run_task = refuse
    updater.check()
    updater.start()
    updater.tick()
    assert updater.status()["state"] == "idle"
    assert updater.status()["message"] == "Couldn't start the installer."
    assert worker.calls[-1] == "resume" and not state_file(tmp_path).exists()


def test_cancel_undoes_only_the_updates_pause(tmp_path):
    worker = FakeWorker(ready=False)
    updater, runs, _ = make_updater(tmp_path, worker=worker)
    updater.check()
    updater.start()
    assert updater.cancel()["state"] == "idle"
    assert worker.calls == ["pause_after_file", "resume"]
    already = FakeWorker(paused=True, message="Paused by you", ready=False)
    updater2, runs2, _ = make_updater(tmp_path, worker=already)
    updater2.check()
    updater2.start()
    updater2.cancel()
    assert "resume" not in already.calls
    worker.ready = already.ready = True
    updater.tick()
    updater2.tick()
    assert runs == [] and runs2 == []


def test_cancel_for_resume_leaves_the_worker_to_the_caller(tmp_path):
    worker = FakeWorker(ready=False)
    updater, _, _ = make_updater(tmp_path, worker=worker)
    updater.check()
    updater.start()
    updater.cancel_for_resume()
    assert updater.status()["state"] == "idle" and worker.calls == ["pause_after_file"]


def write_state(tmp_path, *, version="0.7.2", started_at=1000.0, was_paused=False, message=None,
                done=True, log_lines=0):
    folder = tmp_path / "data" / "update"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(_json.dumps({
        "version": version, "started_at": started_at, "was_paused": was_paused,
        "pause_message": message}))
    if done:
        (folder / "done.txt").write_text("0")
    if log_lines:
        (folder / "install.log").write_text("".join(f"line {i}\n" for i in range(1, log_lines + 1)))


def test_startup_after_a_good_update(tmp_path):
    write_state(tmp_path)
    worker = FakeWorker()
    updater, _, removed = make_updater(tmp_path, worker=worker, current="0.7.2")
    updater.finish_previous()
    result = updater.status()["last_result"]
    assert result["ok"] is True and result["version"] == "0.7.2"
    assert not state_file(tmp_path).exists() and worker.calls == [] and removed == [True]
    assert not (tmp_path / "data" / "update" / "done.txt").exists()


def test_startup_after_a_failed_update_shows_the_log_and_keeps_the_pause(tmp_path):
    write_state(tmp_path, was_paused=True, message=BREAKER, log_lines=40)
    worker = FakeWorker()
    updater, _, _ = make_updater(tmp_path, worker=worker, current="0.7.1")
    updater.finish_previous()
    result = updater.status()["last_result"]
    assert result["ok"] is False and result["version"] == "0.7.2"
    assert result["log_tail"].splitlines() == [f"line {i}" for i in range(11, 41)]
    assert worker.calls == [("restore_pause", BREAKER)]


def test_startup_after_a_good_update_keeps_an_owner_pause(tmp_path):
    write_state(tmp_path, was_paused=True, message=None)
    worker = FakeWorker()
    updater, _, _ = make_updater(tmp_path, worker=worker, current="0.7.2")
    updater.finish_previous()
    assert worker.calls == [("restore_pause", None)]


def test_startup_counts_a_stale_install_as_failed(tmp_path):
    write_state(tmp_path, done=False, started_at=1000.0 - STALE_SECONDS - 1)
    updater, _, _ = make_updater(tmp_path, current="0.7.1")
    updater.finish_previous()
    assert updater.status()["last_result"]["ok"] is False
    assert not state_file(tmp_path).exists()


def test_startup_during_the_install_keeps_the_pause_and_reports_later(tmp_path):
    # The installer starts the new version and checks it, and only then writes done.txt, so the
    # new version usually starts while the install is still running.
    write_state(tmp_path, done=False, started_at=1000.0 - 60, was_paused=True, message=BREAKER)
    worker = FakeWorker()
    updater, _, removed = make_updater(tmp_path, worker=worker, current="0.7.2")
    updater.finish_previous()
    assert worker.calls == [("restore_pause", BREAKER)]
    assert updater.status()["last_result"] is None
    assert state_file(tmp_path).exists() and removed == []
    (tmp_path / "data" / "update" / "done.txt").write_text("0")
    updater.tick()
    assert updater.status()["last_result"]["ok"] is True
    assert not state_file(tmp_path).exists() and removed == [True]
    assert worker.calls == [("restore_pause", BREAKER)]


@pytest.mark.parametrize("text", ["{not json", "[]", '{"version": 7}', ""])
def test_startup_ignores_a_broken_state_file(tmp_path, text):
    folder = tmp_path / "data" / "update"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(text)
    updater, _, _ = make_updater(tmp_path)
    updater.finish_previous()
    assert updater.status()["last_result"] is None
    assert not (folder / "state.json").exists()


def test_last_result_is_shown_once(tmp_path):
    write_state(tmp_path)
    updater, _, _ = make_updater(tmp_path, current="0.7.2")
    updater.finish_previous()
    assert updater.status(consume=True)["last_result"]["ok"] is True
    assert updater.status()["last_result"] is None


def test_github_is_not_contacted_until_asked(tmp_path):
    asked = []
    updater, _, _ = make_updater(tmp_path, fetch_json=lambda url: asked.append(url) or {})
    updater.finish_previous()
    updater.status()
    updater.tick()
    assert asked == []
