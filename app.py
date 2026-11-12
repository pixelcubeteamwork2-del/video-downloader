"""Video Downloader - Flask backend powered by yt-dlp and gallery-dl.

Sites: Facebook, YouTube, Instagram, X (Twitter), Reddit, LinkedIn, Pinterest.
Features: full downloads in any quality, trimmed clips, full-resolution frame
captures, thumbnails, and every image/video in a post (media gallery).
Downloads run as background jobs so the page can show live progress.
"""

import html as html_lib
import importlib.util
import json
import math
import os
import re
import sys
import zipfile
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from urllib.parse import urlparse

from flask import (
    Flask, Response, after_this_request, jsonify, render_template, request, send_file,
)
from PIL import Image
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, DownloadError

app = Flask(__name__)

SUPPORTED_SITES = {
    "facebook": ("facebook.com", "fb.watch", "fb.com"),
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "instagram": ("instagram.com", "instagr.am"),
    "twitter": ("twitter.com", "x.com", "t.co"),
    "reddit": ("reddit.com", "redd.it"),
    "linkedin": ("linkedin.com", "lnkd.in"),
    "tiktok": ("tiktok.com",),
    "pinterest": ("pin.it",),  # plus every pinterest.<country> domain, see detect_site
}
PINTEREST_HOST = re.compile(r"(?:^|\.)pinterest\.(?:com?\.)?[a-z]{2,4}$")
SITE_NAMES = "Facebook, YouTube, Instagram, TikTok, X, Reddit, LinkedIn or Pinterest"
# Short-link hosts we expand ourselves before handing the URL to yt-dlp.
SHORT_LINK_HOSTS = {"pin.it", "t.co", "lnkd.in", "redd.it", "vm.tiktok.com", "vt.tiktok.com"}
# Sites whose posts can hold several images/videos (carousels, galleries, threads).
MEDIA_SITES = {"instagram", "twitter", "reddit", "linkedin", "pinterest", "facebook", "tiktok"}
HAS_GALLERY_DL = importlib.util.find_spec("gallery_dl") is not None

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PROJECT_DIR, "settings.json")
COOKIES_FILE = os.path.join(PROJECT_DIR, "cookies.txt")
COOKIE_BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium")
# Download choices sent by the browser: h<height>, audio, mp3, best, or fid:<format id>.
FORMAT_KEY = re.compile(r"h\d{2,4}|audio|mp3|best|fid:[\w\-]+")
# Stream URLs found by /api/info, used by the preview player and frame capture.
SESSIONS: dict[str, dict] = {}
SESSION_TTL = 3 * 3600
# Background download jobs, polled by the page for progress.
JOBS: dict[str, dict] = {}
JOB_TTL = 3600


def find_ffmpeg() -> str | None:
    """Locate ffmpeg on PATH, via FFMPEG_PATH, or in common install folders."""
    env = os.environ.get("FFMPEG_PATH")
    if env and os.path.exists(env):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    home = os.path.expanduser("~")
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates = [
        os.path.join(home, "bin", exe),
        os.path.join(home, "ffmpeg", "bin", exe),
        os.path.join("C:\\", "ffmpeg", "bin", exe),
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "ffmpeg", "bin", exe),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", exe),
        os.path.join(home, "scoop", "shims", exe),
        os.path.join("C:\\", "ProgramData", "chocolatey", "bin", exe),
    ]
    return next((p for p in candidates if os.path.isfile(p)), None)


FFMPEG_PATH = find_ffmpeg()
HAS_FFMPEG = FFMPEG_PATH is not None
if FFMPEG_PATH:
    # Some yt-dlp checks (e.g. partial/trimmed downloads) ignore ffmpeg_location
    # and only search PATH, so put ffmpeg's folder on PATH for this process.
    ffmpeg_dir = os.path.dirname(os.path.abspath(FFMPEG_PATH))
    if ffmpeg_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
DOWNLOAD_ROOT = os.path.join(tempfile.gettempdir(), "video_downloader")
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)


