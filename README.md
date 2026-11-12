# Video Downloader

A local web app (Python + HTML) for downloading videos **and images** from public posts on
**Facebook, YouTube, Instagram, TikTok, X (Twitter), Reddit, LinkedIn and Pinterest**, including short
links (`pin.it`, `t.co`, `lnkd.in`, `redd.it`, `vm.tiktok.com`). Video comes as MP4 in any available quality
(up to 4K on YouTube), and audio as MP3 or M4A.

- **Photos and carousels:** posts with images (X photos, Instagram photos and carousels, LinkedIn image posts, Reddit galleries, Pinterest image pins) open in a media gallery. Save each photo at its original size, or everything at once as a ZIP. Posts that mix a video with photos show the video workspace plus the gallery.
- **Trim:** play the video in the page, mark a start and end (or type times like `1:23.5`), and download just that part in any quality. Two cut modes:
  - **Fast** (default): copies the original streams, so quality is untouched and it's ready in seconds. The clip may start up to ~2 s early because it begins on the nearest keyframe.
  - **Frame-exact**: re-encodes so the cut lands on the exact frame. Uses an NVIDIA GPU (NVENC) when available, otherwise the CPU.
- **For video editors (H.264), on by default:** YouTube's best qualities use AV1 or VP9 video, which Premiere Pro and many other editors can't open (they show only the audio). With this switch on, the app picks H.264 when the site offers it (YouTube up to 1080p) and otherwise converts the file to H.264 + AAC after download, on an NVIDIA GPU when available. Turn it off to keep the original codec and smaller files.
- **Downloads in your browser:** every download is a normal browser download, so it shows in the browser's download list and history (Ctrl+J) with pause, resume and cancel. Files that are ready to use (X, Instagram, Facebook, Pinterest, LinkedIn) stream straight from the site with real progress. YouTube HD (separate video and sound) is joined while it downloads, so it also shows its size and a progress bar at once; the size is a slight overestimate and the rest of the file is a padding ("free") box that players and editors skip. Fast-cut clips stream the same way while ffmpeg cuts them. Their size is an estimate (the clip's share of the full video, plus room for the keyframe lead-in and busier scenes), so clip files carry roughly 30–60% padding; if a clip still comes out bigger, the page makes it again the prepared way. Files that need re-encoding (H.264 conversion, frame-exact clips, MP3) are prepared first, with progress in the page corner, then download with their real size.
- **Thumbnail:** save the video's thumbnail in every real size the site offers, as the original file or converted to max-quality JPG or lossless PNG.
- **Capture frame:** pause on any frame (use the ±1 frame buttons or the `,` and `.` keys) and save it as a lossless PNG or max-quality JPG at the video's full resolution.

**Built with:** Flask, [yt-dlp](https://github.com/yt-dlp/yt-dlp) (videos), [gallery-dl](https://github.com/mikf/gallery-dl) (images), Pillow, and plain HTML/CSS/JavaScript (`templates/`, `static/`).

## Setup

```bash
pip install -r requirements.txt
```

This installs Flask, yt-dlp, gallery-dl and Pillow.

Optional but recommended: install [ffmpeg](https://ffmpeg.org/) and put it on your PATH. Many sites serve HD video and audio as separate streams, and ffmpeg is needed to merge them, make MP3s, trim clips and capture frames. If ffmpeg isn't on your PATH, set `FFMPEG_PATH` to the full path of `ffmpeg.exe`.

## Run

```bash
python app.py
```

Open http://127.0.0.1:5000, paste a link, click **Fetch video**, then pick a quality or save the images.

Pages: `/` is the landing page (`templates/home.html`), `/download?url=...` is the download workspace
(`templates/download.html`), and `/classic` is the previous single-page interface (`templates/index.html`),
which also holds the **Login** settings.

## What works without logging in

| Site | Videos | Images |
|---|---|---|
| YouTube, Facebook, Pinterest | Public videos | Thumbnails; Pinterest image pins |
| X (Twitter) | Public posts | Original-size photos |
| Instagram | Public reels and video posts | Public photo posts and carousels |
| TikTok | Public videos, **without watermark**, up to 1080p | Photo slideshows |
| LinkedIn | Public `/posts/` videos | Public image posts |
| Reddit | **Needs login** | **Needs login** |

## Login for private posts (optional)

Reddit, and some Instagram, X and LinkedIn posts, are only shown to logged-in users. Click **Login** in the top bar:

- **Use the login from my browser:** pick the browser you're logged in with. Firefox works best. Chrome and Edge often lock their login data while running; close them or use a cookies.txt file instead.
- **Or use a cookies.txt file:** export one while logged in, with a browser extension such as "Get cookies.txt LOCALLY", and upload it. It's saved as `cookies.txt` in this folder and takes priority over the browser setting.

Your login is only used on this computer to talk to those sites. `cookies.txt` and `settings.json` hold sensitive data: don't share them, and click **Remove** when you no longer need them.

## How it works

| Endpoint | Purpose |
|---|---|
| `POST /api/info` | Reads a link; returns a video (`kind: "video"`) with quality options, or a media gallery (`kind: "gallery"`) |
| `POST /api/jobs` | Starts a download job: `{ url, format, start?, end?, exact? }` returns `{ id }` |
| `GET /api/jobs/<id>` | Job progress: phase, percent, speed, time left, error |
| `POST /api/jobs/<id>/cancel` | Cancels a running job |
| `GET /api/jobs/<id>/file` | Sends the finished file |
| `GET /api/media?token=...` | Lists every image/video in the post |
| `GET /api/media/file?token=...&i=...` | Saves one item of the post (`inline=1` for previews) |
| `GET /api/media/zip?token=...` | All items of the post as one ZIP |
| `GET/POST /api/settings`, `POST/DELETE /api/settings/cookies` | Login settings |
| `GET /api/preview/<token>` | Streams a browser-playable copy of the video for the in-page player |
| `GET /api/thumbnails?token=...` | Lists the thumbnail sizes that really exist (each one is measured, not taken from metadata) |
| `GET /api/thumbnail?token=...&i=...&fmt=original\|jpg\|png` | Saves one thumbnail, optionally converted |
| `GET /api/frame?token=...&t=...&fmt=png\|jpg` | Saves one full-resolution frame at time `t` |

Temporary files are stored in your system temp folder (`video_downloader/`) and deleted shortly after each download. Unexpected download errors are logged to `logs/errors.log`.

## Notes

- Private, friends-only, group, members-only and age-restricted content needs a login (see above).
- Playlist links download only the single video in the link.
- TikTok goes through gallery-dl (yt-dlp's TikTok support is currently broken). The app saves TikTok's own player copy, which has no watermark, to a temporary folder when you click Fetch, and works from that copy. TikTok often uses H.265; with "For video editors" on it's converted to H.264, which makes the file larger.
- For YouTube, yt-dlp recommends installing the Deno JavaScript runtime (`winget install DenoLand.Deno`); without it some formats may be missing.
- If a site changes and downloads break, update the tools: `pip install -U yt-dlp gallery-dl`.
- Only download content you own or have permission to save, and respect each site's Terms of Service.
