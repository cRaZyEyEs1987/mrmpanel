#!/usr/bin/env bash
# Build tarball + publish to local mirror web root (this server only).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(grep -E '^MRMPANEL_VERSION=' "$ROOT/scripts/install-full.sh" | head -1 | cut -d= -f2 | tr -d '"' || true)"
VERSION="${MRMPANEL_VERSION:-${VERSION:-0.1.0}}"
DIST_WEB="${MRMPANEL_DIST_WEB:-/var/www/mrmpanel-dist}"
STAGE="$(mktemp -d /tmp/mrmpanel-publish-XXXXXX)"
NAME="mrmpanel-${VERSION}"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "[publish] version=${VERSION}"
mkdir -p "$STAGE/$NAME" "$STAGE/out"

rsync -a \
  --exclude '.git/' \
  --exclude 'panel/.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.dev-data/' \
  --exclude '.cursor/' \
  --exclude 'dist/' \
  "$ROOT/" "$STAGE/$NAME/"

[[ -f "$STAGE/$NAME/scripts/install-full.sh" ]] || { echo "missing install-full.sh" >&2; exit 1; }
chmod +x "$STAGE/$NAME/scripts/install-full.sh" "$STAGE/$NAME/install.sh" 2>/dev/null || true

tar -C "$STAGE" -czf "$STAGE/out/${NAME}.tar.gz" "$NAME"
(
  cd "$STAGE/out"
  sha256sum "${NAME}.tar.gz" > "${NAME}.sha256"
  cp -f "${NAME}.tar.gz" mrmpanel-latest.tar.gz
  cp -f "${NAME}.sha256" mrmpanel-latest.sha256
)

cp -f "$ROOT/install.sh" "$STAGE/out/install.sh"
chmod +x "$STAGE/out/install.sh"

cat > "$STAGE/out/index.html" <<EOF
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>mrmpanel downloads</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; }
    pre { background: #f4f4f4; padding: 1rem; overflow: auto; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>mrmpanel</h1>
  <p>Release <strong>${VERSION}</strong> — fresh Alma/Rocky/RHEL 9–10 or Ubuntu 24.04:</p>
  <pre>curl -fsSL https://mrmpanel.hostingandstuff.online/install.sh | sudo bash -s -- --all</pre>
  <ul>
    <li><a href="/install.sh">install.sh</a></li>
    <li><a href="/releases/mrmpanel-latest.tar.gz">mrmpanel-latest.tar.gz</a></li>
    <li><a href="/releases/mrmpanel-latest.sha256">checksum</a></li>
  </ul>
</body>
</html>
EOF

echo "[publish] → ${DIST_WEB}"
sudo mkdir -p "$DIST_WEB/releases"
sudo cp -f "$STAGE/out/install.sh" "$DIST_WEB/install.sh"
sudo cp -f "$STAGE/out/index.html" "$DIST_WEB/index.html"
sudo cp -f "$STAGE/out/${NAME}.tar.gz" "$DIST_WEB/releases/"
sudo cp -f "$STAGE/out/${NAME}.sha256" "$DIST_WEB/releases/"
sudo cp -f "$STAGE/out/mrmpanel-latest.tar.gz" "$DIST_WEB/releases/"
sudo cp -f "$STAGE/out/mrmpanel-latest.sha256" "$DIST_WEB/releases/"
sudo chmod 755 "$DIST_WEB/install.sh"
sudo chmod 644 "$DIST_WEB/index.html" "$DIST_WEB/releases/"*
# SELinux: allow nginx to read the mirror tree
if command -v chcon >/dev/null 2>&1; then
  sudo chcon -R -t httpd_sys_content_t "$DIST_WEB" 2>/dev/null || true
fi

mkdir -p "$ROOT/dist/releases"
cp -f "$STAGE/out/install.sh" "$STAGE/out/index.html" "$ROOT/dist/"
cp -f "$STAGE/out/${NAME}.tar.gz" "$STAGE/out/mrmpanel-latest.tar.gz" "$ROOT/dist/releases/"

echo "[publish] done"
sudo ls -lh "$DIST_WEB/install.sh" "$DIST_WEB/releases/mrmpanel-latest.tar.gz"
