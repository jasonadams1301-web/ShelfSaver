"""Updating ShelfSaver from its GitHub releases, only when the owner asks.

Nothing here runs on its own: GitHub is contacted only when the owner presses Check for updates
or Update on the Settings page. A Windows copy installed by scripts/install.ps1 can update itself
by running the new release's installer; Docker, Linux and development copies are only told how.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Mapping, Optional

from . import __version__

logger = logging.getLogger(__name__)

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
    except Exception as error:  # noqa: BLE001 - any trouble reaching or reading GitHub is the same to the owner
        logger.warning("Couldn't check GitHub for updates: %r", error)
        return CheckResult("failed", message=CHECK_FAILED)
    if latest is None or mine is None:
        logger.warning("GitHub's newest release has no usable version: %r", tag)
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


# ---- The updater: waits for the worker, hands off to the installer, reports the result

STALE_SECONDS = 30 * 60
LOG_TAIL_LINES = 30
TICK_SECONDS = 2


def _no_task(*args) -> None:
    raise UpdateError("This copy can't start the installer.")


def _read_text_any(path: Path) -> str:
    """Text written by PowerShell 5.1 may be UTF-16 (with a BOM) or UTF-8."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8-sig", "replace")


class Updater:
    """Check for updates and install them, only when the owner asks.

    States: "idle"; "pending" (waiting for the file under way, or a parked one, to finish);
    "preparing" (downloading and unpacking); "installing" (the installer task has started and
    will stop this app).
    """

    def __init__(self, worker, data_dir: Path, current: str, kind: str,
                 fetch_json: Callable[[str], dict] = fetch_json,
                 fetch_stream: Callable[[str], BinaryIO] = fetch_stream,
                 run_task: Callable[[Path, Path], None] = _no_task,
                 remove_task: Callable[[], None] = lambda: None,
                 clock: Callable[[], float] = time.time):
        self._worker = worker
        self._dir = Path(data_dir) / "update"
        self._current = current
        self._kind = kind
        self._fetch_json = fetch_json
        self._fetch_stream = fetch_stream
        self._run_task = run_task
        self._remove_task = remove_task
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "idle"
        self._version: Optional[str] = None
        self._message: Optional[str] = None
        self._check: Optional[dict] = None
        self._last_result: Optional[dict] = None
        self._was_paused = False
        self._pause_message: Optional[str] = None
        self._previous: Optional[dict] = None  # an earlier update whose result isn't known yet

    @property
    def _state_path(self) -> Path:
        return self._dir / "state.json"

    @property
    def _done_path(self) -> Path:
        return self._dir / "done.txt"

    # ---- What the page sees

    def status(self, consume: bool = False) -> dict:
        with self._lock:
            result = self._status_locked()
            if consume:
                self._last_result = None
            return result

    def check(self) -> dict:
        found = check_latest(self._current, self._fetch_json)
        newer = found.status == "newer"
        result = {"status": found.status, "version": found.version, "notes": found.notes,
                  "url": found.url, "message": found.message, "checked_at": self._clock(),
                  "can_update": newer and self._kind == "windows",
                  "how": how_to_update(self._kind, found.version, found.url) if newer else None}
        with self._lock:
            self._check = result
            if self._state == "idle":
                self._message = None
        return result

    # ---- Owner actions

    def start(self) -> dict:
        with self._lock:
            if self._state != "idle":
                return self._status_locked()
            check = self._check
            if not check or check["status"] != "newer":
                self._message = "Check for updates first."
                return self._status_locked()
            if not check["can_update"]:
                self._message = check["how"] or "This copy can't update itself."
                return self._status_locked()
            was_paused, message = self._worker.pause_state()
            self._was_paused, self._pause_message = was_paused, message
            self._state, self._version, self._message = "pending", check["version"], None
        if not was_paused:
            self._worker.pause_after_file()
        logger.info("Update to %s asked for; it starts once no file is under way", check["version"])
        return self.status()

    def cancel(self) -> dict:
        if self._cancel_pending() and not self._was_paused:
            self._worker.resume()
        return self.status()

    def cancel_for_resume(self) -> None:
        """Resume was pressed: drop a pending update; the caller resumes the worker itself."""
        self._cancel_pending()

    def _cancel_pending(self) -> bool:
        with self._lock:
            if self._state != "pending":
                return False
            self._state, self._version, self._message = "idle", None, "The update was cancelled."
        logger.info("Update cancelled")
        return True

    # ---- The background loop

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep checking; the error is in the log
                logger.exception("Update check failed unexpectedly")

    def tick(self) -> None:
        self._finish_if_done()
        with self._lock:
            if self._state != "pending" or not self._worker.ready_for_update():
                return
            self._state = "preparing"
            version = self._version
        try:
            root = prepare(version, self._dir, self._fetch_stream)
            self._write_state(version)
            self._run_task(root / "scripts" / "install.ps1", self._dir)
        except (UpdateError, OSError) as error:
            message = str(error) if isinstance(error, UpdateError) else "Couldn't prepare the update."
            logger.warning("Update to %s stopped: %s", version, message)
            self._state_path.unlink(missing_ok=True)
            with self._lock:
                self._state, self._version, self._message = "idle", None, message
            if not self._was_paused:
                self._worker.resume()
            return
        with self._lock:
            self._state = "installing"
        logger.info("Installing %s: ShelfSaver will restart", version)

    def _write_state(self, version: str) -> None:
        self._done_path.unlink(missing_ok=True)
        data = {"version": version, "started_at": self._clock(), "was_paused": self._was_paused,
                "pause_message": self._pause_message}
        temp = self._state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temp, self._state_path)

    # ---- After a restart

    def finish_previous(self) -> None:
        """At start-up, before the worker runs: keep an earlier pause, note the update's result.

        The installer starts the new version (and checks it) before it writes done.txt, so the
        result is often filled in a few seconds later, by tick().
        """
        try:
            data = json.loads(_read_text_any(self._state_path))
            version = data["version"]
            started_at = float(data["started_at"])
            if not isinstance(version, str) or parse_version(version) is None:
                raise ValueError("bad version")
            message = data.get("pause_message")
            previous = {"version": version, "started_at": started_at,
                        "was_paused": bool(data.get("was_paused")),
                        "pause_message": message if isinstance(message, str) else None}
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 - a broken record must never stop ShelfSaver starting
            logger.warning("Ignoring an unreadable update record")
            self._state_path.unlink(missing_ok=True)
            return
        if previous["was_paused"]:
            self._worker.restore_pause(previous["pause_message"])
        with self._lock:
            self._previous = previous
        self._finish_if_done()

    def _finish_if_done(self) -> None:
        with self._lock:
            previous = self._previous
        if previous is None:
            return
        done = self._done_path.exists()
        if not done and self._clock() - previous["started_at"] < STALE_SECONDS:
            return
        ok = self._current == previous["version"]
        result = {"ok": ok, "version": previous["version"], "log_tail": ""}
        if not ok:
            try:
                lines = _read_text_any(self._dir / "install.log").splitlines()
                result["log_tail"] = "\n".join(lines[-LOG_TAIL_LINES:])
            except OSError:
                pass
        if ok:
            logger.info("Updated to %s", previous["version"])
        else:
            logger.warning("The update to %s didn't finish", previous["version"])
        self._state_path.unlink(missing_ok=True)
        self._done_path.unlink(missing_ok=True)
        try:
            self._remove_task()
        except Exception:  # noqa: BLE001 - a leftover task is harmless and retried next time
            logger.warning("Couldn't remove the finished update task")
        with self._lock:
            self._previous = None
            self._last_result = result

    def _status_locked(self) -> dict:
        # Paused for the update but still not ready means a converted file is waiting for room on
        # its library drive (the worker is paused, so no file is under way).
        waiting_for_room = (self._state == "pending" and self._worker.pause_state()[0]
                            and not self._worker.ready_for_update())
        return {"state": self._state, "version": self._version, "message": self._message,
                "check": self._check, "last_result": self._last_result, "kind": self._kind,
                "waiting_for_room": waiting_for_room}


