#!/bin/bash
# One-time setup for trio-test-harness on this Mac.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "== Homebrew packages"
if ! command -v brew >/dev/null; then echo "Homebrew is required (https://brew.sh)"; exit 1; fi
HOMEBREW_NO_AUTO_UPDATE=1 brew list --formula libusbmuxd >/dev/null 2>&1 || brew install libusbmuxd      # iproxy
HOMEBREW_NO_AUTO_UPDATE=1 brew list --formula tesseract  >/dev/null 2>&1 || brew install tesseract       # OCR fallback
HOMEBREW_NO_AUTO_UPDATE=1 brew list --formula coreutils  >/dev/null 2>&1 || brew install coreutils       # gtimeout
HOMEBREW_NO_AUTO_UPDATE=1 brew list --formula idb-companion >/dev/null 2>&1 || brew install facebook/fb/idb-companion || \
  echo "!! idb-companion failed to install; simulator taps need it (retry after Xcode is installed)"

echo "== Python virtualenv"
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== vendored tools (gitignored; cloned on demand)"
mkdir -p vendor
[ -d vendor/phone-harness/.git ] || git clone -q --depth 1 https://github.com/ShawnPana/phone-harness.git vendor/phone-harness   # iphone-usb mode (MIT)
.venv/bin/pip install -q -e vendor/phone-harness
[ -d vendor/WebDriverAgent/.git ] || git clone -q --depth 1 https://github.com/appium/WebDriverAgent.git vendor/WebDriverAgent  # simulator input

echo "== Config"
[ -f harness.env ] || { cp harness.env.example harness.env; echo "created harness.env — edit it (team id, VNC password)"; }

echo "== Claude Code skills"
mkdir -p "$HOME/.claude/skills"
for d in "$HERE"/.claude/skills/*/; do
  name="$(basename "$d")"
  target="$HOME/.claude/skills/$name"
  if [ -L "$target" ] || [ ! -e "$target" ]; then ln -sfn "$d" "$target"; echo "  linked ~/.claude/skills/$name"; else echo "  ~/.claude/skills/$name exists (not a symlink) — left alone"; fi
done

echo "== th on PATH (optional)"
echo "   add to your shell rc:  export PATH=\"$HERE/bin:\$PATH\"   (gives you trio-test-harness, trio-test-e2e and the th alias)"

echo
./trio-test-harness doctor || true
echo
echo "Next: ./bootstrap-xcode.sh if Xcode is missing; put TH_VNC_HOST/TH_VNC_PASSWORD/TH_DEVICE_PASSCODE in .env for the phone;"
echo "      then ./trio-test-harness doctor and ./trio-test-e2e --mode simulator --pr <url>"
