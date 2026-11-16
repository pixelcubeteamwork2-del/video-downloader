"""Video Downloader as a desktop app.

Starts the local server and opens it in its own app window (Microsoft Edge, Google Chrome,
Brave or Chromium in app mode: no tabs or address bar, a separate profile). Downloads go
through that window's own download list, so they show their size and progress there.
Closing the window quits. Works on Windows and macOS (and Linux).

Run from source:   python desktop.py
Build:             desktop/build.ps1 (Windows)  ·  desktop/build_mac.sh (macOS)
"""

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

APP_NAME = "Video Downloader"
PORT = 5757  # fixed, so the window's saved preferences (theme, preview quality) are kept
FROZEN = getattr(sys, "frozen", False)
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"

# --- helper mode: the packaged app runs gallery-dl through itself (there's no "python -m") ---
if len(sys.argv) > 1 and sys.argv[1] == "--gallery-dl":
    from gallery_dl import main as gallery_main

    sys.argv = ["gallery-dl", *sys.argv[2:]]
    sys.exit(gallery_main())


def data_dir() -> str:
    """Settings, cookies, logs and the window's browser profile, in the usual place per system:
    Windows %APPDATA%/Video Downloader, macOS ~/Library/Application Support/Video Downloader,
    Linux ~/.local/share/Video Downloader."""
    home = os.path.expanduser("~")
    if IS_WIN:
        base = os.environ.get("APPDATA") or home
    elif IS_MAC:
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    path = os.path.join(base, APP_NAME)
    os.makedirs(os.path.join(path, "logs"), exist_ok=True)
    return path


DATA = data_dir()
if FROZEN:
    os.environ.setdefault("VD_DATA_DIR", DATA)  # from source, keep using the project folder's login
    # Tools shipped with the app (ffmpeg, deno for YouTube) work on computers that lack them:
    # next to the .exe on Windows, inside the .app bundle on macOS.
    bundled = [os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", "")]
    os.environ["PATH"] = os.pathsep.join([p for p in bundled if p] + [os.environ.get("PATH", "")])
if IS_MAC:
    # Apps opened from Finder don't get the Terminal's PATH, so Homebrew tools would be missed.
    extra = [p for p in ("/opt/homebrew/bin", "/usr/local/bin") if os.path.isdir(p)]
    os.environ["PATH"] = os.pathsep.join([os.environ.get("PATH", ""), *extra])

# A windowed .exe has no console: send output to a log file instead of nowhere.
if sys.stdout is None or sys.stderr is None:
    log = open(os.path.join(DATA, "logs", "desktop.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log

# Don't flash a console window for every ffmpeg / gallery-dl / yt-dlp helper process.
if IS_WIN:
    _Popen = subprocess.Popen

    class _QuietPopen(_Popen):
        def __init__(self, *args, **kwargs):
            if not kwargs.get("creationflags"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _QuietPopen


# --- MCP mode: AI assistants (Claude, ChatGPT, Codex...) start the .exe with --mcp ---
if "--mcp" in sys.argv or "--mcp-http" in sys.argv:
    import mcp_server

    mcp_server.main(sys.argv[1:])
    sys.exit(0)


def already_running(port: int) -> bool:
    """True if this app's server is already answering on the port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free port")


def find_browser() -> str | None:
    """A Chromium-based browser that can open an app window (--app): Edge, Chrome, Brave, Chromium."""
    if IS_WIN:
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.join(local, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
    elif IS_MAC:
        apps = ["Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium", "Vivaldi"]
        roots = ["/Applications", os.path.expanduser("~/Applications")]
        candidates = [os.path.join(r, f"{a}.app", "Contents", "MacOS", a) for a in apps for r in roots]
    else:
        names = ("google-chrome", "google-chrome-stable", "microsoft-edge", "brave-browser",
                 "chromium", "chromium-browser")
        candidates = [shutil.which(n) or "" for n in names]
    return next((p for p in candidates if p and os.path.isfile(p)), None)


def open_window(url: str) -> subprocess.Popen | None:
    """Open the app window. Returns its process (it ends when the window closes)."""
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return None
    return subprocess.Popen([
        browser, f"--app={url}",
        f"--user-data-dir={os.path.join(DATA, 'window')}",  # its own profile: its own window and process
        "--window-size=1440,920", "--no-first-run", "--no-default-browser-check",
        "--disable-background-mode", "--disable-features=Translate,msEdgeStartupBoost",
    ])


def window_open() -> bool:
    """True while the app window's browser runs. Its first process may hand off to another, so
    its PID can't be waited on; instead look at the lock the browser keeps on its profile:
    Windows: "lockfile", held open while running (and deleted at exit).
    macOS/Linux: "SingletonLock", a symlink to "<host>-<pid>" of the running browser."""
    profile = os.path.join(DATA, "window")
    if IS_WIN:
        lock = os.path.join(profile, "lockfile")
        if not os.path.exists(lock):
            return False
        try:
            with open(lock, "a"):
                return False  # a leftover file nobody holds
        except OSError:
            return True
    try:
        target = os.readlink(os.path.join(profile, "SingletonLock"))
        os.kill(int(target.rsplit("-", 1)[1]), 0)  # signal 0 only checks the process exists
        return True
    except (OSError, ValueError, IndexError):
        return False


def wait_for_window_close():
    for _ in range(60):  # give it up to 30 s to start
        if window_open():
            break
        time.sleep(0.5)
    misses = 0
    while misses < 3:  # gone for 3 s in a row (a first launch restarts itself once)
        time.sleep(1)
        misses = 0 if window_open() else misses + 1


def main():
    port = PORT
    if already_running(port):  # second launch: just open another window on the running app
        open_window(f"http://127.0.0.1:{port}/")
        return
    port = free_port(PORT)

    import app as server  # import after the settings above (data folder, quiet helpers)
    from werkzeug.serving import make_server

    server.purge_old_jobs()

    @server.app.before_request  # must be set up before the first request
    def _seen():
        LAST_REQUEST[0] = time.time()

    print(f"{APP_NAME} on http://127.0.0.1:{port}  ffmpeg: {server.FFMPEG_PATH or 'not found'}")
    httpd = make_server("127.0.0.1", port, server.app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    window = open_window(f"http://127.0.0.1:{port}/")
    if window is None:  # no Chromium browser (e.g. only Safari): it opened in the default browser
        wait_until_idle(server)
    else:
        wait_for_window_close()
    httpd.shutdown()


IDLE_QUIT = 30 * 60  # seconds without any page request or running download
LAST_REQUEST = [time.time()]


def wait_until_idle(server):
    """Without an app window there's nothing to close, so quit after a long quiet spell."""
    while True:
        time.sleep(30)
        if any(j.get("status") in ("running", "ready") for j in server.JOBS.values()):
            LAST_REQUEST[0] = time.time()
        elif time.time() - LAST_REQUEST[0] > IDLE_QUIT:
            return


if __name__ == "__main__":
    main()
