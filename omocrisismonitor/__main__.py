"""Entry point: boot the server, and (unless --no-open) open the themed
Chromium --app window pointed at it.

Single-instance guarded by a pid lock in $XDG_DATA_HOME/omocrisismonitor/.
Window placement (the Hisense / DP-2) is done by the Hyprland rule in
share/hyprland-windowrule.conf plus a best-effort hyprctl nudge.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import data_dir, load

TITLE = "OmoCrisisMonitor"
_LOCK_FD = None  # kept open for the process lifetime; flock releases on death


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex((host, port)) == 0


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _port_open(host, port):
            return True
        time.sleep(0.15)
    return False


def _spawn_detached(argv: list[str]) -> None:
    subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def _acquire_single_instance() -> bool:
    """True if we got the lock. Uses flock: the kernel drops it automatically
    when this process exits, however it exits — no stale pid files."""
    global _LOCK_FD
    path = data_dir() / "omocrisismonitor.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _LOCK_FD = fd  # keep the reference alive
    return True


def _chromium() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "brave"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _open_ui(url: str) -> None:
    browser = _chromium()
    if not browser:
        print(f"[omocrisismonitor] no chromium found — open {url} yourself", file=sys.stderr)
        return
    profile = data_dir() / "browser"
    _spawn_detached(
        [
            browser,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--class=omocrisismonitor",
            "--no-first-run",
            "--no-default-browser-check",
            "--ozone-platform-hint=auto",
        ]
    )
    # best-effort: shove it onto the larger display (DP-2 / Hisense) under Hyprland
    if shutil.which("hyprctl"):
        for _ in range(20):
            time.sleep(0.25)
            out = subprocess.run(
                ["hyprctl", "clients", "-j"], capture_output=True, text=True
            ).stdout
            if TITLE in out or "omocrisismonitor" in out:
                subprocess.run(
                    ["hyprctl", "dispatch", "movewindow", "mon:DP-2"],
                    capture_output=True,
                )
                break


def main() -> None:
    ap = argparse.ArgumentParser(prog="omocrisismonitor")
    ap.add_argument("--no-open", action="store_true", help="don't launch the window")
    ap.add_argument("-p", "--port", type=int, default=None)
    ap.add_argument("--host", default=None)
    args = ap.parse_args()

    cfg = load()
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port

    if _port_open(host, port):
        print(f"[omocrisismonitor] already serving on {host}:{port}")
        if not args.no_open:
            _open_ui(f"http://{host}:{port}/")
        return

    if not _acquire_single_instance():
        print("[omocrisismonitor] another instance holds the lock — exiting")
        return

    import uvicorn

    from .server import create_app

    app = create_app(cfg)

    if not args.no_open and hasattr(os, "fork"):
        # open the window once the port is up, from a child so uvicorn.run can block
        if os.fork() == 0:
            if _wait_for_port(host, port):
                _open_ui(f"http://{host}:{port}/")
            os._exit(0)

    print(f"[omocrisismonitor] http://{host}:{port}/")
    # uvicorn owns SIGINT/SIGTERM. Cap the graceful-shutdown wait so a still-
    # connected window can't wedge the process on `pkill` (we saw it hang).
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=5,
        ws_ping_interval=None,  # we send our own keepalive from /ws
    )


if __name__ == "__main__":
    main()
