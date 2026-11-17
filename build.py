#!/usr/bin/env python3
"""Build the downloadable Mac and Windows packages into dist/."""
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
VERSION = open(os.path.join(HERE, "VERSION")).read().strip()

APP_FILES = ["server.py", "apply.py", "jobs.py", "tailor.py", "render.py", "importer.py", "main.py", "updater.py",
             "VERSION", "README.md", "profile.example.yaml", "config.example.yaml", "requirements.txt", "urls.txt",
             "static/index.html", "static/login.html", "static/icon-192.png", "static/icon-512.png",
             "static/apple-touch-icon.png", "fonts/Carlito-Regular.ttf", "fonts/Carlito-Bold.ttf",
             "data/sample_jd.txt"]

# ------------------------------------------------------------------ Mac
MAC_LAUNCHER = r'''#!/bin/bash
# AutoApply for Mac. Native on Apple silicon (M1/M2/M3/M4) and Intel.
# The app's code is copied to ~/Library/Application Support/AutoApply/app so it can
# update itself; your data lives next to it and is never touched by updates.
BUNDLE="$(cd "$(dirname "$0")/../.." && pwd)"
BUNDLED="$BUNDLE/Contents/Resources/app"
SUPPORT="$HOME/Library/Application Support/AutoApply"
mkdir -p "$SUPPORT/data"
export AUTOAPPLY_HOME="$SUPPORT" AUTOAPPLY_LOGFILE=1
LOG="$SUPPORT/setup.log"
ARCH="$(uname -m)"            # arm64 on Apple silicon, x86_64 on Intel
say() { osascript -e "display notification \"$1\" with title \"AutoApply\"" >/dev/null 2>&1; }
fail() { osascript -e "display dialog \"$1\n\nDetails: ~/Library/Application Support/AutoApply/setup.log\" buttons {\"OK\"} with title \"AutoApply\" with icon caution" >/dev/null 2>&1; exit 1; }

# Once you've allowed AutoApply to open, clear the download flag from everything
# inside it so macOS doesn't block its parts one at a time.
xattr -dr com.apple.quarantine "$BUNDLE" 2>/dev/null || true

# Already running? Only trust a reply that is actually AutoApply (another program may use the same port).
PORT="$(cat "$SUPPORT/data/port" 2>/dev/null || echo 8765)"
for P in "$PORT" 8765; do
  if curl -fs -m 2 "http://127.0.0.1:$P/api/state" 2>/dev/null | grep -Eq '"app": *"autoapply"'; then open "http://127.0.0.1:$P"; exit 0; fi
done

# Download a self-contained Python built for this Mac's chip (no installer, nothing system-wide).
download_python() {
  local cpu="$ARCH"; [ "$cpu" = "arm64" ] && cpu="aarch64"
  local triple="$cpu-apple-darwin" api url sum tmp v
  api=$(curl -fsSL -m 30 -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" 2>>"$LOG")
  for v in 12 13; do
    url=$(printf '%s' "$api" | grep -oE "https://[^\"]*/cpython-3\.$v\.[0-9]+\+[0-9]+-$triple-install_only\.tar\.gz" | head -1)
    [ -n "$url" ] && break
  done
  [ -z "$url" ] && url="https://github.com/astral-sh/python-build-standalone/releases/download/20251217/cpython-3.14.2+20251217-$triple-install_only.tar.gz"
  echo "Downloading $url" >> "$LOG"
  tmp="$SUPPORT/python-download.tar.gz"
  curl -fL -m 900 -o "$tmp" "$url" 2>>"$LOG" || return 1
  sum=$(curl -fsSL -m 30 "$url.sha256" 2>/dev/null | grep -oE '^[0-9a-f]{64}')
  [ -z "$sum" ] && sum=$(curl -fsSL -m 30 "${url%/*}/SHA256SUMS" 2>/dev/null | grep -F "${url##*/}" | grep -oE '^[0-9a-f]{64}')
  if [ -n "$sum" ] && [ "$(shasum -a 256 "$tmp" | cut -d' ' -f1)" != "$sum" ]; then
    echo "Checksum mismatch" >> "$LOG"; rm -f "$tmp"; return 1
  fi
  rm -rf "$SUPPORT/python"
  tar -xzf "$tmp" -C "$SUPPORT" 2>>"$LOG" && rm -f "$tmp"
  "$SUPPORT/python/bin/python3" -c "import ssl, venv" 2>>"$LOG"
}

# Pick a Python that runs natively on this Mac.
native() { "$1" -c 'import sys,platform; sys.exit(0 if sys.version_info >= (3,9) and platform.machine() == sys.argv[1] else 1)' "$ARCH" >/dev/null 2>&1; }
PY=""
[ -x "$SUPPORT/python/bin/python3" ] && native "$SUPPORT/python/bin/python3" && PY="$SUPPORT/python/bin/python3"
if [ -z "$PY" ]; then
  CANDS="/opt/homebrew/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/local/bin/python3"
  xcode-select -p >/dev/null 2>&1 && CANDS="$CANDS /usr/bin/python3"   # skip Apple's stub so no install popup appears
  for c in $CANDS; do [ -x "$c" ] && native "$c" && { PY="$c"; break; }; done
fi
if [ -z "$PY" ]; then
  say "Getting AutoApply ready for your Mac (one-time download, a few minutes)…"
  download_python && PY="$SUPPORT/python/bin/python3"
fi
if [ -z "$PY" ]; then
  r=$(osascript -e 'button returned of (display dialog "AutoApply could not download Python automatically (check your internet connection).\n\nYou can install it yourself: click Download, run the installer, then open AutoApply again." buttons {"Cancel","Download"} default button 2 with title "AutoApply")')
  [ "$r" = "Download" ] && open "https://www.python.org/downloads/macos/"
  exit 1
fi

# Copy the bundled code in on first run, or when this download is newer than what's installed
APP="$SUPPORT/app"
newer() { "$PY" -c 'import sys; v=lambda s: tuple(int("0"+"".join(c for c in p if c.isdigit())) for p in s.strip().split(".")); sys.exit(0 if v(sys.argv[1]) > v(sys.argv[2]) else 1)' "$1" "$2"; }
if [ ! -f "$APP/server.py" ] || newer "$(cat "$BUNDLED/VERSION")" "$(cat "$APP/VERSION" 2>/dev/null || echo 0)"; then
  rm -rf "$APP.tmp" && cp -R "$BUNDLED" "$APP.tmp" && rm -rf "$APP" && mv "$APP.tmp" "$APP" || fail "Couldn't install AutoApply's files."
fi

VENV="$SUPPORT/.venv"
# (Re)create the environment only if it's missing or built for a different chip
if [ ! -x "$VENV/bin/python" ] || ! native "$VENV/bin/python"; then
  say "Setting up for the first time. This takes a few minutes; AutoApply opens when it's ready."
  rm -rf "$VENV"
  "$PY" -m venv "$VENV" >> "$LOG" 2>&1 || fail "Setup failed while creating AutoApply's environment."
  "$VENV/bin/python" -m pip install -q --upgrade pip >> "$LOG" 2>&1
  rm -f "$VENV/.req"
fi
REQ="$(shasum "$APP/requirements.txt" | cut -d' ' -f1)"
if [ "$(cat "$VENV/.req" 2>/dev/null)" != "$REQ" ]; then
  "$VENV/bin/python" -m pip install -q -r "$APP/requirements.txt" >> "$LOG" 2>&1 || fail "Setup failed. Check your internet connection and open AutoApply again."
  if [ ! -d "$HOME/Library/Caches/ms-playwright" ]; then
    say "Downloading the browser AutoApply uses to fill out applications…"
    "$VENV/bin/python" -m playwright install chromium >> "$LOG" 2>&1 || true   # falls back to Google Chrome
  fi
  echo "$REQ" > "$VENV/.req"
fi

cd "$APP"
nohup "$VENV/bin/python" "$APP/server.py" >/dev/null 2>&1 &
disown
'''