def detect_site(url: str) -> str | None:
    """Return 'facebook', 'youtube' or 'pinterest' for a supported URL, else None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if PINTEREST_HOST.search(host):
        return "pinterest"
    for site, hosts in SUPPORTED_SITES.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return site
    return None


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\r\n]+', "", name).strip()
    return (name or "video")[:120]


def stamp(seconds: float) -> str:
    """83.5 -> '1m23.5s' for filenames."""
    m, s = divmod(seconds, 60)
    s_txt = f"{s:.1f}".rstrip("0").rstrip(".")
    return f"{int(m)}m{s_txt}s" if m else f"{s_txt}s"


# ---------- Login (cookies) settings ----------

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def cookie_source() -> tuple[str, str] | None:
    """('file', path) or ('browser', name) when the user turned on login, else None."""
    if os.path.isfile(COOKIES_FILE):
        return ("file", COOKIES_FILE)
    browser = load_settings().get("cookies_browser")
    if browser in COOKIE_BROWSERS:
        return ("browser", browser)
    return None


def base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    source = cookie_source()
    if source and source[0] == "file":
        opts["cookiefile"] = source[1]
    elif source:
        opts["cookiesfrombrowser"] = (source[1],)
    return opts


VIDEO_EXTS = ("mp4", "webm", "mov", "mkv", "flv", "3gp")
AUDIO_EXTS = ("m4a", "mp3", "aac", "ogg", "opus", "wav")


def has_video(f: dict) -> bool:
    codec = f.get("vcodec")
    if codec:
        return codec != "none"
    # Codec not reported (common on Pinterest/Facebook direct MP4s): judge by size/container.
    return bool(f.get("height") or f.get("width")) or f.get("ext") in VIDEO_EXTS


def has_audio(f: dict) -> bool:
    codec = f.get("acodec")
    if codec:
        return codec != "none"
    # Unknown codec in a regular media file: assume it carries sound.
    return f.get("ext") in VIDEO_EXTS + AUDIO_EXTS


def short_side(f: dict) -> int:
    """Resolution as people name it: 1080 for both 1920x1080 and vertical 1080x1920."""
    w, h = f.get("width") or 0, f.get("height") or 0
    return min(w, h) if w and h else (h or w)


def is_direct(f: dict) -> bool:
    """A plain HTTP file URL (not HLS/DASH manifests) that players and ffmpeg can seek in."""
    return bool(f.get("url")) and f.get("protocol", "https") in ("http", "https")


def quality_label(p: int) -> str:
    """p is the short side, e.g. 1080 for Full HD in landscape or portrait."""
    if p >= 2160:
        return f"{p}p 4K"
    if p >= 1440:
        return f"{p}p 2K"
    return f"{p}p" + (" HD" if p >= 720 else " SD")


def build_options(info: dict) -> list[dict]:
    """Turn yt-dlp formats into a short list of download choices."""
    formats = info.get("formats") or []
    audios = [f for f in formats if has_audio(f) and not has_video(f)]
    best_audio_size = max(
        (f.get("filesize") or f.get("filesize_approx") or 0 for f in audios), default=0
    )

    # Largest file per height, so the size shown matches what we'll likely download.
    # Keys stay "height" (what yt-dlp filters on); labels use the short side so
    # vertical videos read 1080p rather than 1920p.
    by_height: dict[int, int] = {}
    label_p: dict[int, int] = {}
    for f in formats:
        height = f.get("height")
        if not has_video(f) or not height or height < 144:
            continue
        if not HAS_FFMPEG and not has_audio(f):
            continue  # can't merge separate streams without ffmpeg
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if size and not has_audio(f):
            size += best_audio_size
        by_height[height] = max(by_height.get(height, 0), size)
        label_p[height] = short_side(f)

    choices, seen_labels = [], set()
    for h in sorted(by_height, reverse=True):
        label = quality_label(label_p[h])
        if label in seen_labels:  # e.g. 1136x640 and 1138x640 both read "640p SD"
            continue
        seen_labels.add(label)
        choices.append({"format": f"h{h}", "label": label, "ext": "mp4", "size": by_height[h] or None})

    # Facebook sometimes reports no heights, only "hd"/"sd" format ids.
    if not choices:
        for fid, label in (("hd", "HD"), ("sd", "SD")):
            if any(f.get("format_id") == fid for f in formats):
                choices.append({"format": f"fid:{fid}", "label": label, "ext": "mp4", "size": None})
    if not choices:
        choices.append({"format": "best", "label": "Best available", "ext": "mp4", "size": None})

    if any(has_audio(f) for f in formats):
        if HAS_FFMPEG:
            choices.append({"format": "mp3", "label": "Audio", "ext": "mp3", "size": None})
        choices.append({"format": "audio", "label": "Audio", "ext": "m4a", "size": best_audio_size or None})
    return choices


def pick_preview(formats: list[dict]) -> dict | None:
    """A browser-playable MP4 for the in-page player: with sound, ideally <=720p."""
    playable = [
        f for f in formats
        if is_direct(f) and has_video(f) and f.get("ext") == "mp4"
        and not str(f.get("vcodec") or "").startswith("av01")
    ]
    if not playable:
        return None

    def score(f):
        p = short_side(f) or 360
        small = p <= 720
        # Prefer sound, then the sharpest stream <=720p, else the smallest larger one.
        return (has_audio(f), small, p if small else -p, f.get("tbr") or 0)

    return max(playable, key=score)


def preview_choices(formats: list[dict], default: dict | None) -> list[dict]:
    """One browser-playable stream per height (up to 1080p), for the player's quality menu.
    Lower sizes seek faster; on YouTube they usually have no sound (picture only)."""
    best: dict[int, dict] = {}
    for f in formats:
        if not (is_direct(f) and has_video(f) and f.get("ext") in ("mp4", "webm")):
            continue
        if str(f.get("vcodec") or "").startswith("av01"):
            continue
        h = short_side(f)
        if not h or h > 1080:
            continue
        rank = (has_audio(f), f.get("ext") == "mp4", -(f.get("tbr") or 0))
        if h not in best or rank > best[h][0]:
            best[h] = (rank, f)
    picks = {h: f for h, (_, f) in best.items()}
    if default is not None and short_side(default):
        picks[short_side(default)] = default
    out = []
    for h in sorted(picks, reverse=True):
        f = picks[h]
        out.append({**stream_ref(f), "label": f"{h}p", "sound": has_audio(f), "default": f is default})
    return out


def pick_frame_sources(formats: list[dict]) -> list[dict]:
    """Highest-resolution video streams, best first, for frame capture."""
    videos = [f for f in formats if is_direct(f) and has_video(f)]
    videos.sort(
        key=lambda f: (
            (f.get("width") or 0) * (f.get("height") or 0),
            not str(f.get("vcodec") or "").startswith("av01"),  # prefer VP9/H.264 on ties
            f.get("tbr") or 0,
        ),
        reverse=True,
    )
    return videos[:3]


def stream_ref(f: dict) -> dict:
    return {
        "url": f["url"],
        "headers": f.get("http_headers") or {},
        "height": f.get("height"),
        "width": f.get("width"),
    }


def expire_sessions():
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if now - v["created"] > SESSION_TTL]:
        old = SESSIONS.pop(k, None) or {}
        if old.get("local_dir"):
            shutil.rmtree(old["local_dir"], ignore_errors=True)


def save_session(url: str, info: dict) -> str:
    expire_sessions()
    now = time.time()
    formats = info.get("formats") or []
    preview = pick_preview(formats)
    token = uuid.uuid4().hex
    SESSIONS[token] = {
        "created": now,
        "url": url,
        "title": info.get("title") or "video",
        "preview": stream_ref(preview) if preview else None,
        "previews": preview_choices(formats, preview) if preview else [],
        "frames": [stream_ref(f) for f in pick_frame_sources(formats)],
        "thumb_candidates": thumbnail_candidates(info),
        "thumbs": None,  # filled in by /api/thumbnails after probing real sizes
    }
    return token


def format_selector(key: str, compat: bool = False) -> str:
    """Map a browser format key to a yt-dlp format selector string.

    compat (editor mode) prefers H.264 video + AAC audio at exactly the chosen
    height, which Premiere Pro & co. can open. If the site has no H.264 at that
    height (e.g. YouTube 1440p/4K is AV1/VP9 only), the normal choice is kept and
    the file is converted after download (see make_editor_friendly).
    """
    if key in ("audio", "mp3"):
        return "bestaudio[ext=m4a]/bestaudio/best"
    if key.startswith("fid:"):
        return f"{key[4:]}/best"
    if key.startswith("h"):
        h = int(key[1:])
        if HAS_FFMPEG:
            # One file with sound over plain HTTP streams straight to the browser. The "?"
            # lets in files whose codec isn't labelled (X, Facebook and Pinterest send H.264).
            single = "[acodec!=?none][protocol^=http]"
            if compat:
                first = (f"best[height={h}][vcodec!^=?av01][vcodec!^=?vp]{single}/"
                         f"bestvideo[height={h}][vcodec^=avc1]+bestaudio[ext=m4a]/"
                         f"best[height={h}][vcodec^=avc1]/")
            else:
                first = f"best[height={h}][vcodec!=none]{single}/"
            return (
                first +
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]/best"
            )
        return f"best[height<={h}][vcodec!=none][acodec!=none]/best"
    return "bestvideo+bestaudio/best" if HAS_FFMPEG else "best"


def parse_range(args) -> tuple[float, float] | None | str:
    """Read optional start/end (seconds). Returns (start, end), None, or an error string."""
    start, end = str(args.get("start") or "").strip(), str(args.get("end") or "").strip()
    if not start and not end:
        return None
    try:
        s, e = float(start or 0), float(end)
    except ValueError:
        return "Start and end must be times in seconds."
    if s < 0 or e - s < 0.1:
        return "The end time must be after the start time."
    return s, e


@app.route("/")
def index():
    return render_template("home.html", has_ffmpeg=HAS_FFMPEG)


@app.route("/download")
def download_page():
    return render_template("download.html", has_ffmpeg=HAS_FFMPEG)


@app.route("/classic")
def classic_page():
    """The previous single-page interface, kept available."""
    return render_template("index.html", has_ffmpeg=HAS_FFMPEG)


# ---------- Information pages ----------
# Set CONTACT_EMAIL (environment variable) to show a contact address on these pages.
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()
PAGES_UPDATED = "October 30, 2026"


def info_page(template: str, title: str, description: str):
    return render_template(template, page_title=title, page_description=description,
                           last_updated=PAGES_UPDATED, contact_email=CONTACT_EMAIL)


@app.route("/about-us")
@app.route("/about")
def about_page():
    return info_page("about.html", "About Us",
                     "What Video Downloader is, what it does and how it works.")


@app.route("/privacy-policy")
@app.route("/privacy")
def privacy_page():
    return info_page("privacy.html", "Privacy Policy",
                     "How Video Downloader handles your information: no accounts, no tracking.")


@app.route("/terms-and-conditions")
@app.route("/terms-of-service")
@app.route("/terms")
def terms_page():
    return info_page("terms.html", "Terms of Service",
                     "The rules for using Video Downloader.")


@app.route("/disclaimer")
def disclaimer_page():
    return info_page("disclaimer.html", "Disclaimer",
                     "Personal-use, copyright and no-affiliation notice for Video Downloader.")


def expand_short_link(url: str) -> str:
    """Follow redirects of a short link (pin.it) to the real page URL."""
    try:
        req = urllib.request.Request(url, headers=THUMB_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.geturl()
    except urllib.error.HTTPError as e:
        return e.geturl() or url  # e.g. TikTok: redirected to the video page, which then says 403
    except (urllib.error.URLError, OSError, ValueError):
        return url


# ---------- Post media (images + videos in a post) ----------

LOGIN_HINTS = ("login", "log in", "sign in", "authentication", "authorization", "cookies",
               "registered users", "private", "account authentication")


def needs_login(message: str) -> bool:
    low = (message or "").lower()
    return any(h in low for h in LOGIN_HINTS)


def media_item(url, kind, ext=None, width=None, height=None, headers=None) -> dict:
    ext = (ext or os.path.splitext(urlparse(url).path)[1].lstrip(".") or ("mp4" if kind == "video" else "jpg")).lower()
    return {"url": url, "kind": kind, "ext": {"jpeg": "jpg"}.get(ext, ext),
            "width": width, "height": height, "headers": headers or {}}


def gallery_cookie_args() -> list[str]:
    source = cookie_source()
    if source and source[0] == "file":
        return ["--cookies", source[1]]
    if source:
        return ["--cookies-from-browser", source[1]]
    return []


def post_meta_from(meta: dict) -> dict:
    """Title and author of a post from gallery-dl metadata (keys differ per site)."""
    text = next((meta.get(k) for k in ("content", "title", "description", "caption")
                 if isinstance(meta.get(k), str) and meta.get(k).strip()), "")
    author = meta.get("author") or meta.get("user") or meta.get("owner") or {}
    if isinstance(author, dict):
        author = author.get("nick") or author.get("name") or author.get("username") or ""
    author = author or meta.get("username") or meta.get("uploader") or ""
    text = re.sub(r"https?://\S+", "", " ".join(text.split())).strip()
    return {"title": text[:120] or None, "uploader": author if isinstance(author, str) else None}


def gallery_dl_media(url: str) -> tuple[list[dict], str | None, dict]:
    """List every file gallery-dl finds in a post. Returns (items, error, post meta)."""
    if not HAS_GALLERY_DL:
        return [], "gallery-dl is not installed (pip install gallery-dl).", {}
    cmd = [sys.executable, "-m", "gallery_dl", "--dump-json", *gallery_cookie_args()]
    try:
        r = subprocess.run([*cmd, url], capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
        messages = json.loads(r.stdout or "[]")
    except subprocess.TimeoutExpired:
        return [], "Timed out reading the post.", {}
    except ValueError:
        return [], (r.stderr.strip().splitlines() or ["Couldn't read the post."])[-1], {}

    items, error, post = [], None, {}
    for m in messages:
        if not m:
            continue
        if m[0] == -1:  # error message from the extractor
            info = m[1] if len(m) > 1 and isinstance(m[1], dict) else {}
            error = info.get("message") or info.get("error") or "Couldn't read the post."
        elif m[0] == 3 and len(m) > 2:  # a downloadable file
            file_url, meta = m[1], m[2] or {}
            post = post or post_meta_from(meta)
            if file_url.startswith("ytdl:"):
                continue  # videos that need yt-dlp are handled by the video workspace
            ext = (meta.get("extension") or "").lower()
            kind = "video" if ext in VIDEO_EXTS or meta.get("type") == "video" else "image"
            items.append(media_item(file_url, kind, ext, meta.get("width"), meta.get("height")))
    if not items and not error and r.stderr.strip():
        error = r.stderr.strip().splitlines()[-1]
    return items, error, post


def _base36(x: float) -> str:
    """JavaScript's Number.toString(36) for a positive float."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    ip, fp = int(x), x - int(x)
    out = ""
    while ip:
        ip, rem = divmod(ip, 36)
        out = digits[rem] + out
    out = out or "0"
    if fp:
        out += "."
        for _ in range(12):
            fp *= 36
            out += digits[int(fp)]
            fp -= int(fp)
            if not fp:
                break
    return out


def twitter_embed_media(url: str) -> list[dict]:
    """Photos/videos of a public post via X's embed data (no login needed)."""
    match = re.search(r"/status(?:es)?/(\d+)", url)
    if not match:
        return []
    tweet_id = match.group(1)
    token = re.sub(r"(0+|\.)", "", _base36(int(tweet_id) / 1e15 * math.pi))
    api = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en&token={token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(api, headers=THUMB_HEADERS), timeout=15) as r:
            data = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    items = []
    for m in data.get("mediaDetails") or []:
        size = m.get("original_info") or {}
        if m.get("type") == "photo" and m.get("media_url_https"):
            base, ext = os.path.splitext(m["media_url_https"])
            fmt = ext.lstrip(".") or "jpg"
            items.append(media_item(f"{base}?format={fmt}&name=orig", "image", fmt,
                                    size.get("width"), size.get("height")))
        elif m.get("video_info"):
            mp4s = [v for v in m["video_info"].get("variants", []) if v.get("content_type") == "video/mp4"]
            if mp4s:
                best = max(mp4s, key=lambda v: v.get("bitrate") or 0)
                items.append(media_item(best["url"], "video", "mp4", size.get("width"), size.get("height")))
    return items


