#!/usr/bin/env bash
# Rebuild friday/sentinel/dashboard/tailwind.css with the pinned Tailwind standalone CLI.
# Run after changing any class in the dashboard's HTML/JS. Nothing here runs at deploy time.
set -euo pipefail

VERSION="v3.4.17"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DASH="$ROOT/friday/sentinel/dashboard"
CACHE="$ROOT/.cache"

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)               ASSET=tailwindcss-macos-arm64; SHA=a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3 ;;
  Darwin-x86_64)              ASSET=tailwindcss-macos-x64;   SHA=6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3 ;;
  Linux-x86_64)               ASSET=tailwindcss-linux-x64;   SHA=7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4 ;;
  Linux-aarch64|Linux-arm64)  ASSET=tailwindcss-linux-arm64; SHA=69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b ;;
  *) echo "build_css: unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

BIN="$CACHE/tailwindcss-$VERSION-$ASSET"
if [ ! -x "$BIN" ]; then
  mkdir -p "$CACHE"
  echo "build_css: downloading tailwindcss $VERSION ($ASSET)"
  curl -sSL -o "$BIN.tmp" "https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/$ASSET"
  if command -v sha256sum >/dev/null 2>&1; then
    echo "$SHA  $BIN.tmp" | sha256sum -c - >/dev/null
  else
    echo "$SHA  $BIN.tmp" | shasum -a 256 -c - >/dev/null
  fi
  chmod +x "$BIN.tmp" && mv "$BIN.tmp" "$BIN"
fi

cd "$DASH"
"$BIN" -c tailwind.config.js -i tailwind.src.css -o tailwind.css --minify
echo "build_css: wrote $DASH/tailwind.css ($(wc -c < tailwind.css | tr -d ' ') bytes)"