INFO_PLIST = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>AutoApply</string>
  <key>CFBundleDisplayName</key><string>AutoApply</string>
  <key>CFBundleIdentifier</key><string>local.autoapply.app</string>
  <key>CFBundleVersion</key><string>{VERSION}</string>
  <key>CFBundleShortVersionString</key><string>{VERSION}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>AutoApply</string>
  <key>CFBundleIconFile</key><string>AutoApply</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string><string>x86_64</string></array>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
'''

MAC_README = """AutoApply for Mac
=================

1. Drag AutoApply into your Applications folder.
2. Open it. The first time, macOS may say it can't verify the developer:
     • Right-click AutoApply → Open → Open
     • On newer macOS, if there's no Open button: System Settings → Privacy & Security,
       scroll down, click "Open Anyway"
     • If it says the app is "damaged": open Terminal and run
         xattr -dr com.apple.quarantine /Applications/AutoApply.app
   You only do this once.
3. The first launch sets itself up (a few minutes), then opens in your browser.
   Runs natively on Apple silicon (M1/M2/M3/M4) and Intel Macs. You don't need to
   install Python: if your Mac doesn't have it, AutoApply downloads its own copy.

To quit: click "Quit AutoApply" at the bottom left of the app.
Your resumes are saved in: ~/Library/Application Support/AutoApply/Resumes
  (or click "Open resumes folder" in the app)
Phone: open Settings → Use on my phone.
"""

# ------------------------------------------------------------------ Windows
WIN_INSTALL = r'''@echo off
title AutoApply setup
cd /d "%~dp0"
echo.
echo   Installing AutoApply...
echo.
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 (
  echo   AutoApply needs Python, which is free. Opening the download page...
  echo   IMPORTANT: on the first installer screen, tick "Add python.exe to PATH".
  echo   Then run this installer again.
  start https://www.python.org/downloads/windows/
  pause & exit /b 1
)
if not exist .venv\Scripts\python.exe %PY% -m venv .venv
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt || (echo. & echo   Install failed. Check your internet connection and run this again. & pause & exit /b 1)
echo   Downloading the browser used to fill out applications...
.venv\Scripts\python -m playwright install chromium || echo   (Skipped. AutoApply will use Chrome or Edge instead.)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make-shortcuts.ps1"