def linkedin_page_media(url: str) -> tuple[list[dict], dict]:
    """Images of a public LinkedIn post, read from the post page."""
    try:
        req = urllib.request.Request(url, headers={**THUMB_HEADERS, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return [], {}
    post = {}
    og_title = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
    if og_title:
        text = html_lib.unescape(og_title.group(1))
        author, _, rest = text.partition(" on LinkedIn: ")
        post = {"title": (rest or text)[:120], "uploader": author if rest else None}
    found: dict[str, tuple[int, str]] = {}
    for raw in re.findall(r'https://media\.licdn\.com/dms/image/[^"\'\s<>]+', html):
        img = raw.replace("&amp;", "&")
        if "feedshare" not in img:
            continue  # skip avatars, logos and ads
        key = re.sub(r"/feedshare-[^/]+/", "/", img.split("?")[0])
        size = int((re.search(r"feedshare-shrink_(\d+)", img) or [0, 0])[1] or 0)
        if key not in found or size > found[key][0]:
            found[key] = (size, img)
    return [media_item(img, "image", "jpg") for _, img in found.values()], post


def ytdlp_entry_media(info: dict | None, post_url: str = "") -> list[dict]:
    """Images/videos from a yt-dlp result (e.g. Instagram carousels).

    Image-only entries have no formats; their largest thumbnail is the photo.
    Videos split into separate picture/sound streams are fetched with yt-dlp
    (by their position in the post) so the saved file has sound.
    """
    if not info:
        return []
    entries = info.get("entries") or [info]
    area = lambda f: ((f.get("width") or 0) * (f.get("height") or 0), f.get("tbr") or 0)
    items = []
    for position, e in enumerate(entries, start=1):
        if not e:
            continue
        videos = [f for f in e.get("formats") or [] if has_video(f)]
        if videos:
            best = max(videos, key=area)
            muxed = [f for f in videos if is_direct(f) and has_audio(f)]
            if muxed and area(max(muxed, key=area)) >= area(best):
                top = max(muxed, key=area)
                items.append(media_item(top["url"], "video", top.get("ext") or "mp4",
                                        top.get("width"), top.get("height"), top.get("http_headers")))
            else:
                item = media_item(e.get("webpage_url") or post_url, "video", "mp4",
                                  best.get("width"), best.get("height"))
                item["ytdl"] = {"url": post_url or e.get("webpage_url"),
                                "index": position if info.get("entries") else None}
                items.append(item)
            continue
        thumbs = [t for t in e.get("thumbnails") or [] if t.get("url")]
        if thumbs:
            best = max(thumbs, key=lambda t: ((t.get("width") or 0) * (t.get("height") or 0), t.get("preference") or 0))
            items.append(media_item(best["url"], "image", None, best.get("width"), best.get("height")))
    return items


def collect_post_media(site: str, url: str, info: dict | None) -> tuple[list[dict], str | None, dict]:
    """Every image/video in a post, using whichever source works for the site.

    Returns (items, error, post) where post holds the title/author if found.
    """
    items, error, post = [], None, {}
    if site == "instagram":
        items = ytdlp_entry_media(info, url)
        if not items:
            items, error, post = gallery_dl_media(url)
    elif site == "linkedin":
        items, post = linkedin_page_media(url)
        if not items:
            items = ytdlp_entry_media(info, url)
    elif site in MEDIA_SITES:
        items, error, post = gallery_dl_media(url)
        if not items and site == "twitter":
            items = twitter_embed_media(url)

    # Fill in missing sizes for images by reading the start of each file.
    missing = [i for i in items if i["kind"] == "image" and not (i["width"] and i["height"])]
    if missing:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for item, probed in zip(missing, pool.map(lambda i: probe_image(i["url"]), missing)):
                if probed:
                    item.update(width=probed["width"], height=probed["height"],
                                ext={"jpeg": "jpg"}.get(probed["format"], probed["format"]))
    # Drop exact duplicates, keep order.
    seen, unique = set(), []
    for i in items:
        key = (i["url"], (i.get("ytdl") or {}).get("index"))  # carousel videos share the post URL
        if key not in seen:
            seen.add(key)
            unique.append(i)
    return unique, (None if unique else error), post


def public_media(items: list[dict]) -> list[dict]:
    # "direct": the file can be previewed in the page as-is (not a yt-dlp merge job).
    return [{"index": n, "kind": i["kind"], "ext": i["ext"], "width": i["width"], "height": i["height"],
             "direct": not i.get("ytdl")}
            for n, i in enumerate(items)]


def has_playable_video(info: dict | None) -> bool:
    if not info:
        return False
    entry = next((e for e in (info.get("entries") or [info]) if e), {})
    return any(has_video(f) for f in entry.get("formats") or [])


AUDIO_ONLY_EXTS = ("mp3", "m4a", "aac", "opus", "ogg", "wav")


def tiktok_local(url: str) -> tuple[dict | None, str | None]:
    """Download a TikTok post into its own folder with gallery-dl.

    TikTok's file links only work inside gallery-dl's session (fetching them directly
    gives 403), so the post is saved locally and everything works from that copy.
    gallery-dl takes TikTok's player stream, which has no watermark.
    """
    if not HAS_GALLERY_DL:
        return None, "TikTok needs gallery-dl: run  pip install gallery-dl"
    folder = os.path.join(DOWNLOAD_ROOT, "tt-" + uuid.uuid4().hex)
    os.makedirs(folder)
    cmd = [sys.executable, "-m", "gallery_dl", *gallery_cookie_args(), "-D", folder,
           "--filename", "{id}_{num}.{extension}", "--write-metadata", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        shutil.rmtree(folder, ignore_errors=True)
        return None, "TikTok took too long to answer. Try again."
    files = sorted(os.path.join(folder, f) for f in os.listdir(folder) if not f.endswith(".json"))
    if not files:
        shutil.rmtree(folder, ignore_errors=True)
        err = [l for l in (r.stderr or "").splitlines() if "error" in l.lower()]
        return None, re.sub(r"^\[[^\]]*\]\[error\]\s*", "", (err or ["TikTok didn't return this post."])[-1])
    meta = {}
    for f in os.listdir(folder):
        if f.endswith(".json"):
            try:
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    meta = json.load(fh)
                break
            except (OSError, ValueError):
                pass
    post = post_meta_from(meta)
    author = meta.get("author") if isinstance(meta.get("author"), dict) else {}
    video = meta.get("video") if isinstance(meta.get("video"), dict) else {}
    covers = [video.get(k) for k in ("originCover", "cover", "dynamicCover") if isinstance(video.get(k), str)]
    return {
        "folder": folder, "files": files, "covers": covers,
        "title": (meta.get("desc") or post.get("title") or "TikTok video").strip()[:120] or "TikTok video",
        "uploader": author.get("nickname") or author.get("uniqueId") or post.get("uploader"),
    }, None


def local_video_response(site: str, url: str, local: dict, path: str):
    """Video workspace for a file we already have on disk (TikTok)."""
    m = probe_media(path)
    fmt = {"format_id": "local", "url": path, "protocol": "file", "ext": "mp4",
           "width": m["width"], "height": m["height"], "vcodec": m["vcodec"] or "none",
           "acodec": m["acodec"] or "none", "filesize": os.path.getsize(path)}
    expire_sessions()
    token = uuid.uuid4().hex
    SESSIONS[token] = {
        "created": time.time(), "url": url, "post_url": url, "site": site, "title": local["title"],
        "preview": {"path": path},
        "frames": [{"url": path, "headers": {}, "width": m["width"], "height": m["height"]}],
        "thumb_candidates": local["covers"], "thumbs": None,
        "local_file": path, "local_info": m, "local_dir": local["folder"], "media": [],
    }
    return jsonify(
        kind="video", site=site, title=local["title"], thumbnail=(local["covers"] or [None])[0],
        duration=m["duration"], uploader=local["uploader"], url=url, token=token,
        preview=f"/api/preview/{token}", frame_size=f"{m['width']}x{m['height']}" if m["width"] else None,
        fps=m.get("fps") or 30, can_edit=HAS_FFMPEG,
        exact_encoder=("NVIDIA GPU" if nvenc_available() else "CPU") if HAS_FFMPEG else None,
        media_available=False, options=build_options({"formats": [fmt]}),
    )


def local_gallery_response(site: str, url: str, local: dict):
    """Media gallery for files we already have on disk (TikTok photo posts)."""
    items = []
    for path in local["files"]:
        ext = path.rsplit(".", 1)[-1].lower()
        if ext in AUDIO_ONLY_EXTS:
            kind, w, h = "audio", None, None
        else:
            kind, w, h = "image", None, None
            try:
                with Image.open(path) as img:
                    w, h = img.size
            except Exception:  # noqa: BLE001
                kind = "video"
        item = media_item(path, kind, ext, w, h)
        item["path"] = path
        items.append(item)
    token = save_gallery_session(site, url, None, items, local["title"])
    SESSIONS[token]["local_dir"] = local["folder"]
    return jsonify(kind="gallery", site=site, token=token, url=url, title=local["title"],
                   uploader=local["uploader"], thumbnail=None, media=public_media(items))


@app.post("/api/info")
def video_info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    site = detect_site(url)
    if not site:
        return jsonify(error=f"Please enter a valid {SITE_NAMES} link."), 400
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host in SHORT_LINK_HOSTS:
        url = expand_short_link(url)
        site = detect_site(url)
        if not site:
            return jsonify(error="That short link doesn't lead to a supported post."), 422
    profile_hint = profile_link_message(site, url)
    if profile_hint:
        return jsonify(error=profile_hint, profile_link=True), 422

    if site == "tiktok":
        local, tt_error = tiktok_local(url)
        if not local:
            login = needs_login(tt_error or "")
            return jsonify(error=tt_error, login_required=login), 422
        videos = [f for f in local["files"] if f.rsplit(".", 1)[-1].lower() in VIDEO_EXTS]
        if videos:
            return local_video_response(site, url, local, videos[0])
        return local_gallery_response(site, url, local)

    # ignore_no_formats_error: image-only posts (e.g. Instagram photos) still return
    # their data instead of failing with "no video". Multi-media posts keep all items.
    opts = base_opts() | {"ignore_no_formats_error": True}
    if site in MEDIA_SITES - {"facebook", "pinterest"}:
        opts["noplaylist"] = False
    info, error = None, None
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        error = clean_error(e)
    except Exception as e:  # noqa: BLE001
        error = f"Unexpected error: {e}"

    entry = video_entry(info)
    if not entry:
        # No video: list the post's images (and any direct videos) instead.
        items, media_error, post = collect_post_media(site, url, info) if site in MEDIA_SITES else ([], None, {})
        if items:
            first = info or {}
            title = (first.get("title") or post.get("title") or first.get("description")
                     or f"{SITE_TITLES.get(site, 'Social')} post")
            token = save_gallery_session(site, url, info, items, title)
            return jsonify(
                kind="gallery", site=site, token=token, url=url, title=title,
                uploader=first.get("uploader") or first.get("channel") or post.get("uploader"),
                thumbnail=first.get("thumbnail"),
                media=public_media(items),
            )
        message = error or media_error or "No video or images were found in this post."
        login = needs_login(message) or (site == "reddit" and cookie_source() is None)
        if login and cookie_source() is None:
            message = (f"{SITE_TITLES.get(site, 'This site')} only shows this post to logged-in users. "
                       "Turn on Login in Settings to use your own browser login, then try again.")
        elif login:
            message += " Your saved login didn't work for this post; check the Login settings."
        return jsonify(error=message, login_required=login), 422

    page_url = entry.get("webpage_url") or url
    token = save_session(page_url, entry)
    session = SESSIONS[token]
    session.update(site=site, post_url=url, post_info=info if site in MEDIA_SITES else None)
    best = session["frames"][0] if session["frames"] else {}
    return jsonify(
        kind="video",
        site=site,
        title=entry.get("title") or "Video",
        thumbnail=entry.get("thumbnail"),
        duration=entry.get("duration"),
        uploader=entry.get("uploader") or entry.get("channel"),
        url=page_url,
        token=token,
        preview=f"/api/preview/{token}" if session["preview"] else None,
        previews=[{"label": p["label"], "sound": p["sound"], "default": p["default"]}
                  for p in session.get("previews", [])],
        frame_size=f"{best['width']}x{best['height']}" if best.get("width") else None,
        fps=entry.get("fps") or 30,
        can_edit=HAS_FFMPEG,
        exact_encoder=("NVIDIA GPU" if nvenc_available() else "CPU") if HAS_FFMPEG else None,
        media_available=site in MEDIA_SITES,
        options=build_options(entry),
    )


SITE_TITLES = {"facebook": "Facebook", "youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok",
               "twitter": "X (Twitter)", "reddit": "Reddit", "linkedin": "LinkedIn", "pinterest": "Pinterest"}


def video_entry(info: dict | None) -> dict | None:
    """The first item of a post that actually has video, or None."""
    if not info:
        return None
    for e in info.get("entries") or [info]:
        if e and any(has_video(f) for f in e.get("formats") or []):
            return e
    return None


def save_gallery_session(site: str, url: str, info: dict | None, items: list[dict], title: str) -> str:
    token = uuid.uuid4().hex
    SESSIONS[token] = {
        "created": time.time(), "url": url, "post_url": url, "site": site, "title": title,
        "preview": None, "frames": [], "thumb_candidates": [], "thumbs": [],
        "post_info": info, "media": items,
    }
    return token


# ---------- Media gallery endpoints ----------

@app.get("/api/media")
def list_media():
    session = SESSIONS.get(request.args.get("token", ""))
    if not session:
        return jsonify(error="Session expired. Fetch the post again."), 404
    if session.get("media") is None:
        items, error, _ = collect_post_media(session.get("site"), session.get("post_url") or session["url"],
                                             session.get("post_info"))
        session["media"] = items
        session["media_error"] = error
    return jsonify(media=public_media(session["media"]), error=session.get("media_error"))


def fetch_media(item: dict, timeout=60):
    if item.get("path"):
        return open(item["path"], "rb")  # already downloaded (TikTok)
    headers = {**THUMB_HEADERS, **(item.get("headers") or {})}
    return urllib.request.urlopen(urllib.request.Request(item["url"], headers=headers), timeout=timeout)


def ytdl_media_file(item: dict, job_dir: str) -> str:
    """Download one post video with yt-dlp (merging picture + sound). Returns the file path."""
    opts = base_opts() | {
        "format": "bestvideo*+bestaudio/best" if HAS_FFMPEG else "best",
        "outtmpl": os.path.join(job_dir, "item.%(ext)s"),
        "noplaylist": False,
    }
    if HAS_FFMPEG:
        opts["merge_output_format"] = "mp4"
    if item["ytdl"].get("index"):
        opts["playlist_items"] = str(item["ytdl"]["index"])
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(item["ytdl"]["url"], download=True)
    files = [os.path.join(job_dir, f) for f in os.listdir(job_dir)]
    if not files:
        raise RuntimeError("yt-dlp produced no file.")
    return max(files, key=os.path.getsize)


def media_filename(session: dict, index: int, item: dict) -> str:
    return f"{safe_filename(session['title'])} - {index + 1}.{item['ext']}"


def media_preview(session: dict, index: int, item: dict):
    """A small, cached JPEG of an image for the gallery cards (originals can be 20+ MP)."""
    cache = session.setdefault("previews", {})
    if index not in cache:
        try:
            with fetch_media(item) as r:
                data = r.read()
            with Image.open(BytesIO(data)) as img:
                img.thumbnail((640, 640))
                buf = BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=82)
            cache[index] = buf.getvalue()
        except Exception:  # noqa: BLE001
            return jsonify(error="Preview unavailable."), 502
    resp = send_file(BytesIO(cache[index]), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.get("/api/media/file")
def media_file():
    """Send one image/video of the post (inline=1 for in-page previews)."""
    session = SESSIONS.get(request.args.get("token", ""))
    if not session or not session.get("media"):
        return jsonify(error="Session expired. Fetch the post again."), 404
    try:
        index = int(request.args.get("i", "0"))
        item = session["media"][index]
    except (ValueError, IndexError):
        return jsonify(error="Unknown item."), 400
    if item.get("ytdl"):
        job_dir = os.path.join(DOWNLOAD_ROOT, uuid.uuid4().hex)
        os.makedirs(job_dir)
        try:
            path = ytdl_media_file(item, job_dir)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify(error=clean_error(e)), 502
        schedule_cleanup(job_dir)
        return send_file(path, as_attachment=request.args.get("inline") != "1",
                         download_name=media_filename(session, index, item))
    if request.args.get("inline") == "1" and item["kind"] == "image":
        return media_preview(session, index, item)
    if item.get("path"):
        return send_file(item["path"], as_attachment=request.args.get("inline") != "1",
                         download_name=media_filename(session, index, item), conditional=True)
    try:
        upstream = fetch_media(item)
    except urllib.error.HTTPError as e:
        return jsonify(error=f"The site refused the download (HTTP {e.code})."), 502
    except (urllib.error.URLError, OSError) as e:
        return jsonify(error=f"Couldn't download the file: {e}"), 502

    headers = {"Content-Type": upstream.headers.get("Content-Type") or "application/octet-stream"}
    if upstream.headers.get("Content-Length"):
        headers["Content-Length"] = upstream.headers["Content-Length"]
    if request.args.get("inline") != "1":
        name = media_filename(session, index, item)
        ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
        headers["Content-Disposition"] = (f'attachment; filename="{ascii_name}"; '
                                          f"filename*=UTF-8''{urllib.parse.quote(name)}")
    else:
        headers["Cache-Control"] = "private, max-age=3600"

    def generate():
        with upstream:
            while chunk := upstream.read(256 * 1024):
                yield chunk

    return Response(generate(), headers=headers)


@app.get("/api/media/zip")
def media_zip():
    """Every image/video of the post in one ZIP file."""
    session = SESSIONS.get(request.args.get("token", ""))
    if not session or not session.get("media"):
        return jsonify(error="Session expired. Fetch the post again."), 404
    job_dir = os.path.join(DOWNLOAD_ROOT, uuid.uuid4().hex)
    os.makedirs(job_dir)
    zip_path = os.path.join(job_dir, "media.zip")
    failed = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:  # images/videos are already compressed
        for index, item in enumerate(session["media"]):
            try:
                if item.get("ytdl"):
                    path = ytdl_media_file(item, tempfile.mkdtemp(dir=job_dir))
                    zf.write(path, media_filename(session, index, item))
                else:
                    with fetch_media(item, timeout=120) as r:
                        zf.writestr(media_filename(session, index, item), r.read())
            except Exception:  # noqa: BLE001 - skip files that fail, keep the rest
                failed += 1
    if failed == len(session["media"]):
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error="None of the files could be downloaded."), 502
    schedule_cleanup(job_dir)
    return send_file(zip_path, as_attachment=True, download_name=f"{safe_filename(session['title'])}.zip")


# ---------- Login settings ----------

@app.get("/api/settings")
def get_settings():
    source = cookie_source()
    return jsonify(
        browser=load_settings().get("cookies_browser") or "",
        browsers=list(COOKIE_BROWSERS),
        cookie_file=os.path.isfile(COOKIES_FILE),
        active=source[0] if source else None,
        gallery_dl=HAS_GALLERY_DL,
    )


@app.post("/api/settings")
def update_settings():
    browser = str((request.get_json(silent=True) or {}).get("browser") or "").lower()
    if browser and browser not in COOKIE_BROWSERS:
        return jsonify(error="Unknown browser."), 400
    data = load_settings()
    data["cookies_browser"] = browser
    save_settings(data)
    if browser:
        # Check right away that the browser's cookies can actually be read.
        try:
            from yt_dlp.cookies import extract_cookies_from_browser
            jar = extract_cookies_from_browser(browser)
            count = len(jar)
        except Exception as e:  # noqa: BLE001
            data["cookies_browser"] = ""
            save_settings(data)
            return jsonify(error=f"Couldn't read {browser.title()}'s login cookies: {e}. "
                                 "Close the browser and try again, use Firefox, or upload a cookies.txt file."), 422
        return jsonify(ok=True, cookies=count)
    return jsonify(ok=True)


@app.post("/api/settings/cookies")
def upload_cookies():
    upload = request.files.get("file")
    if not upload:
        return jsonify(error="Choose a cookies.txt file first."), 400
    text = upload.read(5 * 1024 * 1024).decode("utf-8", "replace")
    rows = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not rows or not all(len(l.split("\t")) >= 7 for l in rows[:20]):
        return jsonify(error="That isn't a cookies.txt file (Netscape format). "
                             "Export it with a 'Get cookies.txt' browser extension."), 400
    if not text.startswith("# Netscape HTTP Cookie File") and not text.startswith("# HTTP Cookie File"):
        text = "# Netscape HTTP Cookie File\n" + text  # the header yt-dlp and gallery-dl expect
    with open(COOKIES_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return jsonify(ok=True, cookies=len(rows))


@app.delete("/api/settings/cookies")
def delete_cookies():
    if os.path.isfile(COOKIES_FILE):
        os.remove(COOKIES_FILE)
    return jsonify(ok=True)


@app.get("/api/preview/<token>")
def preview(token):
    """Proxy the preview stream so the browser can play and seek it."""
    session = SESSIONS.get(token)
    if not session or not session["preview"]:
        return jsonify(error="Preview expired. Fetch the video again."), 404
    src = session["preview"]
    try:
        src = session.get("previews", [])[int(request.args["q"])]
    except (KeyError, ValueError, IndexError):
        pass
    if src.get("path"):
        return send_file(src["path"], mimetype="video/mp4", conditional=True)
    headers = dict(src["headers"])
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    try:
        upstream = urllib.request.urlopen(urllib.request.Request(src["url"], headers=headers), timeout=30)
    except urllib.error.HTTPError as e:
        return jsonify(error=f"Preview stream returned HTTP {e.code}."), 502
    except OSError as e:
        return jsonify(error=f"Preview stream failed: {e}"), 502

    passthrough = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
    out_headers = {k: upstream.headers[k] for k in passthrough if upstream.headers.get(k)}
    out_headers.setdefault("Accept-Ranges", "bytes")

    def generate():
        with upstream:
            while chunk := upstream.read(256 * 1024):
                yield chunk

    return Response(generate(), status=upstream.status, headers=out_headers)


# ---------- Thumbnails ----------

THUMB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}


def thumbnail_candidates(info: dict, limit: int = 10) -> list[str]:
    """Thumbnail URLs worth probing, most promising first.

    yt-dlp lists thumbnails worst to best, and its sizes aren't always right,
    so we only use its order here and measure the real images later.
    """
    thumbs = info.get("thumbnails") or []
    if not thumbs and info.get("thumbnail"):
        thumbs = [{"url": info["thumbnail"]}]
    ranked = sorted(
        enumerate(thumbs),
        key=lambda it: (it[1].get("preference") or 0, (it[1].get("width") or 0), it[0]),
        reverse=True,
    )
    urls = []
    for _, t in ranked:
        url = t.get("url")
        # YouTube's "?sqp=" variants are small resized copies of the same image.
        if url and url not in urls and "sqp=" not in url:
            urls.append(url)
    return urls[:limit]


def probe_image(url: str) -> dict | None:
    """Read just the start of an image to learn its real size and format."""
    for byte_range in ("bytes=0-131071", None):
        headers = dict(THUMB_HEADERS)
        if byte_range:
            headers["Range"] = byte_range
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
                data = r.read()
        except (urllib.error.URLError, OSError, ValueError):
            return None
        try:
            with Image.open(BytesIO(data)) as img:
                return {"url": url, "width": img.width, "height": img.height,
                        "format": (img.format or "jpeg").lower()}
        except Exception:  # noqa: BLE001 - header not in first chunk, retry with full file
            continue
    return None


def measure_thumbnails(urls: list[str]) -> list[dict]:
    """Probe candidates in parallel; keep one image per size, biggest first."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [t for t in pool.map(probe_image, urls) if t]
    by_size: dict[tuple, dict] = {}
    for t in found:  # urls are best-first, so the first hit per size wins...
        key = (t["width"], t["height"])
        prev = by_size.get(key)
        if prev is None or (prev["format"] == "webp" and t["format"] == "jpeg"):
            by_size[key] = t  # ...but prefer JPG over WebP for compatibility
    sizes = sorted(by_size.values(), key=lambda t: t["width"] * t["height"], reverse=True)
    kept: list[dict] = []
    for t in sizes:  # drop near-duplicates such as 1278x720 next to 1280x720
        if all(abs(t["width"] - k["width"]) > k["width"] * 0.02 or
               abs(t["height"] - k["height"]) > k["height"] * 0.02 for k in kept):
            kept.append(t)
    return [t for t in kept if t["width"] >= 120][:5] or kept[:1]


@app.get("/api/thumbnails")
def list_thumbnails():
    session = SESSIONS.get(request.args.get("token", ""))
    if not session:
        return jsonify(error="Session expired. Fetch the video again."), 404
    if session["thumbs"] is None:
        session["thumbs"] = measure_thumbnails(session["thumb_candidates"])
    ext = {"jpeg": "jpg"}
    return jsonify(thumbnails=[
        {"index": i, "width": t["width"], "height": t["height"],
         "format": ext.get(t["format"], t["format"])}
        for i, t in enumerate(session["thumbs"])
    ])


@app.get("/api/thumbnail")
def download_thumbnail():
    """Send one thumbnail as-is, or converted to JPG (quality 100) or PNG."""
    session = SESSIONS.get(request.args.get("token", ""))
    if not session or not session["thumbs"]:
        return jsonify(error="Session expired. Fetch the video again."), 404
    try:
        thumb = session["thumbs"][int(request.args.get("i", "0"))]
    except (ValueError, IndexError):
        return jsonify(error="Unknown thumbnail."), 400
    want = request.args.get("fmt", "original")
    if want not in ("original", "jpg", "png"):
        return jsonify(error="Invalid format."), 400

    try:
        req = urllib.request.Request(thumb["url"], headers=THUMB_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
    except (urllib.error.URLError, OSError) as e:
        return jsonify(error=f"Couldn't download the thumbnail: {e}"), 502

    src_ext = {"jpeg": "jpg"}.get(thumb["format"], thumb["format"])
    if want == "original" or want == src_ext:
        out, ext = data, src_ext
    else:
        with Image.open(BytesIO(data)) as img:
            buf = BytesIO()
            if want == "jpg":
                img.convert("RGB").save(buf, "JPEG", quality=100, subsampling=0, optimize=True)
            else:
                img.save(buf, "PNG", optimize=True)
        out, ext = buf.getvalue(), want

    name = f"{safe_filename(session['title'])} - thumbnail {thumb['width']}x{thumb['height']}.{ext}"
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "application/octet-stream")
    return send_file(BytesIO(out), mimetype=mime, as_attachment=True, download_name=name)


@app.get("/api/frame")
def frame():
    """Grab one full-resolution frame at time t from the best video stream."""
    session = SESSIONS.get(request.args.get("token", ""))
    if not session:
        return jsonify(error="Session expired. Fetch the video again."), 404
    if not HAS_FFMPEG:
        return jsonify(error="Frame capture needs ffmpeg."), 400
    if not session["frames"]:
        return jsonify(error="No video stream available for frame capture."), 422
    try:
        t = max(0.0, float(request.args.get("t", "0")))
    except ValueError:
        return jsonify(error="Invalid time."), 400
    fmt = "jpg" if request.args.get("fmt") == "jpg" else "png"

    job_dir = os.path.join(DOWNLOAD_ROOT, uuid.uuid4().hex)
    os.makedirs(job_dir)
    out = os.path.join(job_dir, f"frame.{fmt}")
    quality = ["-q:v", "1", "-qmin", "1"] if fmt == "jpg" else ["-compression_level", "6"]
    error = "Could not capture the frame."
    for src in session["frames"]:  # fall back to the next stream if decoding fails
        header_str = "".join(f"{k}: {v}\r\n" for k, v in src["headers"].items())
        cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{t:.3f}",
            *(["-headers", header_str] if header_str else []),
            "-i", src["url"],
            "-frames:v", "1", "-update", "1", *quality, out,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            error = "Frame capture timed out."
            continue
        if result.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            break
        error = (result.stderr.strip().splitlines() or [error])[-1]
    else:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=error), 422

    schedule_cleanup(job_dir)
    name = f"{safe_filename(session['title'])} - frame {stamp(t)}.{fmt}"
    return send_file(out, as_attachment=True, download_name=name)


_nvenc_ok: bool | None = None


def nvenc_available() -> bool:
    """True if ffmpeg can encode H.264 on an NVIDIA GPU (checked once)."""
    global _nvenc_ok
    if _nvenc_ok is None:
        _nvenc_ok = False
        if FFMPEG_PATH:
            try:
                r = subprocess.run(
                    [FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=size=1280x720:rate=30", "-t", "0.5",
                     "-c:v", "h264_nvenc", "-f", "null", "-"],
                    capture_output=True, timeout=30,
                )
                _nvenc_ok = r.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
    return _nvenc_ok


# ---------- Background jobs ----------

def new_job(**fields) -> dict:
    now = time.time()
    for k in [k for k, j in JOBS.items() if now - j["created"] > JOB_TTL]:
        shutil.rmtree(JOBS.pop(k)["dir"], ignore_errors=True)
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id, "created": now, "status": "running", "phase": "Starting",
        "percent": None, "speed": None, "eta": None, "error": None,
        "path": None, "name": None, "cancel": False,
        "dir": os.path.join(DOWNLOAD_ROOT, job_id), **fields,
    }
    os.makedirs(job["dir"])
    JOBS[job_id] = job
    return job


ERROR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "errors.log")


def log_error(job: dict, exc: BaseException, note: str = ""):
    """Print the full traceback to the console and append it to logs/errors.log."""
    lines = [
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] job {job['id']} {note}".rstrip(),
        f"  url={job.get('url')} format={job.get('key')} clip={job.get('clip')}",
        "".join(traceback.format_exception(exc)).rstrip(),
    ]
    text = "\n".join(lines)
    print("\n" + text, flush=True)
    try:
        os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")
    except OSError:
        pass


def friendly_error(exc: BaseException) -> str:
    """A readable message for unexpected crashes inside yt-dlp or ffmpeg."""
    if isinstance(exc, RuntimeError) and str(exc):
        return str(exc)  # our own messages are already readable
    detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return (f"The download failed because of an internal error ({detail}). "
            "Try again, or pick a different quality. Details were saved to logs/errors.log.")


def run_job(job: dict, target, *args):
    try:
        target(job, *args)
        job.update(status="done", phase="Done", percent=100, eta=0)
    except DownloadCancelled:
        job.update(status="cancelled", phase="Cancelled")
    except DownloadError as e:
        job.update(status="error", error=clean_error(e))
    except Exception as e:  # noqa: BLE001
        log_error(job, e)
        job.update(status="error", error=friendly_error(e))
    if job["status"] != "done":
        shutil.rmtree(job["dir"], ignore_errors=True)


EDITOR_VIDEO = {"h264"}
EDITOR_AUDIO = {"aac", "mp3"}


def probe_media(path: str) -> dict:
    """Video/audio codec, width and duration of a local file (read from ffmpeg's header dump)."""
    r = subprocess.run([FFMPEG_PATH, "-hide_banner", "-i", path], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    video = re.search(r"Stream #[^\n]*?: Video: (\w+)[^\n]*?, (\d{2,5})x(\d{2,5})", r.stderr)
    audio = re.search(r"Stream #[^\n]*?: Audio: (\w+)", r.stderr)
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return {
        "vcodec": video.group(1) if video else None,
        "width": int(video.group(2)) if video else 0,
        "height": int(video.group(3)) if video else 0,
        "acodec": audio.group(1) if audio else None,
        "duration": (int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3])) if dur else 0.0,
    }


def h264_args(width: int) -> list[str]:
    """High-quality H.264 settings: NVIDIA GPU when available, else the CPU."""
    if nvenc_available() and width <= 4096:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", "18", "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p"]


def run_ffmpeg(job: dict, cmd: list[str], duration: float, log_path: str):
    """Run ffmpeg, turning its -progress output into job percent / time left."""
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen([*cmd[:-1], "-progress", "pipe:1", "-nostats", cmd[-1]],
                                stdout=subprocess.PIPE, stderr=log, text=True)
        started = time.time()
        for line in proc.stdout:
            if job["cancel"]:
                proc.kill()
                break
            k, _, v = line.strip().partition("=")
            if k == "out_time_us" and v.isdigit() and duration:
                pct = min(99.9, int(v) / 1e6 / duration * 100)
                elapsed = time.time() - started
                job.update(percent=round(pct, 1), speed=None,
                           eta=round(elapsed * (100 - pct) / pct) if pct > 1 else None)
            elif k == "speed" and v.endswith("x"):
                job["speed_x"] = v
        proc.wait()
    if job["cancel"]:
        raise DownloadCancelled()
    if proc.returncode != 0:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        raise RuntimeError(lines[-1] if lines else "ffmpeg failed.")
    os.remove(log_path)


def make_editor_friendly(job: dict, path: str) -> str:
    """Convert a video to H.264 + AAC if it isn't already (AV1/VP9/Opus won't open in most editors)."""
    info = probe_media(path)
    if not info["vcodec"]:
        return path  # audio-only file
    video_ok = info["vcodec"] in EDITOR_VIDEO
    audio_ok = info["acodec"] in EDITOR_AUDIO or not info["acodec"]
    if video_ok and audio_ok and path.lower().endswith(".mp4"):
        return path
    out = os.path.splitext(path)[0] + ".editor.mp4"
    cmd = [FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", path,
           "-map", "0:v:0", "-map", "0:a:0?",
           *(["-c:v", "copy"] if video_ok else h264_args(info["width"])),
           *(["-c:a", "copy"] if audio_ok else ["-c:a", "aac", "-b:a", "192k"]),
           "-movflags", "+faststart", out]
    what = f"{info['vcodec'].upper()} video to H.264" if not video_ok else f"{(info['acodec'] or 'audio').upper()} audio to AAC"
    job.update(phase=f"Converting {what} for video editors",
               percent=0, speed=None, eta=None, speed_x=None)
    run_ffmpeg(job, cmd, info["duration"], os.path.join(job["dir"], "convert.log"))
    os.remove(path)
    final = os.path.splitext(path)[0] + ".mp4"
    os.replace(out, final)
    return final


def download_full(job: dict, url: str, key: str, compat: bool = True):
    """Download a whole video with yt-dlp, reporting progress into the job."""
    def on_progress(d):
        if job["cancel"]:
            raise DownloadCancelled()
        if d["status"] != "downloading":
            return
        f = d.get("info_dict") or {}
        part = "audio" if f.get("vcodec") == "none" else "video"
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        job.update(
            phase=f"Downloading {part}",
            percent=round(d["downloaded_bytes"] / total * 100, 1) if total else None,
            speed=d.get("speed"), eta=d.get("eta"),
        )

    def on_postprocess(d):
        if d["status"] == "started":
            names = {"Merger": "Merging video and audio", "ExtractAudio": "Converting to MP3"}
            job.update(phase=names.get(d.get("postprocessor"), "Finishing"), percent=None, speed=None, eta=None)

    opts = base_opts() | {
        "format": format_selector(key, compat),
        "outtmpl": os.path.join(job["dir"], "%(title).100s.%(ext)s"),
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_postprocess],
        "concurrent_fragment_downloads": 4,
    }
    if key in ("mp3", "audio") and HAS_FFMPEG:
        # Also covers sites with no audio-only stream (e.g. Pinterest): the
        # sound is pulled out of the video file. M4A is copied, not re-encoded.
        codec = "mp3" if key == "mp3" else "m4a"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "0"}
        ]
    elif HAS_FFMPEG:
        opts["merge_output_format"] = "mp4"

    job["phase"] = "Finding streams"
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except (DownloadCancelled, DownloadError):
        raise
    except Exception as e:  # noqa: BLE001 - yt-dlp internal crash (e.g. IndexError)
        if job["cancel"]:
            raise DownloadCancelled() from e
        # Retry once the safe way: one connection at a time, and let yt-dlp fall
        # back to the best single file if the chosen streams can't be fetched.
        log_error(job, e, "- retrying with safe settings")
        for name in os.listdir(job["dir"]):
            os.remove(os.path.join(job["dir"], name))
        opts["concurrent_fragment_downloads"] = 1
        opts["format"] = f"{opts['format']}/bestvideo*+bestaudio/best"
        job.update(phase="Retrying", percent=None, speed=None, eta=None)
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    files = [os.path.join(job["dir"], f) for f in os.listdir(job["dir"])]
    if not files:
        raise RuntimeError("Download failed: no file produced.")
    path = max(files, key=os.path.getsize)
    if compat and HAS_FFMPEG and key not in ("audio", "mp3"):
        path = make_editor_friendly(job, path)
    job.update(path=path, name=safe_filename(info.get("title", "")) + os.path.splitext(path)[1])


