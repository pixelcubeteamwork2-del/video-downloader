# Builds the Video Downloader desktop app into dist\Video Downloader\ (run from anywhere).
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1
# The folder can be copied to any Windows 10/11 PC; it needs Microsoft Edge (built in) or Chrome.
# For the Mac app, run desktop/build_mac.sh on a Mac (or let GitHub build both, see
# .github/workflows/desktop.yml).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

python -m pip install --quiet --upgrade pyinstaller -r requirements.txt

python -m PyInstaller --noconfirm --clean --windowed `
    --name "Video Downloader" `
    --icon "$root\desktop\app.ico" `
    --add-data "$root\templates;templates" `
    --add-data "$root\static;static" `
    --hidden-import app `
    --hidden-import mcp_server `
    --collect-submodules uvicorn `
    --collect-submodules gallery_dl `
    --collect-data gallery_dl `
    --distpath dist --workpath build --specpath build `
    desktop.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# Ship ffmpeg next to the program (HD joining, trims, MP3, frames and scenes need it).
$ffmpeg = $env:FFMPEG_PATH
if (-not $ffmpeg) { $ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source }
if (-not $ffmpeg -and (Test-Path "$HOME\bin\ffmpeg.exe")) { $ffmpeg = "$HOME\bin\ffmpeg.exe" }
if (-not $ffmpeg) {
    # No ffmpeg on this PC (e.g. a fresh PC or the GitHub build): use the self-contained one
    # from the imageio-ffmpeg package.
    python -m pip install --quiet --upgrade imageio-ffmpeg
    $ffmpeg = (python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())").Trim()
}
if ($ffmpeg) {
    Copy-Item $ffmpeg "dist\Video Downloader\ffmpeg.exe" -Force
    Write-Host "Included ffmpeg from $ffmpeg"
} else {
    Write-Warning "ffmpeg not found: the app will look for it on the PC it runs on."
}
# Ship deno too: yt-dlp uses it to unlock YouTube's formats on PCs that don't have it.
$deno = (Get-Command deno -ErrorAction SilentlyContinue).Source
if (-not $deno) {
    # Not installed: fetch the official single-file build.
    New-Item -ItemType Directory -Force build | Out-Null
    Invoke-WebRequest "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" -OutFile build\deno.zip
    Expand-Archive build\deno.zip -DestinationPath build\deno -Force
    $deno = "$root\build\deno\deno.exe"
}
if ($deno) {
    Copy-Item $deno "dist\Video Downloader\deno.exe" -Force
    Write-Host "Included deno from $deno"
} else {
    Write-Warning "deno not found: some YouTube qualities may be missing (winget install DenoLand.Deno)."
}

# One zip to share: unzip anywhere on another Windows 10/11 PC and run Video Downloader.exe.
Remove-Item "dist\Video Downloader.zip" -ErrorAction SilentlyContinue
tar -a -c -f "dist\Video Downloader.zip" -C dist "Video Downloader"
Write-Host "Zip: $root\dist\Video Downloader.zip"
Write-Host "Built:$root\dist\Video Downloader\Video Downloader.exe"
