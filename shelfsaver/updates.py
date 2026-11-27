"""Updating ShelfSaver from its GitHub releases, only when the owner asks.

Nothing here runs on its own: GitHub is contacted only when the owner presses Check for updates
or Update on the Settings page. A Windows copy installed by scripts/install.ps1 can update itself
by running the new release's installer; Docker, Linux and development copies are only told how.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from . import __version__

REPO = "jasonadams1301-web/ShelfSaver"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT_SECONDS = 15
USER_AGENT = f"ShelfSaver/{__version__}"

CHECK_FAILED = "Couldn't check GitHub right now. Try again later."

_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(tag: str) -> Optional[tuple[int, int, int]]:
    """(0, 7, 3) from "v0.7.3" or "0.7.3"; None for anything else."""
    match = _TAG.match(tag or "")
    return tuple(int(part) for part in match.groups()) if match else None


@dataclass
class CheckResult:
    status: str  # "newer", "current" or "failed"
    version: Optional[str] = None
    notes: str = ""
    url: str = ""
    message: str = ""


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def check_latest(current: str, fetch_json: Callable[[str], dict] = fetch_json) -> CheckResult:
    """Ask GitHub for its newest release and compare it with the running version."""
    try:
        answer = fetch_json(RELEASES_URL)
        tag = answer.get("tag_name") if isinstance(answer, dict) else None
        latest = parse_version(tag) if isinstance(tag, str) else None
        mine = parse_version(current)
    except Exception:  # noqa: BLE001 - any trouble reaching or reading GitHub is the same to the owner
        return CheckResult("failed", message=CHECK_FAILED)
    if latest is None or mine is None:
        return CheckResult("failed", message=CHECK_FAILED)
    if latest <= mine:
        return CheckResult("current", version=current)
    return CheckResult("newer", version=".".join(map(str, latest)),
                       notes=answer.get("body") or "", url=answer.get("html_url") or "")


def install_kind(*, is_windows: bool, prefix: str, program_files: str,
                 env: Mapping[str, str]) -> str:
    """"windows" for a copy installed by install.ps1; otherwise "docker", "linux" or "dev"."""
    if env.get("SHELFSAVER_DOCKER"):
        return "docker"
    if not is_windows:
        return "linux"
    if program_files:
        installed = os.path.join(program_files, "ShelfSaver", "venv")
        if os.path.normcase(os.path.normpath(prefix)) == os.path.normcase(os.path.normpath(installed)):
            return "windows"
    return "dev"


def how_to_update(kind: str, version: str, url: str) -> Optional[str]:
    """How to update by hand, for copies that can't update themselves."""
    if kind == "docker":
        return f"Pull the new image: ghcr.io/jasonadams1301-web/shelfsaver:{version}"
    if kind == "linux":
        return f"Download it from {url} and run install.sh again."
    if kind == "dev":
        return f"This copy wasn't installed by setup, so update it by hand: {url}"
    return None
