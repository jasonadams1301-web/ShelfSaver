"""Updating ShelfSaver from its GitHub releases, only when the owner asks.

Nothing here runs on its own: GitHub is contacted only when the owner presses Check for updates
or Update on the Settings page. A Windows copy installed by scripts/install.ps1 can update itself
by running the new release's installer; Docker, Linux and development copies are only told how.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Mapping, Optional

from . import __version__

REPO = "jasonadams1301-web/ShelfSaver"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT_SECONDS = 15
USER_AGENT = f"ShelfSaver/{__version__}"

ZIP_URL = "https://github.com/" + REPO + "/archive/refs/tags/v{version}.zip"
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024

CHECK_FAILED = "Couldn't check GitHub right now. Try again later."


class UpdateError(Exception):
    """Something stopped the update; the message is shown to the owner as it is."""

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


# ---- Download and unpack a release

def fetch_stream(url: str) -> BinaryIO:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)


def download(url: str, dest: Path, fetch_stream: Callable[[str], BinaryIO] = fetch_stream,
             max_bytes: int = MAX_DOWNLOAD_BYTES) -> None:
    """Save url to dest, refusing anything bigger than max_bytes. Nothing is left on failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with fetch_stream(url) as source, open(dest, "wb") as out:
            total = 0
            while True:
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UpdateError("The download was bigger than expected.")
                out.write(chunk)
    except UpdateError:
        dest.unlink(missing_ok=True)
        raise
    except OSError as error:
        dest.unlink(missing_ok=True)
        raise UpdateError("Couldn't download the update.") from error


def extract(zip_path: Path, dest: Path) -> Path:
    """Unpack a GitHub source ZIP into dest and return its one top folder.

    Every name is checked before anything is written, so an entry that would land outside dest
    (`..`, an absolute path or a drive) stops the whole unpack.
    """
    dest = Path(dest)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            tops = set()
            base = dest.resolve()
            for name in names:
                posix = PurePosixPath(name.replace("\\", "/"))
                if posix.is_absolute() or ":" in name or ".." in posix.parts or not posix.parts:
                    raise UpdateError("The download contains a file outside its folder.")
                target = (dest / Path(*posix.parts)).resolve()
                if base != target and base not in target.parents:
                    raise UpdateError("The download contains a file outside its folder.")
                tops.add(posix.parts[0])
            if len(tops) != 1:
                raise UpdateError("The download isn't a ShelfSaver release.")
            dest.mkdir(parents=True, exist_ok=True)
            archive.extractall(dest)
    except zipfile.BadZipFile as error:
        raise UpdateError("The download isn't a ShelfSaver release.") from error
    root = dest / tops.pop()
    if not root.is_dir():
        raise UpdateError("The download isn't a ShelfSaver release.")
    return root


_VERSION_LINE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def extracted_version(root: Path) -> Optional[str]:
    """The version an unpacked release says it is, read as text (never imported)."""
    try:
        text = (root / "shelfsaver" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION_LINE.search(text)
    return match.group(1) if match else None


def prepare(version: str, update_dir: Path,
            fetch_stream: Callable[[str], BinaryIO] = fetch_stream) -> Path:
    """Download and unpack release `version` into update_dir/v<version>; return its top folder."""
    update_dir = Path(update_dir)
    update_dir.mkdir(parents=True, exist_ok=True)
    for old in update_dir.glob("v*"):
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
    archive = update_dir / f"v{version}.zip"
    try:
        download(ZIP_URL.format(version=version), archive, fetch_stream)
        root = extract(archive, update_dir / f"v{version}")
    finally:
        archive.unlink(missing_ok=True)
    if extracted_version(root) != version:
        raise UpdateError(f"The download doesn't contain version {version}.")
    if not (root / "scripts" / "install.ps1").is_file():
        raise UpdateError("The download isn't a ShelfSaver release.")
    return root
