"""Video Downloader as an MCP server, so AI assistants (Claude, ChatGPT, Codex, ...) can use it.

Tools: read a link, download a video or clip, download audio, get the transcript, capture a
frame (the assistant also sees it), save the thumbnail, and save every image of a post.
Files are saved to the Downloads/Video Downloader folder unless a folder is given.

Run:
    python mcp_server.py                 # stdio: Claude Desktop, Claude Code, Codex, Cursor...
    python mcp_server.py --http [PORT]   # Streamable HTTP on http://127.0.0.1:5758/mcp
The packaged app does the same with --mcp / --mcp-http [PORT]:
    Windows:  "Video Downloader.exe" --mcp
    macOS:    "/Applications/Video Downloader.app/Contents/MacOS/Video Downloader" --mcp
"""

import os
import re
import shutil
import sys
import time

# stdio MCP talks over stdout, so anything else that writes there (helpers, warnings) must not.
# Keep the real stdout for the protocol and point file descriptor 1 at stderr for everyone else.
_HTTP = "--http" in sys.argv or "--mcp-http" in sys.argv
if not _HTTP and sys.stdout is not None:
    _proto = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno() if sys.stderr else os.open(os.devnull, os.O_WRONLY), 1)
    sys.stdout = _proto

import anyio  # noqa: E402
from mcp.server.mcpserver import Context, Image, MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402  (its message reaches the assistant)

import app as vd  # noqa: E402  (the Flask app: same logic as the web page)

vd.print = lambda *a, **k: print(*a, **{**k, "file": sys.stderr})  # its error log line → stderr

API = vd.app.test_client()  # call the app's own endpoints in-process, no server needed
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads", "Video Downloader")
INFOS: dict[str, dict] = {}  # url -> /api/info answer (holds the session token)
MAX_TRANSCRIPT = 200_000  # characters returned inline; longer ones are also saved to a file

mcp = MCPServer(
    name="video-downloader",
    title="Video Downloader",
    instructions=(
        "Downloads videos, clips, audio, transcripts, frames, thumbnails and images from public "
        "posts on YouTube, Facebook, Instagram, TikTok, X, Reddit, LinkedIn and Pinterest. "
        "Call get_video_info first to see the qualities, length and transcripts. Times can be "
        "seconds (83.5) or m:ss / h:mm:ss (1:23.5). Files are saved on the user's computer and "
        "each tool returns the saved path. Only download content the user has the right to save."
    ),
)


# ---------- helpers ----------

def _json(resp) -> dict:
    data = resp.get_json(silent=True) or {}
    if resp.status_code >= 400:
        raise ToolError(data.get("error") or f"Request failed ({resp.status_code}).")
    return data


def _info(url: str) -> dict:
    """/api/info for a link (cached while its session lasts)."""
    data = _json(API.post("/api/info", json={"url": url.strip()}))
    INFOS[url] = data
    return data


def _token(url: str) -> tuple[str, dict]:
    """The link's session token and info, read again only when the session has expired."""
    info = INFOS.get(url)
    if not info or info["token"] not in vd.SESSIONS:
        info = _info(url)
    return info["token"], info


