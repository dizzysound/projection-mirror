#!/bin/bash
# Build + STABLE-SIGN the native app so the Screen Recording (TCC) grant survives rebuilds.
# The dedicated keychain + self-signed code-signing cert give a constant "designated
# requirement"; re-signing with it each build keeps macOS treating it as the same app.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"   # run from the script's own dir (any checkout)
KC=f300u-signing.keychain; KP=f300u
security unlock-keychain -p "$KP" "$KC"
HASH=$(security find-identity "$KC" | awk '/\)/{print $2; exit}')
[ -n "$HASH" ] || { echo "no signing identity in $KC"; exit 1; }
echo "signing identity: $HASH"
rm -rf build dist ProjectionMirror.spec
./.venv/bin/pyinstaller --noconfirm --windowed --name ProjectionMirror \
  --osx-bundle-identifier org.projectionmirror.app \
  --hidden-import Crypto --hidden-import mss --hidden-import PIL \
  --hidden-import Quartz \
  pm_app.py >/tmp/build.log 2>&1
echo "pyinstaller rc=$?"
codesign --force --deep --keychain "$KC" --sign "$HASH" dist/ProjectionMirror.app
codesign --verify --deep --verbose=1 dist/ProjectionMirror.app && echo SIGNED_OK
rm -rf ~/Desktop/ProjectionMirror.app
cp -R dist/ProjectionMirror.app ~/Desktop/
xattr -dr com.apple.quarantine ~/Desktop/ProjectionMirror.app 2>/dev/null || true
echo DEPLOYED
codesign -d -r- ~/Desktop/ProjectionMirror.app 2>&1 | tail -1
