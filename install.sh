#!/usr/bin/env bash
# mrmpanel bootstrap installer — single-file entrypoint
# Downloads the full package from the release mirror, then runs the real installer.
#
# Usage:
#   sudo bash -c "$(curl -fsSL https://mrmpanel.hostingandstuff.online/install.sh)" -- --all
#   curl -fsSL https://mrmpanel.hostingandstuff.online/install.sh | sudo bash -s -- --all
#   sudo bash install.sh --web --mariadb
#
set -euo pipefail

MRMPANEL_MIRROR="${MRMPANEL_MIRROR:-https://mrmpanel.hostingandstuff.online}"
MRMPANEL_VERSION="${MRMPANEL_VERSION:-latest}"
# Must not match mrmpanel-* — find uses that pattern for the extracted package dir
WORKDIR="${TMPDIR:-/tmp}/mrm-bootstrap-$$"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
log()  { echo -e "${GREEN}[mrmpanel]${NC} $*"; }
warn() { echo -e "${YELLOW}[mrmpanel]${NC} $*"; }
err()  { echo -e "${RED}[mrmpanel]${NC} $*" >&2; }
die()  { err "$*"; exit 1; }

cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

[[ $(id -u) -eq 0 ]] || die "Run as root: curl -fsSL ${MRMPANEL_MIRROR}/install.sh | sudo bash -s -- --all"

# Need curl or wget
if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL --connect-timeout 30 --retry 3 -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -q -O "$2" "$1"; }
else
  die "Need curl or wget to download the package"
fi

# Install missing extract tools via the distro package manager.
# </dev/null on pkg mgrs so they do not consume stdin when invoked via: curl | bash
ensure_extract_tools() {
  local need=()
  command -v tar >/dev/null 2>&1 || need+=(tar)
  command -v gzip >/dev/null 2>&1 || need+=(gzip)
  if [[ ${#need[@]} -eq 0 ]]; then
    return 0
  fi

  [[ -f /etc/os-release ]] || die "Cannot detect OS to install: ${need[*]}"
  # shellcheck source=/dev/null
  . /etc/os-release
  log "Installing missing tools: ${need[*]} (${ID:-unknown})"

  case "${ID:-}" in
    almalinux|rocky|rhel|centos|fedora)
      if command -v dnf >/dev/null 2>&1; then
        dnf -y install "${need[@]}" </dev/null || die "Failed to install ${need[*]} via dnf"
      elif command -v yum >/dev/null 2>&1; then
        yum -y install "${need[@]}" </dev/null || die "Failed to install ${need[*]} via yum"
      else
        die "Need dnf or yum to install: ${need[*]}"
      fi
      ;;
    ubuntu|debian)
      apt-get update -y </dev/null || die "apt-get update failed"
      DEBIAN_FRONTEND=noninteractive apt-get install -y "${need[@]}" </dev/null \
        || die "Failed to install ${need[*]} via apt"
      ;;
    *)
      die "Unsupported OS '${ID:-unknown}' — install manually: ${need[*]}"
      ;;
  esac

  hash -r 2>/dev/null || true
  command -v tar >/dev/null 2>&1 || die "tar still missing after package install"
}

# Ensure tar/gzip exist before download/extract (minimal Alma images often lack tar)
ensure_extract_tools

TARBALL_URL="${MRMPANEL_MIRROR}/releases/mrmpanel-${MRMPANEL_VERSION}.tar.gz"
CHECKSUM_URL="${MRMPANEL_MIRROR}/releases/mrmpanel-${MRMPANEL_VERSION}.sha256"
# Bust CDN/proxy caches so --force upgrades actually get the new package
CACHE_BUST="$(date +%s)"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

log "Downloading package from ${TARBALL_URL}"
fetch "${TARBALL_URL}?t=${CACHE_BUST}" mrmpanel.tar.gz \
  || die "Download failed. Is DNS for mrmpanel.hostingandstuff.online pointing here?"

if fetch "${CHECKSUM_URL}?t=${CACHE_BUST}" mrmpanel.sha256 2>/dev/null; then
  log "Verifying checksum…"
  if command -v sha256sum >/dev/null 2>&1; then
    echo "$(awk '{print $1}' mrmpanel.sha256)  mrmpanel.tar.gz" | sha256sum -c - \
      || die "Checksum mismatch — aborting"
  fi
else
  warn "No checksum file found; continuing without verification"
fi

# Show which stack image this package ships (helps debug stale WordPress versions)
if tar -tzf mrmpanel.tar.gz 2>/dev/null | grep -q 'stacks/wordpress.yml'; then
  WP_IMG="$(tar -xOf mrmpanel.tar.gz --wildcards '*/stacks/wordpress.yml' 2>/dev/null | grep -E '^image:' | head -1 || true)"
  [[ -n "$WP_IMG" ]] && log "Package WordPress stack: ${WP_IMG}"
fi

log "Extracting…"
tar -xzf mrmpanel.tar.gz
SRC="$(find "$WORKDIR" -mindepth 1 -maxdepth 1 -type d -name 'mrmpanel-*' | head -1)"
[[ -n "$SRC" && -f "$SRC/scripts/install-full.sh" ]] || die "Package layout unexpected (missing scripts/install-full.sh)"

log "Starting full installer…"
# Bash runs EXIT traps on exec — disarm so cleanup does not delete $SRC mid-install
trap - EXIT
# Reattach stdin to the terminal so password/hostname prompts work under: curl | bash.
# -r is not enough: /dev/tty exists but cannot be opened without a controlling
# terminal (e.g. ssh host 'sudo bash …'), which would abort the install.
if { : </dev/tty; } 2>/dev/null; then
  exec bash "$SRC/scripts/install-full.sh" "$@" </dev/tty
else
  exec bash "$SRC/scripts/install-full.sh" "$@"
fi