def _seconds(value) -> float | None:
    """83.5, "83.5", "1:23.5" or "1:02:03" -> seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ToolError(f"Can't read the time {value!r}. Use seconds or m:ss.") from None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def _folder(folder: str | None) -> str:
    path = os.path.abspath(os.path.expanduser(folder or DEFAULT_FOLDER))
    os.makedirs(path, exist_ok=True)
    return path


def _unique(folder: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 2
    while os.path.exists(path):
        path, n = os.path.join(folder, f"{base} ({n}){ext}"), n + 1
    return path


def _pick_quality(options: list[dict], quality: str) -> str:
    """'best', '1080p', '1080', '4k', 'h720' -> the closest available format key at or below it."""
    q = (quality or "best").lower().strip()
    if q in ("best", "highest", "max"):
        return "best"
    if any(o.get("format") == q for o in options):  # an exact key from get_video_info, e.g. h640
        return q
    q = {"4k": "2160", "2k": "1440", "uhd": "2160", "fhd": "1080", "hd": "720", "sd": "480"}.get(q, q)
    m = re.search(r"\d{3,4}", q)
    if not m:
        raise ToolError(f"Unknown quality {quality!r}. Use best, 1080p, 720p, ...")
    want = int(m.group())
    # Match the label ("360p SD"), not the key: for vertical videos the key is the long side
    # (360p is h640), so comparing keys would pick a much lower quality.
    by_label = sorted(((int(lm.group(1)), o["format"]) for o in options
                       if re.fullmatch(r"h\d+", o.get("format", ""))
                       and (lm := re.match(r"(\d{3,4})p", o.get("label", "")))), reverse=True)
    if not by_label:
        return "best"
    return next((key for p, key in by_label if p <= want), by_label[-1][1])


async def _run_job(ctx: Context | None, body: dict, folder: str | None) -> str:
    """Start a save-to-file job, report its progress, and move the result into the folder."""
    data = await anyio.to_thread.run_sync(lambda: _json(API.post("/api/jobs", json={**body, "save": True})))
    job = vd.JOBS[data["id"]]
    last = None
    while job["status"] in ("running", "ready"):
        await anyio.sleep(1)
        if ctx and (job.get("percent"), job.get("phase")) != last:
            last = (job.get("percent"), job.get("phase"))
            try:
                await ctx.report_progress(float(job.get("percent") or 0), 100.0, job.get("phase") or "Working")
            except Exception:  # noqa: BLE001 - progress is best-effort
                pass
    if job["status"] != "done":
        raise ToolError(job.get("error") or f"The download was {job['status']}.")
    dest = _unique(_folder(folder), job["name"])
    if job.get("keep"):
        shutil.copy2(job["path"], dest)
    else:
        shutil.move(job["path"], dest)
        shutil.rmtree(job["dir"], ignore_errors=True)
    return dest


def _saved(path: str) -> dict:
    return {"file": path, "size_mb": round(os.path.getsize(path) / 2**20, 2)}


# ---------- tools ----------

@mcp.tool()
async def get_video_info(url: str) -> dict:
    """Read a video or post link: title, site, length, the qualities you can download (with
    sizes), audio options and available transcripts. Image posts list their photos/videos."""
    info = await anyio.to_thread.run_sync(_info, url)
    out = {k: info.get(k) for k in ("kind", "site", "title", "uploader", "duration", "url")}
    if info.get("kind") == "gallery":
        media = await anyio.to_thread.run_sync(lambda: _json(API.get(f"/api/media?token={info['token']}")))
        out["media"] = media.get("media")
        return out
    out["resolution"] = info.get("frame_size")
    out["qualities"] = [
        {"quality": o["format"], "label": o["label"], "type": o["ext"],
         "size_mb": round(o["size"] / 2**20, 1) if o.get("size") else None}
        for o in info.get("options") or []
    ]
    tr = await anyio.to_thread.run_sync(lambda: _json(API.get(f"/api/transcripts?token={info['token']}")))
    out["transcripts"] = tr.get("transcripts")
    return out


@mcp.tool()
async def download_video(url: str, quality: str = "best", start: str | float | None = None, end: str | float | None = None,
                         frame_exact: bool = False, editor_friendly: bool = True,
                         folder: str | None = None, ctx: Context | None = None) -> dict:
    """Download a video as MP4, whole or a clip. quality: best, 2160p/4k, 1440p, 1080p, 720p,
    480p, 360p... (the closest available at or below is used). start/end make a clip (seconds or
    m:ss). frame_exact cuts on the exact frame (slower, re-encodes). editor_friendly makes an
    H.264 + AAC file that Premiere/DaVinci/CapCut open. Returns the saved file path."""
    token, info = await anyio.to_thread.run_sync(_token, url)
    if info.get("kind") == "gallery":
        raise ToolError("This post has no single video. Use download_images to save its media.")
    key = _pick_quality(info.get("options") or [], quality)
    body = {"url": url, "format": key, "compat": editor_friendly, "title": info.get("title"), "token": token}
    s, e = _seconds(start), _seconds(end)
    if s is not None or e is not None:
        body.update(start=s or 0.0, end=e if e is not None else info.get("duration"), exact=frame_exact)
    path = await _run_job(ctx, body, folder)
    return {**_saved(path), "quality": key}


@mcp.tool()
async def download_audio(url: str, format: str = "mp3", start: str | float | None = None, end: str | float | None = None,
                         folder: str | None = None, ctx: Context | None = None) -> dict:
    """Download only the sound: format mp3 (converted) or m4a (original, no quality loss).
    start/end save just that part (seconds or m:ss). Returns the saved file path."""
    fmt = format.lower().strip(". ")
    if fmt not in ("mp3", "m4a"):
        raise ToolError("format must be mp3 or m4a.")
    token, info = await anyio.to_thread.run_sync(_token, url)
    body = {"url": url, "format": "mp3" if fmt == "mp3" else "audio", "token": token,
            "title": info.get("title")}
    s, e = _seconds(start), _seconds(end)
    if s is not None or e is not None:
        body.update(start=s or 0.0, end=e if e is not None else info.get("duration"), exact=False)
    return _saved(await _run_job(ctx, body, folder))


@mcp.tool()
async def get_transcript(url: str, language: str | None = None, timestamps: bool = True,
                         save_to_file: bool = False, folder: str | None = None) -> dict:
    """Get a video's transcript (captions) as text, optionally with [m:ss] times. language is a
    code like en, es, si (default: the first one listed, usually the original language).
    save_to_file also saves it as a .txt file."""
    def work():
        token, _ = _token(url)
        tracks = _json(API.get(f"/api/transcripts?token={token}")).get("transcripts") or []
        if not tracks:
            raise ToolError("This video has no transcript or captions.")
        pick = tracks[0]
        if language:
            lang = language.lower()
            pick = next((t for t in tracks if t["lang"].lower() == lang), None) or \
                next((t for t in tracks if t["lang"].lower().startswith(lang.split("-")[0])), None)
            if not pick:
                raise ToolError(f"No {language} transcript. Available: {', '.join(t['lang'] for t in tracks)}")
        q = f"/api/transcript?token={token}&i={pick['index']}&timestamps={'1' if timestamps else '0'}"
        data = _json(API.get(q))
        out = {"language": pick["lang"], "name": pick["name"], "auto_generated": pick["auto"],
               "lines": data["lines"], "text": data["text"][:MAX_TRANSCRIPT]}
        if save_to_file or len(data["text"]) > MAX_TRANSCRIPT:
            title = vd.safe_filename(vd.SESSIONS[token]["title"])
            path = _unique(_folder(folder), f"{title} - transcript ({pick['lang']}).txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(data["text"])
            out["file"] = path
            if len(data["text"]) > MAX_TRANSCRIPT:
                out["note"] = "Text was shortened here; the full transcript is in the file."
        return out
    return await anyio.to_thread.run_sync(work)


@mcp.tool()
async def capture_frame(url: str, time: str | float, format: str = "jpg", folder: str | None = None) -> list:
    """Save one full-resolution frame at a time (seconds or m:ss) as jpg or png, and show it.
    Returns the saved path and the image itself so you can look at it."""
    fmt = "png" if format.lower().strip(". ") == "png" else "jpg"
    t = _seconds(time) or 0.0

    def work():
        token, _ = _token(url)
        resp = API.get(f"/api/frame?token={token}&t={t:.3f}&fmt={fmt}")
        if resp.status_code >= 400:
            _json(resp)
        title = vd.safe_filename(vd.SESSIONS[token]["title"])
        path = _unique(_folder(folder), f"{title} - frame {vd.stamp(t)}.{fmt}")
        with open(path, "wb") as f:
            f.write(resp.data)
        return path, resp.data

    path, data = await anyio.to_thread.run_sync(work)
    preview = data
    if len(data) > 900_000:  # keep what the assistant sees small; the saved file stays full size
        from io import BytesIO
        from PIL import Image as PILImage
        im = PILImage.open(BytesIO(data)).convert("RGB")
        im.thumbnail((1600, 1600))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=85)
        preview, fmt = buf.getvalue(), "jpg"
    return [f"Saved {path}", Image(data=preview, format="jpeg" if fmt == "jpg" else "png")]


@mcp.tool()
async def download_thumbnail(url: str, format: str = "original", folder: str | None = None) -> dict:
    """Save the video's thumbnail in the largest size the site has. format: original, jpg or png."""
    fmt = format.lower().strip(". ")
    if fmt not in ("original", "jpg", "png"):
        raise ToolError("format must be original, jpg or png.")

    def work():
        token, _ = _token(url)
        thumbs = _json(API.get(f"/api/thumbnails?token={token}")).get("thumbnails") or []
        if not thumbs:
            raise ToolError("No thumbnail found for this link.")
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        resp = API.get(f"/api/thumbnail?token={token}&i={best['index']}&fmt={fmt}")
        if resp.status_code >= 400:
            _json(resp)
        name = _download_name(resp) or f"{vd.safe_filename(vd.SESSIONS[token]['title'])} - thumbnail.jpg"
        path = _unique(_folder(folder), name)
        with open(path, "wb") as f:
            f.write(resp.data)
        return {**_saved(path), "width": best.get("width"), "height": best.get("height")}
    return await anyio.to_thread.run_sync(work)


