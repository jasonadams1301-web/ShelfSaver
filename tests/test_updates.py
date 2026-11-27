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
