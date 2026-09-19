#!/bin/bash
# Install Xcode on this Mac without the App Store.
#
# Facts for this machine (iMac18,1, macOS 15.7.x): the newest Xcode that runs on macOS 15 is
# Xcode 26.3 (build 17C529). Xcode 26.4+ and 27 require macOS 26, which this iMac cannot run.
# Downloading Xcode requires an Apple ID, so this script needs ONE of:
#   a) an Xcode xip already in ~/Downloads  (https://developer.apple.com/download/all/?q=Xcode%2026.3
#      -> "Xcode 26.3" -> Xcode_26.3_Universal.xip  — Universal, not Apple_silicon, for this Intel Mac)
#   b) `xcodes` (installed at ~/.local/bin/xcodes from the GitHub release; brew cannot build it without Xcode)
#      — it will prompt for your Apple ID + 2FA:  xcodes install 26.3 --experimental-unxip --directory /Applications
#
# Steps marked (sudo) prompt for your password.
set -euo pipefail
WANT="${XCODE_VERSION:-26.3}"
APPS=/Applications

existing=$(ls -d "$APPS"/Xcode*.app 2>/dev/null | head -1 || true)
if [ -n "$existing" ]; then
  echo "Xcode already present: $existing"
else
  free_gb=$(df -g / | awk 'NR==2{print $4}')
  echo "free disk: ${free_gb} GB (Xcode + one iOS simulator runtime need ~30 GB)"
  if [ "$free_gb" -lt 30 ]; then
    echo "freeing Homebrew caches..."; brew cleanup --prune=all -s >/dev/null 2>&1 || true
    free_gb=$(df -g / | awk 'NR==2{print $4}'); echo "free disk now: ${free_gb} GB"
    [ "$free_gb" -lt 25 ] && echo "!! still tight; consider clearing ~/Library/Caches/Google or ~/Downloads first"
  fi
  xip=$(ls -t ~/Downloads/Xcode*"$WANT"*.xip ~/Downloads/Xcode*.xip 2>/dev/null | head -1 || true)
  if [ -n "$xip" ]; then
    echo "expanding $xip into $APPS (10-30 min on this machine)..."
    cd "$APPS"
    xip --expand "$xip"
    ver=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APPS/Xcode.app/Contents/Info.plist")
    mv "$APPS/Xcode.app" "$APPS/Xcode-$ver.app"
    existing="$APPS/Xcode-$ver.app"
    echo "installed $existing"
  elif command -v xcodes >/dev/null; then
    echo "downloading Xcode $WANT with xcodes (Apple ID login required)..."
    xcodes install "$WANT" --experimental-unxip --directory "$APPS"
    existing=$(ls -d "$APPS"/Xcode*"$WANT"*.app | head -1)
  else
    cat <<MSG
No Xcode xip found in ~/Downloads and 'xcodes' is not installed.
Either:
  1) download Xcode_${WANT}_Universal.xip from https://developer.apple.com/download/all/?q=Xcode%20${WANT}
     into ~/Downloads and re-run this script, or
  2) ~/.local/bin/xcodes install ${WANT} --experimental-unxip --directory /Applications   (interactive Apple ID login)
MSG
    exit 1
  fi
fi

DEV="$existing/Contents/Developer"
export DEVELOPER_DIR="$DEV"
echo "== (sudo) accept license, select Xcode, install first-launch components"
sudo xcode-select -s "$DEV"
sudo xcodebuild -license accept
sudo xcodebuild -runFirstLaunch

echo "== iOS simulator runtime (~8 GB download, no sudo)"
if ! xcrun simctl list runtimes | grep -q "iOS .* - com.apple.CoreSimulator.SimRuntime.iOS"; then
  xcodebuild -downloadPlatform iOS
fi
xcrun simctl list runtimes | grep iOS || true

echo "== idb companion (simulator UI automation)"
brew list --formula idb-companion >/dev/null 2>&1 || brew install facebook/fb/idb-companion || true

echo
echo "Done. Xcode: $existing"
echo "Next: cd $(dirname "$0") && ./bin/th doctor && ./bin/th build && ./bin/th install && ./bin/th test smoke"