def _download_name(resp) -> str | None:
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="([^"]+)"', cd)
    if not m:
        return None
    from urllib.parse import unquote
    return os.path.basename(unquote(m.group(1)))


@mcp.tool()
async def download_images(url: str, folder: str | None = None, ctx: Context | None = None) -> dict:
    """Save every photo (and video) of a post — Instagram carousels, X photos, Reddit galleries,
    Pinterest pins, LinkedIn image posts — at original size. Returns the saved file paths."""
    info = await anyio.to_thread.run_sync(_info, url)
    token = info["token"]
    media = (await anyio.to_thread.run_sync(lambda: _json(API.get(f"/api/media?token={token}")))).get("media") or []
    if not media:
        raise ToolError("No images or videos were found in this post.")
    saved = []
    for n, item in enumerate(media):
        if ctx:
            try:
                await ctx.report_progress(n, len(media), f"Saving item {n + 1} of {len(media)}")
            except Exception:  # noqa: BLE001
                pass

        def one(i=item["index"]):
            resp = API.get(f"/api/media/file?token={token}&i={i}")
            if resp.status_code >= 400:
                _json(resp)
            data = resp.get_data()
            name = _download_name(resp) or f"{vd.safe_filename(info.get('title') or 'media')} {i + 1}.bin"
            path = _unique(_folder(folder), name)
            with open(path, "wb") as f:
                f.write(data)
            return path
        saved.append(await anyio.to_thread.run_sync(one))
    return {"files": saved, "count": len(saved)}


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    if "--mcp" in argv:
        argv = [a for a in argv if a != "--mcp"]
    if "--http" in argv or "--mcp-http" in argv:
        flag = "--http" if "--http" in argv else "--mcp-http"
        rest = argv[argv.index(flag) + 1:]
        port = int(rest[0]) if rest and rest[0].isdigit() else 5758
        print(f"Video Downloader MCP on http://127.0.0.1:{port}/mcp", file=sys.stderr)
        mcp.run("streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
