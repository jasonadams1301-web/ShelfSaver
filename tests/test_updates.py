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