def local_convert(job: dict, session: dict):
    """Editor-friendly copy of a local video (e.g. TikTok's H.265 -> H.264)."""
    src = session["local_file"]
    dst = os.path.join(job["dir"], "video" + os.path.splitext(src)[1])
    shutil.copy2(src, dst)
    path = make_editor_friendly(job, dst)
    job.update(path=path, name=f"{safe_filename(session['title'])}.mp4")


def local_audio(job: dict, session: dict, key: str):
    """MP3 or M4A from a local video file."""
    m = session["local_info"]
    ext = "mp3" if key == "mp3" else "m4a"
    out = os.path.join(job["dir"], f"audio.{ext}")
    codec = ["-c:a", "libmp3lame", "-q:a", "0"] if ext == "mp3" else (
        ["-c:a", "copy"] if m["acodec"] in ("aac", "mp3") else ["-c:a", "aac", "-b:a", "192k"])
    job["phase"] = "Extracting audio"
    run_ffmpeg(job, [FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                     "-i", session["local_file"], "-vn", *codec, out],
               m["duration"], os.path.join(job["dir"], "audio.log"))
    job.update(path=out, name=f"{safe_filename(session['title'])}.{ext}")


def download_clip(job: dict, url: str, key: str, start: float, end: float, exact: bool,
                  compat: bool = True, session: dict | None = None):
    """Cut start-end straight from the source streams with ffmpeg.

    Fast mode copies the original streams (no quality loss, starts at the nearest
    keyframe). Exact mode re-encodes so the cut lands on the exact frame.
    """
    job["phase"] = "Finding streams"
    if session and session.get("local_file"):
        m = session["local_info"]
        info = {"title": session["title"], "duration": m["duration"]}
        streams = [{"url": session["local_file"], "http_headers": {}, "width": m["width"],
                    "vcodec": m["vcodec"] or "none", "acodec": m["acodec"] or "none", "ext": "mp4"}]
    else:
        with YoutubeDL(base_opts() | {"format": format_selector(key, compat)}) as ydl:
            info = ydl.extract_info(url, download=False)
        streams = info.get("requested_formats") or [info]
    vcodecs = [str(s.get("vcodec") or "") for s in streams if has_video(s)]
    acodecs = [str(s.get("acodec") or "") for s in streams if has_audio(s)]
    # Editor mode: a fast (copy) cut must not keep AV1/VP9/Opus, so encode only what needs it.
    video_copy_ok = not compat or all(c.startswith(("avc1", "h264")) or not c for c in vcodecs)
    audio_copy_ok = not compat or all(c.startswith(("mp4a", "aac", "mp3")) or not c for c in acodecs)
    duration = end - start
    if info.get("duration"):
        duration = max(0.1, min(end, info["duration"]) - start)

    cmd = [FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for s in streams:
        headers = s.get("http_headers") or info.get("http_headers") or {}
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        cmd += ["-ss", f"{start:.3f}", "-i", s["url"]]
    cmd += ["-t", f"{duration:.3f}"]
    for i in range(len(streams)):
        cmd += ["-map", f"{i}:v:0?", "-map", f"{i}:a:0?"]

    if key == "mp3":
        ext, codecs = "mp3", ["-vn", "-c:a", "libmp3lame", "-q:a", "0"]
    elif key == "audio":
        ext, codecs = "m4a", ["-vn", "-c:a", "copy"]
    else:
        ext = "mp4"
        width = max((s.get("width") or 0) for s in streams)
        encode_video = exact or not video_copy_ok
        video = h264_args(width) if encode_video else ["-c:v", "copy"]
        audio = ["-c:a", "copy"] if (audio_copy_ok and not exact) else ["-c:a", "aac", "-b:a", "192k"]
        codecs = [*video, *audio]
        if not encode_video:
            codecs += ["-avoid_negative_ts", "make_zero"]
    if ext == "mp4":
        codecs += ["-movflags", "+faststart"]

    out = os.path.join(job["dir"], f"clip.{ext}")
    cmd += [*codecs, out]
    if ext != "mp4" or ("-c:v", "copy") == tuple(codecs[:2]):
        job["phase"] = "Cutting clip"
    elif exact:
        job["phase"] = "Cutting and re-encoding clip"
    else:
        job["phase"] = "Cutting clip and converting to H.264 for video editors"
    run_ffmpeg(job, cmd, duration, os.path.join(job["dir"], "ffmpeg.log"))
    if not os.path.exists(out):
        raise RuntimeError("ffmpeg failed to cut the clip.")
    name = f"{safe_filename(info.get('title', ''))} - clip {stamp(start)}-{stamp(start + duration)}.{ext}"
    job.update(path=out, name=name)


DIRECT_CHUNK = 8 * 1024 * 1024  # fetch in 8 MB ranges: fast on YouTube/CDNs, and resumable
MIME = {"mp4": "video/mp4", "webm": "video/webm", "m4a": "audio/mp4", "mp3": "audio/mpeg"}


def attachment_header(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name)}'