echo.
echo   Done! AutoApply is on your Desktop and in the Start menu.
echo   Opening it now...
start "" ".venv\Scripts\pythonw.exe" "%~dp0server.py"
timeout /t 4 >nul
'''

WIN_SHORTCUTS = r"""# Creates AutoApply shortcuts on the Desktop and in the Start menu.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$shell = New-Object -ComObject WScript.Shell
$targets = @([Environment]::GetFolderPath('Desktop'),
             (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))
foreach ($dir in $targets) {
    $lnk = $shell.CreateShortcut((Join-Path $dir 'AutoApply.lnk'))
    $lnk.TargetPath = Join-Path $here '.venv\Scripts\pythonw.exe'
    $lnk.Arguments = [char]34 + (Join-Path $here 'server.py') + [char]34
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation = Join-Path $here 'AutoApply.ico'
    $lnk.Description = 'AutoApply'
    $lnk.Save()
}
"""

WIN_README = """AutoApply for Windows
=====================

1. Move this AutoApply folder somewhere permanent, like Documents. Your data is saved inside it.
2. Double-click "Install AutoApply". It takes 1–3 minutes the first time.
   If Windows SmartScreen appears: click "More info" → "Run anyway".
3. From then on, open AutoApply from your Desktop or the Start menu.

To quit: click "Quit AutoApply" at the bottom left of the app.
Your resumes: click "Open resumes folder" in the app.
Phone: open Settings → Use on my phone. If Windows asks whether to allow Python on
private networks, click Allow.
"""


def add(z, src_rel, arc, mode=0o644):
    full = os.path.join(HERE, src_rel)
    info = zipfile.ZipInfo.from_file(full, arc)
    info.external_attr = (0o100000 | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(info, open(full, "rb").read())


def add_text(z, arc, text, mode=0o644):
    info = zipfile.ZipInfo(arc)
    info.external_attr = (0o100000 | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(info, text)


def add_dir(z, arc):
    info = zipfile.ZipInfo(arc.rstrip("/") + "/")
    info.external_attr = (0o40755 << 16) | 0x10
    z.writestr(info, "")


def build_mac():
    out = os.path.join(DIST, "AutoApply-Mac.zip")
    base = "AutoApply.app/Contents"
    with zipfile.ZipFile(out, "w") as z:
        for d in ("AutoApply.app", base, f"{base}/MacOS", f"{base}/Resources", f"{base}/Resources/app"):
            add_dir(z, d)
        add_text(z, f"{base}/Info.plist", INFO_PLIST)
        add_text(z, f"{base}/MacOS/AutoApply", MAC_LAUNCHER, 0o755)
        add(z, "assets/AutoApply.icns", f"{base}/Resources/AutoApply.icns")
        for f in APP_FILES:
            add(z, f, f"{base}/Resources/app/{f}")
        add_text(z, "Read me.txt", MAC_README)
    return out


def build_windows():
    out = os.path.join(DIST, "AutoApply-Windows.zip")
    with zipfile.ZipFile(out, "w") as z:
        add_dir(z, "AutoApply")
        for f in APP_FILES:
            add(z, f, f"AutoApply/{f}")
        add(z, "assets/AutoApply.ico", "AutoApply/AutoApply.ico")
        add_text(z, "AutoApply/Install AutoApply.bat", WIN_INSTALL.replace("\n", "\r\n"))
        add_text(z, "AutoApply/make-shortcuts.ps1", WIN_SHORTCUTS.replace("\n", "\r\n"))
        add_text(z, "AutoApply/Read me.txt", WIN_README.replace("\n", "\r\n"))
        add_dir(z, "AutoApply/output")
    return out


if __name__ == "__main__":
    os.makedirs(DIST, exist_ok=True)
    for p in (build_mac(), build_windows()):
        print(f"{os.path.getsize(p) // 1024:>5} KB  {p}")
