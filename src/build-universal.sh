#!/bin/bash
# Build a UNIVERSAL2 (x86_64 + arm64) ProjectionMirror.app.
#
# Proven working 2026-09-09 on Apple Silicon (macOS, arm64) with a python.org UNIVERSAL2
# Python 3.11. Produces a fat bundle that runs NATIVELY on Apple Silicon (the M1 MacBook) and
# on an Intel test Mac. Contrast build.sh, which produces an x86_64-only bundle on the test Mac.
#
# Why this is not just "pyinstaller --target-arch universal2": every native dependency must be
# universal2 too. pyobjc and pycryptodome ship universal2 wheels and mss is pure Python, but
# Pillow ships only per-arch wheels - so we fuse its arm64 + x86_64 wheels into a universal2
# wheel with delocate-merge before building. That was the whole blocker.
#
# Run on an Apple Silicon Mac (arm64). Requires the python.org universal2 Python 3.11 framework
# (lipo -archs shows "x86_64 arm64"). Adjust PYVER/PY if you use a different universal2 Python.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"      # expects the src/*.py beside this script
PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
PYTAG=311
PILLOW_VER="${PILLOW_VER:-12.3.0}"          # pin so the two per-arch wheels match
WORK="${WORK:-/tmp/f300u-universal}"
APPNAME=ProjectionMirror
BUNDLE_ID=org.projectionmirror.app

# 0. sanity: the interpreter must itself be universal2
lipo -archs "$(readlink -f "$PY")" | grep -q "x86_64 arm64" \
  || { echo "ERROR: $PY is not a universal2 Python (need x86_64 arm64)"; exit 1; }

# 1. clean universal2 venv
rm -rf "$WORK"; mkdir -p "$WORK/whl/fused"
"$PY" -m venv "$WORK/venv"
V="$WORK/venv/bin"
"$V/python" -m pip install -q --upgrade pip
"$V/pip" install -q --only-binary=:all: \
  pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz \
  pyobjc-framework-ScreenCaptureKit pyobjc-framework-CoreMedia pycryptodome mss
"$V/pip" install -q pyinstaller

# 2. Pillow: install the arm64 wheel, then lipo the x86_64 slices into its binaries in place.
#
# We do this by hand instead of with delocate-merge. delocate decides "is this file a Mach-O"
# by REGEX-MATCHING lipo's stderr (delocate/fuse.py _RE_LIPO_UNKNOWN_FILE_STDERR), and it only
# knows the old wording "fatal error: lipo: can't figure out the architecture type of: X".
# macOS 26's lipo instead says "warning: not a mach-o 'X'" + "no eligible inputs found", so
# delocate raises on the first differing non-binary file (the two wheels' dist-info RECORD) and
# the fuse dies. Upstream is unfixed: 0.13.0 is the latest release and the last fuse.py commit
# is a0eee2cf (Jan 2025). Verified 2026-09-11.
#
# The hand fuse is safe here because both wheels carry the SAME filenames - Pillow's bundled
# .dylibs are not arch-hash-suffixed - so every binary has a 1:1 counterpart.
( cd "$WORK/whl"
  "$V/pip" download -q --no-deps --only-binary=:all: --python-version "$PYTAG" \
      --platform macosx_11_0_arm64  "pillow==$PILLOW_VER"
  "$V/pip" download -q --no-deps --only-binary=:all: --python-version "$PYTAG" \
      --platform macosx_10_15_x86_64 "pillow==$PILLOW_VER" )
"$V/pip" install -q --force-reinstall --no-deps "$WORK"/whl/pillow-*macosx_11_0_arm64.whl
X86="$WORK/whl/x86"; rm -rf "$X86"; mkdir -p "$X86"
"$V/python" - "$WORK/whl" "$X86" <<'EOPY'
import glob, sys, zipfile
whl = glob.glob(sys.argv[1] + "/pillow-*macosx_10_*_x86_64.whl")[0]
zipfile.ZipFile(whl).extractall(sys.argv[2])
EOPY
SP="$("$V/python" -c 'import PIL, os; print(os.path.dirname(os.path.dirname(PIL.__file__)))')"
fused=0
while IFS= read -r bin; do
  rel="${bin#$SP/}"; other="$X86/$rel"
  [ -f "$other" ] || { echo "ERROR: no x86_64 counterpart for $rel"; exit 1; }
  lipo -create "$bin" "$other" -output "$bin.uni" || { echo "ERROR: lipo failed on $rel"; exit 1; }
  mv "$bin.uni" "$bin"
  codesign --force --sign - "$bin" >/dev/null 2>&1 || true   # lipo drops the signature
  fused=$((fused + 1))
done < <(find "$SP/PIL" \( -name "*.so" -o -name "*.dylib" \) )
echo "fused $fused Pillow binaries to universal2"
[ "$fused" -gt 0 ] || { echo "ERROR: no Pillow binaries found to fuse"; exit 1; }

# 3. verify EVERY runtime extension is universal2 before building (delocate test fixtures excluded)
bad=0
for so in $(find "$WORK/venv/lib" \( -name "*.so" -o -name "*.dylib" \)); do
  lipo -archs "$so" 2>/dev/null | grep -q "x86_64 arm64" || { echo "NOT universal2: $so"; bad=1; }
done
[ "$bad" = 0 ] || { echo "ERROR: a runtime dependency is single-arch; aborting"; exit 1; }

# 4. build the universal2 app
cd "$HERE"
rm -rf "$WORK/build" "$WORK/dist" "$WORK/$APPNAME.spec"
"$V/pyinstaller" --noconfirm --windowed --name "$APPNAME" \
  --workpath "$WORK/build" --distpath "$WORK/dist" --specpath "$WORK" \
  --osx-bundle-identifier "$BUNDLE_ID" --target-arch universal2 \
  --hidden-import Crypto --hidden-import mss --hidden-import PIL --hidden-import Quartz \
  pm_app.py

APP="$WORK/dist/$APPNAME.app"
echo "=== arch check ==="
lipo -archs "$APP/Contents/MacOS/$APPNAME"
echo "built: $APP"

# 5. stable-sign for TCC persistence (recreate the f300u-signing keychain on THIS build host
#    first - see build.sh). Skipped automatically if the keychain is absent.
KC=f300u-signing.keychain; KP=f300u
if security list-keychains | grep -q "$KC"; then
  security unlock-keychain -p "$KP" "$KC"
  HASH=$(security find-identity "$KC" | awk '/\)/{print $2; exit}')
  codesign --force --deep --keychain "$KC" --sign "$HASH" "$APP"
  codesign --verify --deep --verbose=1 "$APP" && echo SIGNED_OK
else
  echo "NOTE: $KC not found on this host - bundle is ad-hoc signed only."
  echo "      Recreate the keychain (see build.sh) so the Screen Recording grant persists."
fi
echo DONE