def stream_info(url: str, key: str, compat: bool) -> dict | None:
    """The chosen quality's stream details (no download), or None if they can't be read."""
    if key == "mp3":
        return None
    try:
        with YoutubeDL(base_opts() | {"format": format_selector(key, compat)}) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001 - the prepared path will report the real error
        return None


def plan_direct(info: dict, key: str, compat: bool) -> dict | None:
    """If the chosen quality is one ready-to-use file, describe it so it can stream straight
    to the browser (real progress in the browser's download list). Otherwise None."""
    if info.get("requested_formats") or not is_direct(info):
        return None  # separate video + audio streams need joining first
    vcodec, acodec = str(info.get("vcodec") or ""), str(info.get("acodec") or "")
    if key != "audio" and not has_audio(info):
        return None
    if compat and key != "audio":
        if vcodec and vcodec != "none" and not vcodec.startswith(("avc1", "h264")):
            return None
        if acodec and acodec != "none" and not acodec.startswith(("mp4a", "aac", "mp3")):
            return None
    ext = info.get("ext") or ("m4a" if key == "audio" else "mp4")
    return {
        "url": info["url"], "headers": info.get("http_headers") or {}, "ext": ext,
        "size": info.get("filesize"),
        "name": f"{safe_filename(info.get('title', ''))}.{ext}",
    }