# ---- The Windows hand-off: a one-off SYSTEM task runs the new release's installer
#
# The installer stops ShelfSaver with `taskkill /T` on every process from the program folder, so it
# can't be a child of the app: it would be killed along with it. Task Scheduler starts it instead,
# as SYSTEM, the way install.ps1's own Test-SystemRuns does.

TASK_NAME = "ShelfSaver-Update"


def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def venv_base_python(prefix: Path) -> Optional[Path]:
    """The python.exe this venv was built from (`home` in pyvenv.cfg), if it still exists.

    The installer runs as SYSTEM, whose PATH may not include a Python installed for one user, so
    it is told which one built the running copy.
    """
    try:
        text = (Path(prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == "home" and value.strip():
            candidate = Path(value.strip()) / "python.exe"
            return candidate if candidate.is_file() else None
    return None


def task_command(install_ps1: Path, update_dir: Path, python: Optional[Path]) -> str:
    """The PowerShell the update task runs: the installer, its log, and its exit code."""
    log = _ps_quote(Path(update_dir) / "install.log")
    done = _ps_quote(Path(update_dir) / "done.txt")
    python_arg = f" -Python {_ps_quote(python)}" if python else ""
    return "\n".join([
        "$LASTEXITCODE = $null",
        f"& {_ps_quote(install_ps1)}{python_arg} *> {log}",
        "$code = if ($null -eq $LASTEXITCODE) { 1 } else { $LASTEXITCODE }",
        f"Set-Content -LiteralPath {done} -Value $code -Encoding Ascii",
    ])


def _encoded(script: str) -> str:
    import base64
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _run_powershell(script: str, error: str) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", _encoded(script)],
            capture_output=True, text=True, timeout=60, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as failure:
        raise UpdateError(error) from failure
    if result.returncode != 0:
        logger.warning("%s %s", error, (result.stderr or result.stdout).strip()[-500:])
        raise UpdateError(error)


def run_install_task(install_ps1: Path, update_dir: Path) -> None:
    """Register and start the one-off SYSTEM task that installs the new release."""
    command = task_command(install_ps1, update_dir, venv_base_python(Path(sys.prefix)))
    # The command is passed encoded, so the installer's own "stop every ShelfSaver process" search,
    # which looks for the program folder in command lines, never matches the update task itself.
    argument = ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
                + _encoded(command))
    _run_powershell("\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {_ps_quote(argument)}",
        "$who = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest",
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
        " -ExecutionTimeLimit (New-TimeSpan -Hours 2)",
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $action -Principal $who"
        " -Settings $settings -Force | Out-Null",
        f"Start-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}",
    ]), "Couldn't start the installer.")


def remove_task() -> None:
    """Remove the finished update task (it can't remove itself while it runs)."""
    _run_powershell(
        f"Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false"
        " -ErrorAction SilentlyContinue", "Couldn't remove the update task.")
