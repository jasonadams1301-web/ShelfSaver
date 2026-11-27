"""Start ShelfSaver: the worker, the scanner and the web server."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading

import uvicorn

from . import __version__, hostos
from .config import load_config
from .db import Database
from .drive_health import DriveHealth
from .logs import setup_logging
from .scanner import Scanner
from .updates import Updater, install_kind, remove_task, run_install_task
from .web import create_app
from .worker import Worker

logger = logging.getLogger(__name__)

LOCK_NAME = "shelfsaver.lock"
ALREADY_RUNNING = "ShelfSaver is already running on this PC. Close it first, then start it again."


def acquire_instance_lock(data_dir):
    """Lock a file in the data folder for as long as the returned handle stays open.

    Returns None if another process holds it. The OS drops the lock when its process dies
    (on Windows and Linux alike), so a crash never blocks the next start.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / LOCK_NAME, "a+b")
    # The OS-specific locking call lives in hostos so this file imports cleanly on Linux.
    if not hostos.try_lock_file(handle):
        handle.close()
        return None
    return handle


def stop_on_sigterm(stop: threading.Event) -> None:
    """Make SIGTERM (what `docker stop` and systemd send) shut ShelfSaver down cleanly.

    Why this is needed: uvicorn handles SIGTERM, shuts the web server down gracefully, then
    restores the previous signal handlers and re-sends the same signal to the process so the exit
    status looks right. Python's default action for SIGTERM is to kill the process on the spot,
    so the re-sent signal ended the program inside server.run(): the `finally` in main() never
    ran, the worker was never told to stop and ffmpeg was not asked to finish. With this handler
    in place uvicorn restores *it*, the re-sent signal just sets the stop flag, server.run()
    returns normally, and the worker shuts down in order.

    Windows does not deliver SIGTERM to Python (Ctrl+C arrives as KeyboardInterrupt, which
    already unwinds through `finally`), so nothing is installed there.
    """
    if not hostos.IS_WINDOWS:
        signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())


def server_options(config) -> dict:
    """The network-facing uvicorn settings, kept in one place so a test can see them.

    X-Forwarded-* headers say "the real visitor is X, and used HTTPS". They are only a claim by
    whoever sent them, so with no `trusted_proxies` configured they are not believed at all
    (proxy_headers off). When proxies are listed, only those addresses are believed; believing
    anyone would let a visitor choose the address the login rate limit sees. (uvicorn's own
    default would trust loopback, which is wrong here: a same-machine program is not a proxy
    unless the person said so.)
    """
    return {"host": config.host, "port": config.port, "log_config": None,
            "proxy_headers": bool(config.trusted_proxies),
            "forwarded_allow_ips": config.trusted_proxies or None}


def make_updater(worker, config) -> Updater:
    kind = install_kind(is_windows=hostos.IS_WINDOWS, prefix=sys.prefix,
                        program_files=os.environ.get("ProgramFiles", ""), env=os.environ)
    if kind == "windows":
        return Updater(worker, config.data_dir, __version__, kind,
                       run_task=run_install_task, remove_task=remove_task)
    return Updater(worker, config.data_dir, __version__, kind)


def main() -> None:
    config = load_config()
    lock = acquire_instance_lock(config.data_dir)
    if lock is None:
        print(ALREADY_RUNNING, file=sys.stderr)
        sys.exit(1)
    setup_logging(config.data_dir)
    db = Database(config.data_dir / "shelfsaver.db")
    worker = Worker(db, config)
    scanner = Scanner(db, config)
    worker.recover()
    drives = DriveHealth(db)
    updater = make_updater(worker, config)
    updater.finish_previous()  # before the worker starts, so an earlier pause is back in place

    stop = threading.Event()
    stop_on_sigterm(stop)
    threads = [
        threading.Thread(target=worker.run_forever, args=(stop,), name="worker"),
        threading.Thread(target=scanner.start, args=(stop,), name="scanner"),
        threading.Thread(target=drives.run_forever, args=(stop,), name="drives", daemon=True),
        threading.Thread(target=updater.run_forever, args=(stop,), name="updates", daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        server = uvicorn.Server(uvicorn.Config(
            create_app(db, worker, scanner, config, drives, updater), **server_options(config)))
        server.run()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
        logger.info("Stopped")


if __name__ == "__main__":
    main()