def remote_size(src: dict) -> int | None:
    req = urllib.request.Request(src["url"], headers={**THUMB_HEADERS, **src["headers"], "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            total = re.search(r"/(\d+)$", r.headers.get("Content-Range") or "")
            return int(total.group(1)) if total else int(r.headers.get("Content-Length") or 0) or None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def stream_direct(job: dict):
    """Proxy one remote file to the browser in 8 MB ranges, with resume (Range) support."""
    src = job["direct"]
    total = src.get("size") or remote_size(src)
    start, status = 0, 200
    wanted = re.match(r"bytes=(\d+)-", request.headers.get("Range") or "")
    if wanted and total:
        start, status = int(wanted.group(1)), 206
    headers = {"Content-Type": MIME.get(src["ext"], "application/octet-stream"),
               "Content-Disposition": attachment_header(src["name"]), "Accept-Ranges": "bytes"}
    if total:
        headers["Content-Length"] = str(total - start)
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"

    def generate():
        yield from ranged(src, start, total)
        job.update(status="done", phase="Done", percent=100)

    job.update(status="running", phase="Downloading in your browser")
    return Response(generate(), status=status, headers=headers)


def ranged(src: dict, start: int, total: int | None):
    """Yield a remote file from byte `start` in 8 MB ranges (fast on YouTube, retried on errors)."""
    pos, failures = start, 0
    while total is None or pos < total:
        end = pos + DIRECT_CHUNK - 1 if total is None else min(pos + DIRECT_CHUNK, total) - 1
        req = urllib.request.Request(src["url"], headers={**THUMB_HEADERS, **src["headers"],
                                                          "Range": f"bytes={pos}-{end}"})
        got = 0
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                while chunk := r.read(256 * 1024):
                    got += len(chunk)
                    yield chunk
        except (urllib.error.URLError, OSError):
            failures += 1
            if failures > 3:
                raise  # cut the stream: the browser marks the download as failed
        pos += got
        if got and total is None and got < DIRECT_CHUNK:
            break
        if not got and failures == 0:
            break


def plan_join(info: dict, key: str, compat: bool) -> dict | None:
    """Separate video + audio streams that can be joined on the fly (no re-encode) and whose
    sizes are known, so the browser can be told the file size before the join starts."""
    if not HAS_FFMPEG or key in ("audio", "mp3"):
        return None
    parts = info.get("requested_formats") or []
    if len(parts) != 2 or not all(is_direct(p) for p in parts):
        return None
    if compat:  # editor mode: only H.264 + AAC can be copied as they are
        for p in parts:
            v, a = str(p.get("vcodec") or "none"), str(p.get("acodec") or "none")
            if v != "none" and not v.startswith(("avc1", "h264")):
                return None
            if a != "none" and not a.startswith(("mp4a", "aac")):
                return None
    streams = []
    for p in parts:
        src = {"url": p["url"], "headers": p.get("http_headers") or info.get("http_headers") or {}}
        src["size"] = p.get("filesize") or remote_size(src)
        if not src["size"]:
            return None
        streams.append(src)
    raw = sum(s["size"] for s in streams)
    return {"streams": streams, "size": raw + raw // 100 + 2 * 1024 * 1024,  # room for MP4 boxes
            "name": f"{safe_filename(info.get('title', ''))}.mp4"}


CLIP_SLACK = 6.0     # seconds: a fast cut starts on the keyframe before the start, so allow for it
CLIP_MARGIN = 1.6    # bitrate varies through a video, so a clip can be denser than the average


def plan_clip(info: dict, key: str, compat: bool, start: float, end: float) -> dict | None:
    """A fast (copy) cut that can stream to the browser while ffmpeg makes it.

    The exact clip size is only known at the end, so the browser is given an estimate: the
    clip's share of the full streams' sizes, with room for the keyframe lead-in and denser
    scenes. If the clip still comes out larger, the stream fails and the page retries the
    clip the prepared way (see stream_join)."""
    if not HAS_FFMPEG or key == "mp3" or not info.get("duration"):
        return None
    parts = info.get("requested_formats") or [info]
    if not 1 <= len(parts) <= 2 or not all(is_direct(p) for p in parts):
        return None
    if compat:  # editor mode: the copied streams must already be H.264 / AAC
        for p in parts:
            v, a = str(p.get("vcodec") or "none"), str(p.get("acodec") or "none")
            if v != "none" and not v.startswith(("avc1", "h264")):
                return None
            if a != "none" and not a.startswith(("mp4a", "aac")):
                return None
    total_dur = float(info["duration"])
    end = min(end, total_dur)
    duration = end - start
    if duration <= 0:
        return None
    streams = []
    for p in parts:
        src = {"url": p["url"], "headers": p.get("http_headers") or info.get("http_headers") or {}}
        src["size"] = p.get("filesize") or remote_size(src)
        if not src["size"]:
            return None
        streams.append(src)
    raw = sum(s["size"] for s in streams)
    share = min(1.0, (duration + CLIP_SLACK) / total_dur * CLIP_MARGIN)
    ext = "m4a" if key == "audio" else "mp4"
    return {"streams": streams, "clip": (start, duration), "ext": ext,
            "size": int(raw * share) + raw // 100 + 2 * 1024 * 1024,
            "name": f"{safe_filename(info.get('title', ''))} - clip {stamp(start)}-{stamp(end)}.{ext}"}


def stream_join(job: dict):
    """Join video + audio with ffmpeg while sending it, under a size given up front.

    The browser needs the size at the start to show "12 / 64.7 MB" and a progress bar, but the
    joined file's exact size is only known at the end. So the size is a slight overestimate,
    and the rest is filled with an MP4 "free" box, which players and editors skip."""
    plan = job["join"]
    total = plan["size"]
    base = request.host_url.rstrip("/")
    clip = plan.get("clip")  # (start, duration) for a fast-cut clip
    cmd = [FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-nostdin"]
    for i in range(len(plan["streams"])):
        if clip:
            cmd += ["-ss", f"{clip[0]:.3f}"]
        cmd += ["-i", f"{base}/api/jobs/{job['id']}/source/{i}"]
    if clip:
        cmd += ["-t", f"{clip[1]:.3f}"]
    for i in range(len(plan["streams"])):
        cmd += ["-map", f"{i}:v:0?", "-map", f"{i}:a:0?"]
    cmd += ["-c", "copy"]
    if clip:
        cmd += ["-avoid_negative_ts", "make_zero"]
    cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    headers = {"Content-Type": MIME.get(plan.get("ext", "mp4"), "video/mp4"),
               "Content-Disposition": attachment_header(plan["name"]),
               "Content-Length": str(total), "Cache-Control": "no-store"}

    def generate():
        log = open(os.path.join(job["dir"], "join.log"), "wb")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log)
        sent = 0
        try:
            while chunk := proc.stdout.read(256 * 1024):
                if sent + len(chunk) > total - 16:
                    job["overflow"] = True  # the page retries a clip the prepared way
                    raise RuntimeError("The file came out larger than expected.")
                sent += len(chunk)
                job["percent"] = sent * 100 / total
                yield chunk
            if proc.wait() != 0 or not sent:
                raise RuntimeError("ffmpeg could not join the video and audio.")
            pad = total - sent
            yield (struct.pack(">I", pad) + b"free" if pad < 2 ** 32
                   else struct.pack(">I", 1) + b"free" + struct.pack(">Q", pad))
            pad -= 8 if pad < 2 ** 32 else 16
            while pad > 0:
                n = min(pad, 1024 * 1024)
                yield bytes(n)
                pad -= n
            job.update(status="done", phase="Done", percent=100)
        except Exception as e:
            job.update(status="error", error=str(e))
            raise  # cut the connection: the browser marks the download as failed
        finally:
            if proc.poll() is None:
                proc.kill()
            log.close()
            threading.Timer(60, shutil.rmtree, args=(job["dir"],), kwargs={"ignore_errors": True}).start()

    job.update(status="running", phase="Downloading in your browser", percent=0)
    return Response(generate(), headers=headers)


@app.get("/api/jobs/<job_id>/source/<int:i>")
def job_source(job_id, i):
    """One source stream of a join job, fetched in fast ranges, for the local ffmpeg."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return "Forbidden", 403
    job = JOBS.get(job_id)
    if not job or not job.get("join") or i >= len(job["join"]["streams"]):
        return "Unknown stream", 404
    src = job["join"]["streams"][i]
    total, start, status = src["size"], 0, 200
    wanted = re.match(r"bytes=(\d+)-", request.headers.get("Range") or "")
    headers = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes"}
    if wanted:
        start, status = int(wanted.group(1)), 206
        headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"
    headers["Content-Length"] = str(total - start)
    return Response(ranged(src, start, total), status=status, headers=headers)


def expected_name(title: str, key: str, clip) -> str:
    ext = "mp3" if key == "mp3" else "m4a" if key == "audio" else "mp4"
    name = safe_filename(title or "video")
    if clip:
        name += f" - clip {stamp(clip[0])}-{stamp(clip[1])}"
    return f"{name}.{ext}"


@app.get("/api/jobs/<job_id>/stream")
def job_stream(job_id):
    """The file as a normal browser download. Prepared files: the download appears at once,
    waits while the server joins/converts/trims, then completes."""
    job = JOBS.get(job_id)
    if not job:
        return "This download expired. Please start it again.", 404
    if job.get("direct"):
        return stream_direct(job)
    if job.get("join"):
        if job["status"] != "ready":
            return "This download was already started. Please start it again.", 409
        return stream_join(job)
    if job.get("direct_local"):
        job.update(status="done", phase="Done")
        return send_file(job["direct_local"], as_attachment=True, download_name=job["name"], conditional=True)

    if job["status"] == "done" and job.get("path") and os.path.exists(job["path"]):
        # Already prepared: send it with its size so the browser shows "x of 12 MB" and a progress bar.
        threading.Timer(300, shutil.rmtree, args=(job["dir"],), kwargs={"ignore_errors": True}).start()
        return send_file(job["path"], as_attachment=True, download_name=job["name"], conditional=True)

    name = job.get("expected_name") or "video.mp4"
    headers = {"Content-Type": MIME.get(name.rsplit(".", 1)[-1], "application/octet-stream"),
               "Content-Disposition": attachment_header(name), "Cache-Control": "no-store"}

    def generate():
        yield b""  # send the headers now so the browser lists the download immediately
        while job["status"] == "running":
            time.sleep(0.5)
        if job["status"] != "done" or not job.get("path") or not os.path.exists(job["path"]):
            # Abort the connection so the browser shows "Failed" instead of saving a broken file.
            raise RuntimeError(f"Download {job['id']} did not finish: {job.get('error') or job['status']}")
        with open(job["path"], "rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk
        threading.Timer(300, shutil.rmtree, args=(job["dir"],), kwargs={"ignore_errors": True}).start()

    return Response(generate(), headers=headers)


@app.post("/api/jobs")
def start_job():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    key = str(data.get("format", "best")).strip()
    if not detect_site(url):
        return jsonify(error=f"Invalid {SITE_NAMES} URL."), 400
    if not FORMAT_KEY.fullmatch(key):
        return jsonify(error="Invalid format."), 400
    if key == "mp3" and not HAS_FFMPEG:
        return jsonify(error="MP3 conversion needs ffmpeg."), 400
    clip = parse_range(data)
    if isinstance(clip, str):
        return jsonify(error=clip), 400
    if clip and not HAS_FFMPEG:
        return jsonify(error="Trimming needs ffmpeg."), 400

    compat = data.get("compat", True) is not False  # editor-friendly H.264 unless turned off
    title = str(data.get("title") or "")[:200]
    job = new_job(url=url, key=key, clip=clip, expected_name=expected_name(title, key, clip))

    session = SESSIONS.get(str(data.get("token") or ""))
    if session and session.get("local_file"):  # e.g. TikTok: the file is already on disk
        m = session["local_info"]
        ready = (not compat or (m["vcodec"] in EDITOR_VIDEO and m["acodec"] in EDITOR_AUDIO | {None}))
        if clip:
            args = (download_clip, url, key, clip[0], clip[1], bool(data.get("exact")), compat, session)
        elif key in ("audio", "mp3"):
            args = (local_audio, session, key)
        elif ready:
            name = f"{safe_filename(session['title'])}{os.path.splitext(session['local_file'])[1]}"
            job.update(direct_local=session["local_file"], status="ready", name=name)
            return jsonify(id=job["id"], direct=True, name=name)
        else:
            args = (local_convert, session)
        threading.Thread(target=run_job, args=(job, *args), daemon=True).start()
        return jsonify(id=job["id"], direct=False, name=job["expected_name"])

    live_clip = clip and not data.get("exact") and data.get("live", True) is not False
    info = stream_info(url, key, compat) if (not clip or live_clip) else None
    if clip:  # a fast cut streams to the browser while it's made, when the streams allow it
        plan = plan_clip(info, key, compat, *clip) if info else None
        if plan:
            job.update(join=plan, status="ready", phase="Ready to stream", name=plan["name"])
            return jsonify(id=job["id"], direct=True, live=True, name=plan["name"])
        info = None
    direct = plan_direct(info, key, compat) if info else None
    if direct:
        job.update(direct=direct, status="ready", phase="Ready to stream", name=direct["name"])
        return jsonify(id=job["id"], direct=True, name=direct["name"])
    join = plan_join(info, key, compat) if info else None
    if join:
        job.update(join=join, status="ready", phase="Ready to stream", name=join["name"])
        return jsonify(id=job["id"], direct=True, name=join["name"])

    if clip:
        args = (download_clip, url, key, clip[0], clip[1], bool(data.get("exact")), compat)
    else:
        args = (download_full, url, key, compat)
    threading.Thread(target=run_job, args=(job, *args), daemon=True).start()
    return jsonify(id=job["id"], direct=False, name=job["expected_name"])


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job not found."), 404
    fields = ("id", "status", "phase", "percent", "speed", "eta", "error", "name", "speed_x", "overflow")
    return jsonify({k: job.get(k) for k in fields})


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    job = JOBS.get(job_id)
    if job:
        job["cancel"] = True
    return jsonify(ok=True)


@app.get("/api/jobs/<job_id>/file")
def job_file(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done" or not job["path"] or not os.path.exists(job["path"]):
        return "File not ready or already removed. Please download again.", 404
    return send_file(job["path"], as_attachment=True, download_name=job["name"])


def schedule_cleanup(job_dir: str):
    @after_this_request
    def cleanup(response):
        # Delay removal so Windows releases the file handle first.
        threading.Timer(60, shutil.rmtree, args=(job_dir,), kwargs={"ignore_errors": True}).start()
        return response


def profile_link_message(site: str, url: str) -> str | None:
    """A friendly message when the link is an account/profile page instead of one post."""
    path = urlparse(url).path.lower().rstrip("/")
    post_markers = {
        "instagram": ("/p/", "/reel/", "/reels/", "/tv/", "/stories/"),
        "twitter": ("/status/", "/i/broadcasts/", "/i/spaces/"),
        "reddit": ("/comments/", "/gallery/", "/s/"),
        "linkedin": ("/posts/", "/feed/update/", "/events/", "/video/", "/learning/", "/pulse/"),
        "pinterest": ("/pin/",),
        "tiktok": ("/video/", "/photo/", "/v/", "/t/"),
    }
    markers = post_markers.get(site)
    if not markers or any(m in path + "/" for m in markers):
        return None
    host = (urlparse(url).hostname or "").lower()
    if host.startswith(("v.redd.it", "i.redd.it", "vm.tiktok.com", "vt.tiktok.com")):
        return None
    name = SITE_TITLES.get(site, "This site")
    what = {"instagram": "post or reel", "twitter": "post", "reddit": "post",
            "linkedin": "post", "pinterest": "pin", "tiktok": "video"}[site]
    article = "an" if name[0].lower() in "aeiou" else "a"
    return (f"That's {article} {name} profile or page link, not a single {what}. Open the {what} you want, "
            f"use Share → Copy link, and paste that link here.")


# yt-dlp's "please report this issue" / "use --cookies" boilerplate isn't useful in the app.
ERROR_BOILERPLATE = re.compile(
    r";?\s*(please report this issue on|Confirm you are on the latest version|"
    r"Use --cookies-from-browser or --cookies|See\s+https://github\.com/yt-dlp)[^\n]*", re.I)


def clean_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    msg = ERROR_BOILERPLATE.sub("", msg).strip().rstrip(";,. ") + "."
    msg = re.sub(r"^\[[\w:]+\]\s*[^:]{1,80}:\s*", "", msg) or msg  # drop "[instagram:user] id:" prefix
    low = msg.lower()
    if "unable to extract" in low:
        return ("Couldn't read this link. Make sure it points to a single public post, and try again. "
                "If it keeps failing, the site may have changed: update with  pip install -U yt-dlp gallery-dl")
    if "not a bot" in low:
        return ("YouTube is temporarily asking this computer to sign in (\"confirm you're not a bot\"), "
                "usually after many downloads in a short time. Wait a while and try again, or turn on "
                "Login and pick the browser where you're signed in to YouTube.")
    if "sign in to confirm your age" in low:
        return ("This video is age-restricted. Turn on Login with a browser where you're signed in "
                "to an adult account to download it.")
    if "login" in low or "private" in low or "sign in" in low:
        return "This post is private or requires login. Turn on Login to use your own account."
    return msg


def purge_old_jobs(max_age=3600):
    now = time.time()
    for name in os.listdir(DOWNLOAD_ROOT):
        p = os.path.join(DOWNLOAD_ROOT, name)
        if os.path.isdir(p) and now - os.path.getmtime(p) > max_age:
            shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    purge_old_jobs()
    print(f"ffmpeg: {FFMPEG_PATH or 'NOT found - HD merging, trimming and frame capture disabled (set FFMPEG_PATH)'}")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
