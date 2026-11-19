#!/usr/bin/env bash
# Builds the Video Downloader Mac app: dist/Video Downloader.app, plus a zip to share.
#   bash desktop/build_mac.sh
# Run it on a Mac (PyInstaller can only build Mac apps on a Mac). The app is built for the
# Mac you run it on: Apple Silicon (arm64) or Intel (x86_64). Needs Python 3.10+ (python.org
# or Homebrew) and an internet connection the first time. The finished app needs nothing
# installed: Python, ffmpeg and Deno are inside it. It opens in Chrome, Edge or Brave.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ARCH="$(uname -m)"                      # arm64 or x86_64
PY="${PYTHON:-python3}"
APP="dist/Video Downloader.app"
mkdir -p build/bin

echo "==> Python packages (in build/venv-mac, your system Python is left alone)"
"$PY" -m venv build/venv-mac
# shellcheck disable=SC1091
source build/venv-mac/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet --upgrade pyinstaller imageio-ffmpeg -r requirements.txt

echo "==> ffmpeg (a self-contained build, so it works on Macs without Homebrew)"
if [ -n "${FFMPEG_PATH:-}" ]; then
  SRC_FFMPEG="$FFMPEG_PATH"
else
  SRC_FFMPEG="$(python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
fi
case "$SRC_FFMPEG" in
  /opt/homebrew/*|/usr/local/Cellar/*|/usr/local/opt/*)
    echo "   warning: $SRC_FFMPEG is Homebrew's ffmpeg, which needs Homebrew's libraries;"
    echo "            the app will only work on Macs that have them. Unset FFMPEG_PATH to use a self-contained one." ;;
esac
cp "$SRC_FFMPEG" build/bin/ffmpeg && chmod +x build/bin/ffmpeg
build/bin/ffmpeg -hide_banner -version | head -1

echo "==> Deno (lets yt-dlp unlock every YouTube quality)"
if command -v deno >/dev/null 2>&1; then
  cp "$(command -v deno)" build/bin/deno
else
  case "$ARCH" in arm64) TRIPLE="aarch64-apple-darwin" ;; *) TRIPLE="x86_64-apple-darwin" ;; esac
  curl -fsSL -o build/deno.zip "https://github.com/denoland/deno/releases/latest/download/deno-$TRIPLE.zip"
  unzip -o -q build/deno.zip -d build/bin
fi
chmod +x build/bin/deno
build/bin/deno --version | head -1

echo "==> Building the app"
python -m PyInstaller --noconfirm --clean --windowed \
  --name "Video Downloader" \
  --icon "$ROOT/desktop/app.icns" \
  --osx-bundle-identifier "app.videodownloader.desktop" \
  --add-data "$ROOT/templates:templates" \
  --add-data "$ROOT/static:static" \
  --add-binary "$ROOT/build/bin/ffmpeg:." \
  --add-binary "$ROOT/build/bin/deno:." \
  --hidden-import app \
  --hidden-import mcp_server \
  --collect-submodules uvicorn \
  --collect-submodules gallery_dl \
  --collect-data gallery_dl \
  --exclude-module imageio_ffmpeg \
  --exclude-module numpy \
  --distpath dist --workpath build --specpath build \
  desktop.py

# The app itself has no window of its own (it opens a Chrome/Edge app window), so keep it out
# of the Dock; otherwise its icon would bounce forever. Then sign it again (ad-hoc), which
# Apple Silicon requires after the Info.plist changes.
plutil -replace LSUIElement -bool YES "$APP/Contents/Info.plist"
plutil -replace CFBundleDisplayName -string "Video Downloader" "$APP/Contents/Info.plist"
codesign --force --deep --sign - "$APP"

ZIP="dist/Video-Downloader-mac-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"   # ditto keeps the app's permissions and signature

echo
echo "Built: $ROOT/$APP"
echo "Zip:   $ROOT/$ZIP"
echo "Install: unzip, drag 'Video Downloader' into Applications, then right-click it > Open the first time."
echo "AI assistants (MCP): \"/Applications/Video Downloader.app/Contents/MacOS/Video Downloader\" --mcp"
