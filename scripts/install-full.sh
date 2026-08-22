#!/usr/bin/env bash
# mrmpanel installer — fresh servers only (Alma/Rocky/RHEL 9–10, Ubuntu 24.04)
set -euo pipefail

MRMPANEL_VERSION="0.1.35"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Full installer lives in scripts/ — package root is one level up
if [[ -d "${SCRIPT_DIR}/../panel" && -d "${SCRIPT_DIR}/../compose" ]]; then
  PACKAGE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
elif [[ -d "${SCRIPT_DIR}/panel" && -d "${SCRIPT_DIR}/compose" ]]; then
  PACKAGE_ROOT="$SCRIPT_DIR"
else
  PACKAGE_ROOT="$SCRIPT_DIR"
fi
INSTALL_ROOT="/opt/mrmpanel"
DATA_ROOT="/var/lib/mrmpanel"
COMPOSE_DIR="${INSTALL_ROOT}/compose"
FEATURES_FILE="${DATA_ROOT}/features.json"
RESUME_CONF="${DATA_ROOT}/install-resume.conf"
RESUME_UNIT="mrmpanel-install-resume.service"
MRMPANEL_MIRROR="${MRMPANEL_MIRROR:-https://mrmpanel.hostingandstuff.online}"
RESUMING=0

# Defaults
FEATURE_WEB=1
FEATURE_MAIL=0
FEATURE_MARIADB=0
FEATURE_POSTGRES=0
FEATURE_DNS=1
NON_INTERACTIVE=0
FORCE_INSTALL=0
HOSTNAME_ARG=""
ADMIN_PASSWORD="${MRMPANEL_ADMIN_PASSWORD:-}"
ACME_EMAIL=""
OPENED_PORTS=""
OPERATOR_USER="${MRMPANEL_OPERATOR_USER:-${SUDO_USER:-}}"
NS_BASE_DOMAIN=""
NS1_HOSTNAME=""
NS2_HOSTNAME=""
NS1_IP=""
NS2_IP=""

RED='\033[0;31m';GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[mrmpanel]${NC} $*"; }
warn() { echo -e "${YELLOW}[mrmpanel]${NC} $*"; }
err()  { echo -e "${RED}[mrmpanel]${NC} $*" >&2; }
die()  { err "$*"; exit 1; }

# Always prompt on the real terminal (curl | bash leaves stdin at EOF)
prompt_read() {
  local prompt="$1"
  local __var="$2"
  local __silent="${3:-0}"
  local __val=""
  if [[ -r /dev/tty ]]; then
    if [[ "$__silent" == "1" ]]; then
      read -r -s -p "$prompt" __val </dev/tty || true
      echo >/dev/tty
    else
      read -r -p "$prompt" __val </dev/tty || true
    fi
  else
    if [[ "$__silent" == "1" ]]; then
      read -r -s -p "$prompt" __val || true
      echo
    else
      read -r -p "$prompt" __val || true
    fi
  fi
  printf -v "$__var" '%s' "$__val"
}

usage() {
  cat <<'EOF'
Usage: install.sh [options]

  --all                 Enable web, mail, mariadb, postgres, dns (still prompts hostname/password)
  --web / --no-web      Web hosting + Traefik (default: on)
  --mail / --no-mail    docker-mailserver (default: off)
  --mariadb / --no-mariadb
  --postgres / --no-postgres
  --dns / --no-dns      Authoritative DNS (PowerDNS; default: on)
  --hostname FQDN       Server hostname (skips PTR prompt)
  --email EMAIL         ACME / Let's Encrypt contact email
  --non-interactive     No prompts (needs --hostname and MRMPANEL_ADMIN_PASSWORD)
  --force               Continue even if Docker/ports already present (resume/repair)
  --resume              Internal: continue after automatic reboot
  -h, --help            Show help

Environment:
  MRMPANEL_ADMIN_PASSWORD   Panel admin password (required in non-interactive)
  MRMPANEL_MARIADB_ROOT_PASSWORD
  MRMPANEL_POSTGRES_PASSWORD
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all) FEATURE_WEB=1; FEATURE_MAIL=1; FEATURE_MARIADB=1; FEATURE_POSTGRES=1; FEATURE_DNS=1; shift ;;
      --web) FEATURE_WEB=1; shift ;;
      --no-web) FEATURE_WEB=0; shift ;;
      --mail) FEATURE_MAIL=1; shift ;;
      --no-mail) FEATURE_MAIL=0; shift ;;
      --mariadb) FEATURE_MARIADB=1; shift ;;
      --no-mariadb) FEATURE_MARIADB=0; shift ;;
      --postgres) FEATURE_POSTGRES=1; shift ;;
      --no-postgres) FEATURE_POSTGRES=0; shift ;;
      --dns) FEATURE_DNS=1; shift ;;
      --no-dns) FEATURE_DNS=0; shift ;;
      --hostname) HOSTNAME_ARG="${2:-}"; shift 2 ;;
      --email) ACME_EMAIL="${2:-}"; shift 2 ;;
      --non-interactive) NON_INTERACTIVE=1; shift ;;
      --force) FORCE_INSTALL=1; shift ;;
      --resume) load_resume || die "No resume state at ${RESUME_CONF}"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
  done
}

need_root() {
  [[ $(id -u) -eq 0 ]] || die "Run as root (sudo bash install.sh)"
}

detect_operator_user() {
  local existing=""
  if [[ -f "$FEATURES_FILE" ]]; then
    existing="$(python3 - "$FEATURES_FILE" <<'PY' 2>/dev/null || true
import json
import sys

print(json.load(open(sys.argv[1])).get("operator_user", ""))
PY
)"
  fi
  if [[ -z "$OPERATOR_USER" || "$OPERATOR_USER" == "root" ]]; then
    OPERATOR_USER="$existing"
  fi
  if [[ -n "$OPERATOR_USER" && "$OPERATOR_USER" != "root" ]] && id "$OPERATOR_USER" >/dev/null 2>&1; then
    log "Panel operator account: ${OPERATOR_USER} (full ACL access under /home)"
    return 0
  fi
  OPERATOR_USER=""
  log "Installer is running as root; no additional /home operator ACL is needed"
}

detect_os() {
  [[ -f /etc/os-release ]] || die "Cannot detect OS"
  # shellcheck source=/dev/null
  . /etc/os-release
  OS_ID="${ID:-}"
  OS_VER="${VERSION_ID:-}"
  OS_MAJOR="${OS_VER%%.*}"
  case "$OS_ID" in
    almalinux|rocky|rhel)
      [[ "$OS_MAJOR" == "9" || "$OS_MAJOR" == "10" ]] \
        || die "Supported RHEL-family: versions 9–10 only (got $OS_VER)"
      PKG_MGR="dnf"
      ;;
    ubuntu)
      [[ "$OS_VER" == "24.04" ]] || die "Supported Ubuntu: 24.04 only (got $OS_VER)"
      PKG_MGR="apt"
      ;;
    *) die "Unsupported OS: $OS_ID $OS_VER (need Alma/Rocky/RHEL 9–10 or Ubuntu 24.04)" ;;
  esac
  log "Detected $PRETTY_NAME"
}

pkg_installed() {
  if [[ "$PKG_MGR" == "dnf" ]]; then
    rpm -q "$1" &>/dev/null
  else
    dpkg -s "$1" &>/dev/null
  fi
}

service_active() {
  systemctl is-active --quiet "$1" 2>/dev/null
}

port_in_use() {
  local port="$1"
  if command -v ss &>/dev/null; then
    ss -tlnp | grep -qE ":${port}\\s" && return 0
  fi
  return 1
}

preflight() {
  log "Running preflight checks (fresh server required)…"
  local conflicts=()

  for svc in docker nginx httpd apache2 postfix dovecot mariadb mysqld mysql postgresql; do
    if service_active "$svc"; then
      conflicts+=("active service: $svc")
    fi
  done

  for pkg in docker-ce docker.io nginx httpd apache2 postfix dovecot-core dovecot mariadb-server mysql-server postgresql; do
    if pkg_installed "$pkg" 2>/dev/null; then
      conflicts+=("installed package: $pkg")
    fi
  done

  if command -v docker &>/dev/null; then
    conflicts+=("docker binary already present")
  fi

  local ports=(80 443)
  [[ "$FEATURE_MAIL" == "1" ]] && ports+=(25 465 587 993)
  [[ "$FEATURE_MARIADB" == "1" ]] && ports+=(3306)
  [[ "$FEATURE_POSTGRES" == "1" ]] && ports+=(5432)
  [[ "$FEATURE_DNS" == "1" ]] && ports+=(53)

  for p in "${ports[@]}"; do
    if port_in_use "$p"; then
      conflicts+=("port $p already in use")
    fi
  done

  if [[ ${#conflicts[@]} -gt 0 ]]; then
    if [[ "$FORCE_INSTALL" == "1" ]]; then
      warn "Preflight found existing software (--force): continuing anyway"
      for c in "${conflicts[@]}"; do
        warn "  - $c"
      done
      return 0
    fi
    err "Preflight failed — this installer is for fresh servers only:"
    for c in "${conflicts[@]}"; do
      err "  - $c"
    done
    err "Remove conflicting software, or re-run with --force to resume/repair."
    exit 1
  fi
  log "Preflight OK"
}

get_public_ip() {
  local ip=""
  for url in "https://ifconfig.me" "https://icanhazip.com" "https://api.ipify.org"; do
    ip="$(curl -4 -fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    [[ -n "$ip" ]] && break
  done
  echo "$ip"
}

get_local_ip() {
  hostname -I 2>/dev/null | awk '{print $1}'
}

# Fresh minimal images often lack dig/host; hostname_flow needs them for PTR.
ensure_dns_tools() {
  if command -v dig &>/dev/null || command -v host &>/dev/null; then
    return 0
  fi
  log "Installing DNS lookup tools for PTR check…"
  if [[ "$PKG_MGR" == "dnf" ]]; then
    dnf -y install bind-utils </dev/null
  else
    apt-get update -y </dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y dnsutils </dev/null
  fi
  hash -r 2>/dev/null || true
}

ptr_lookup() {
  local ip="$1"
  local ptr=""
  if command -v dig &>/dev/null; then
    ptr="$(dig +short -x "$ip" 2>/dev/null | sed 's/\.$//' | head -1)"
  elif command -v host &>/dev/null; then
    ptr="$(host "$ip" 2>/dev/null | awk '/domain name pointer/{print $NF}' | sed 's/\.$//' | head -1)"
  fi
  echo "$ptr"
}

interactive_features() {
  [[ "$NON_INTERACTIVE" == "1" ]] && return 0
  echo
  log "Select features (same options appear in the panel UI):"
  local ans
  prompt_read "  Enable web hosting? [Y/n] " ans
  [[ "${ans,,}" == "n" ]] && FEATURE_WEB=0 || FEATURE_WEB=1
  prompt_read "  Enable mail (docker-mailserver)? [y/N] " ans
  [[ "${ans,,}" == "y" ]] && FEATURE_MAIL=1 || FEATURE_MAIL=0
  prompt_read "  Enable MariaDB? [y/N] " ans
  [[ "${ans,,}" == "y" ]] && FEATURE_MARIADB=1 || FEATURE_MARIADB=0
  prompt_read "  Enable PostgreSQL? [y/N] " ans
  [[ "${ans,,}" == "y" ]] && FEATURE_POSTGRES=1 || FEATURE_POSTGRES=0
  prompt_read "  Enable authoritative DNS (ns1/ns2)? [Y/n] " ans
  [[ "${ans,,}" == "n" ]] && FEATURE_DNS=0 || FEATURE_DNS=1
}

# Multi-label public suffixes — never use these alone as NS_BASE_DOMAIN (e.g. co.za).
is_public_suffix() {
  local name="${1,,}"
  case "$name" in
    co.za|org.za|net.za|web.za|gov.za|ac.za|alt.za) return 0 ;;
    co.uk|org.uk|me.uk|net.uk) return 0 ;;
    com.au|net.au|org.au|co.nz|org.nz|net.nz) return 0 ;;
  esac
  # Single-label TLD (com, net, org, …)
  [[ "$name" != *.* ]]
}

parent_domain() {
  # Strip one left label, but never peel into a public suffix.
  # server.example.co.za → example.co.za; example.co.za → example.co.za
  local host="${1,,}"
  local parent
  [[ "$host" == *.* ]] || { echo "$host"; return 0; }
  parent="${host#*.}"
  if is_public_suffix "$parent"; then
    echo "$host"
  else
    echo "$parent"
  fi
}

derive_nameservers() {
  # server.example.com → ns1.example.com / ns2.example.com on public IP
  # Handles multi-part TLDs (co.za): server.example.co.za → ns1.example.co.za
  local host="${SERVER_HOSTNAME:-}"
  local pub
  pub="$(get_public_ip)"
  [[ -n "$host" ]] || return 0
  if [[ "$host" == *.* ]]; then
    NS_BASE_DOMAIN="$(parent_domain "$host")"
  else
    NS_BASE_DOMAIN="$host"
  fi
  if is_public_suffix "$NS_BASE_DOMAIN"; then
    NS_BASE_DOMAIN="$host"
  fi
  NS1_HOSTNAME="ns1.${NS_BASE_DOMAIN}"
  NS2_HOSTNAME="ns2.${NS_BASE_DOMAIN}"
  NS1_IP="${pub:-}"
  NS2_IP="${pub:-}"
  log "Nameservers: ${NS1_HOSTNAME} / ${NS2_HOSTNAME} → ${NS1_IP:-unknown}"
}

hostname_flow() {
  local public_ip local_ip ptr suggested chosen
  public_ip="$(get_public_ip)"
  local_ip="$(get_local_ip)"
  [[ -n "$public_ip" ]] || warn "Could not detect public IP; PTR suggestion may be limited."

  if [[ -n "$public_ip" && -n "$local_ip" && "$public_ip" != "$local_ip" ]]; then
    warn "NAT detected: local=$local_ip public=$public_ip — PTR should be set on the public IP."
  fi

  local existing=""
  if [[ -f "${DATA_ROOT}/features.json" ]]; then
    existing="$(sed -n 's/.*"hostname"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      "${DATA_ROOT}/features.json" | head -1)"
    [[ "$existing" == "localhost" ]] && existing=""
  fi

  if [[ -n "$HOSTNAME_ARG" ]]; then
    chosen="$HOSTNAME_ARG"
  elif [[ "$NON_INTERACTIVE" == "1" ]]; then
    # Upgrade of an existing install: keep the hostname already in use
    [[ -n "$existing" ]] || die "--hostname required in non-interactive mode"
    chosen="$existing"
    log "Keeping existing hostname: $chosen"
  else
    ptr=""
    [[ -n "$public_ip" ]] && ptr="$(ptr_lookup "$public_ip")"
    echo
    if [[ -n "$existing" ]]; then
      log "Current hostname: $existing"
      suggested="$existing"
    elif [[ -n "$ptr" ]]; then
      log "PTR for ${public_ip:-?} is: $ptr"
      suggested="$ptr"
      warn "If you do not want this hostname, change the PTR at your provider to match your chosen FQDN."
    else
      suggested="server1.example.com"
      warn "No PTR found for ${public_ip:-unknown}."
      warn "Recommend setting PTR to your chosen hostname, e.g. server1.yourdomain.com"
      warn "Example: PTR ${public_ip:-YOUR.PUBLIC.IP} → server1.yourdomain.com"
    fi
    prompt_read "  Hostname to use [${suggested}]: " chosen
    chosen="${chosen:-$suggested}"
  fi

  if [[ ! "$chosen" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]]; then
    die "Invalid hostname: $chosen"
  fi

  if [[ -n "$public_ip" ]]; then
    local current_ptr
    current_ptr="$(ptr_lookup "$public_ip")"
    if [[ -n "$current_ptr" && "$current_ptr" != "$chosen" ]]; then
      warn "PTR ($current_ptr) does not match chosen hostname ($chosen)."
      warn "Update PTR at your VPS provider for best mail deliverability."
    elif [[ -z "$current_ptr" ]]; then
      warn "No PTR set. Please set PTR to: $chosen"
    fi
  fi

  SERVER_HOSTNAME="$chosen"
  hostnamectl set-hostname "$SERVER_HOSTNAME" || true
  if ! grep -q "$SERVER_HOSTNAME" /etc/hosts 2>/dev/null; then
    echo "127.0.0.1 ${SERVER_HOSTNAME}" >> /etc/hosts
  fi
  log "Hostname set to $SERVER_HOSTNAME"
  derive_nameservers
}

install_deps() {
  log "Installing base packages…"
  if [[ "$PKG_MGR" == "dnf" ]]; then
    dnf -y install curl ca-certificates gnupg2 dnf-plugins-core bind-utils openssl tar gzip acl
  else
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates gnupg openssl dnsutils tar gzip acl
  fi
}

# EL10 minimal/cloud images omit iptables kernel modules Docker needs for bridge NAT.
# Prefer modules for the *running* kernel so Docker can start without a reboot.
# If only a newer kernel+modules are available, auto-reboot and resume install.
prepare_el10_docker_host() {
  [[ "$PKG_MGR" == "dnf" && "${OS_MAJOR:-}" == "10" ]] || return 0
  local running want
  running="$(uname -r)"
  want="kernel-modules-extra-${running}"
  log "Preparing EL10 host for Docker (modules for ${running})…"

  dnf -y install iptables iptables-nft >/dev/null || dnf -y install iptables iptables-nft

  if ! modprobe xt_addrtype 2>/dev/null; then
    # Install extras for the running kernel only — avoid pulling a newer kernel package set
    if dnf -y install "$want" 2>/dev/null; then
      log "Installed $want"
    else
      warn "Package $want not in repos; installing latest kernel + modules (reboot will finish install)"
      dnf -y install kernel kernel-core kernel-modules kernel-modules-extra iptables iptables-nft \
        || die "Failed to install kernel-modules-extra"
    fi
  fi

  local mod
  for mod in br_netfilter xt_addrtype iptable_nat iptable_filter; do
    modprobe "$mod" 2>/dev/null || true
  done

  mkdir -p /etc/modules-load.d
  cat > /etc/modules-load.d/mrmpanel-docker.conf <<'EOF'
br_netfilter
xt_addrtype
iptable_nat
iptable_filter
EOF

  if modprobe xt_addrtype 2>/dev/null; then
    log "Kernel modules ready for Docker"
    return 0
  fi

  log "Running kernel ${running} cannot load xt_addrtype — rebooting into updated kernel and resuming install automatically…"
  schedule_reboot_resume
}

write_resume_conf() {
  mkdir -p "$DATA_ROOT" "${DATA_ROOT}/secrets"
  local admin_pw
  admin_pw="${MRMPANEL_ADMIN_PASSWORD:-}"
  if [[ -z "$admin_pw" && -f "${DATA_ROOT}/secrets/admin_password" ]]; then
    admin_pw="$(tr -d '\n' < "${DATA_ROOT}/secrets/admin_password")"
  fi
  [[ -n "$admin_pw" ]] || admin_pw="$(openssl rand -base64 18 | tr -d '/+=' | head -c 16)"
  printf '%s' "$admin_pw" > "${DATA_ROOT}/secrets/admin_password"
  chmod 600 "${DATA_ROOT}/secrets/admin_password"
  ADMIN_PASSWORD="$admin_pw"
  export MRMPANEL_ADMIN_PASSWORD="$admin_pw"

  cat > "$RESUME_CONF" <<EOF
MRMPANEL_MIRROR=${MRMPANEL_MIRROR}
FEATURE_WEB=${FEATURE_WEB}
FEATURE_MAIL=${FEATURE_MAIL}
FEATURE_MARIADB=${FEATURE_MARIADB}
FEATURE_POSTGRES=${FEATURE_POSTGRES}
FEATURE_DNS=${FEATURE_DNS}
OPERATOR_USER=${OPERATOR_USER:-}
HOSTNAME_ARG=${SERVER_HOSTNAME:-${HOSTNAME_ARG}}
ACME_EMAIL=${ACME_EMAIL:-}
NS_BASE_DOMAIN=${NS_BASE_DOMAIN:-}
NS1_HOSTNAME=${NS1_HOSTNAME:-}
NS2_HOSTNAME=${NS2_HOSTNAME:-}
NS1_IP=${NS1_IP:-}
NS2_IP=${NS2_IP:-}
EOF
  chmod 600 "$RESUME_CONF"
}

schedule_reboot_resume() {
  write_resume_conf
  cat > "/etc/systemd/system/${RESUME_UNIT}" <<EOF
[Unit]
Description=Resume mrmpanel installation after reboot
After=network-online.target
Wants=network-online.target
ConditionPathExists=${RESUME_CONF}

[Service]
Type=oneshot
EnvironmentFile=${RESUME_CONF}
ExecStart=/usr/bin/bash -c 'export MRMPANEL_ADMIN_PASSWORD="\$(tr -d "\\n" < ${DATA_ROOT}/secrets/admin_password)"; curl -fsSL "\$MRMPANEL_MIRROR/install.sh" | bash -s -- --resume'
TimeoutStartSec=0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "${RESUME_UNIT}"
  log "Rebooting now — install continues automatically after boot (no action needed)."
  sleep 2
  systemctl reboot
  exit 0
}

clear_resume() {
  systemctl disable --now "${RESUME_UNIT}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${RESUME_UNIT}" "$RESUME_CONF"
  systemctl daemon-reload 2>/dev/null || true
}

load_resume() {
  [[ -f "$RESUME_CONF" ]] || return 1
  # shellcheck source=/dev/null
  set -a
  # shellcheck disable=SC1090
  source "$RESUME_CONF"
  set +a
  FEATURE_WEB="${FEATURE_WEB:-1}"
  FEATURE_MAIL="${FEATURE_MAIL:-0}"
  FEATURE_MARIADB="${FEATURE_MARIADB:-0}"
  FEATURE_POSTGRES="${FEATURE_POSTGRES:-0}"
  FEATURE_DNS="${FEATURE_DNS:-1}"
  OPERATOR_USER="${OPERATOR_USER:-}"
  HOSTNAME_ARG="${HOSTNAME_ARG:-}"
  ACME_EMAIL="${ACME_EMAIL:-}"
  NS_BASE_DOMAIN="${NS_BASE_DOMAIN:-}"
  NS1_HOSTNAME="${NS1_HOSTNAME:-}"
  NS2_HOSTNAME="${NS2_HOSTNAME:-}"
  NS1_IP="${NS1_IP:-}"
  NS2_IP="${NS2_IP:-}"
  if [[ -f "${DATA_ROOT}/secrets/admin_password" ]]; then
    ADMIN_PASSWORD="$(tr -d '\n' < "${DATA_ROOT}/secrets/admin_password")"
    export MRMPANEL_ADMIN_PASSWORD="$ADMIN_PASSWORD"
  fi
  if [[ -f "${DATA_ROOT}/secrets/mariadb_root_password" ]]; then
    export MRMPANEL_MARIADB_ROOT_PASSWORD="$(tr -d '\n' < "${DATA_ROOT}/secrets/mariadb_root_password")"
  fi
  if [[ -f "${DATA_ROOT}/secrets/postgres_password" ]]; then
    export MRMPANEL_POSTGRES_PASSWORD="$(tr -d '\n' < "${DATA_ROOT}/secrets/postgres_password")"
  fi
  if [[ -f "${DATA_ROOT}/secrets/pdns_api_key" ]]; then
    export MRMPANEL_PDNS_API_KEY="$(tr -d '\n' < "${DATA_ROOT}/secrets/pdns_api_key")"
  fi
  NON_INTERACTIVE=1
  FORCE_INSTALL=1
  RESUMING=1
  log "Resuming install after reboot…"
  return 0
}

start_docker() {
  systemctl enable docker >/dev/null
  if systemctl restart docker; then
    return 0
  fi
  err "Docker failed to start. Last logs:"
  journalctl -u docker.service -n 40 --no-pager >&2 || true
  if [[ "${OS_MAJOR:-}" == "10" ]]; then
    err "On Alma/Rocky/RHEL 10 this is often missing kernel-modules-extra / xt_addrtype."
    err "Try: sudo dnf -y install kernel-modules-extra && sudo modprobe xt_addrtype && sudo systemctl restart docker"
    err "If modprobe fails, reboot after: sudo dnf -y update"
  fi
  die "Docker service failed to start"
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && systemctl is-active --quiet docker 2>/dev/null; then
    log "Docker already running — skipping install"
    prepare_el10_docker_host
    ensure_docker_api_compat
    return 0
  fi
  log "Installing Docker Engine…"
  prepare_el10_docker_host
  if [[ "$PKG_MGR" == "dnf" ]]; then
    dnf -y install dnf-plugins-core
    # EL10: prefer Docker's rhel repo; EL9: centos repo (with rhel fallback)
    if [[ "${OS_MAJOR:-}" == "10" ]]; then
      dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo || \
        dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    else
      dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo || \
        dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
    fi
    dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  ensure_docker_api_compat
  start_docker
  log "Docker installed"
}

# Docker Engine 29+ defaults min API to 1.40/1.44; Traefik's Docker provider still
# speaks 1.24 until newer Traefik releases. Allow older clients so Traefik can discover sites.
ensure_docker_api_compat() {
  mkdir -p /etc/docker
  local cfg=/etc/docker/daemon.json
  if [[ -f "$cfg" ]] && grep -q 'min-api-version' "$cfg" 2>/dev/null; then
    return 0
  fi
  if [[ -f "$cfg" ]]; then
    # Merge conservatively with python if present
    if command -v python3 >/dev/null 2>&1; then
      python3 - <<'PY'
import json
from pathlib import Path
p = Path("/etc/docker/daemon.json")
data = json.loads(p.read_text() or "{}")
if not isinstance(data, dict):
    data = {}
data["min-api-version"] = "1.24"
p.write_text(json.dumps(data, indent=2) + "\n")
PY
    else
      warn "Set min-api-version in $cfg manually if Traefik cannot see containers"
      return 0
    fi
  else
    cat > "$cfg" <<'EOF'
{
  "min-api-version": "1.24"
}
EOF
  fi
  log "Configured Docker min-api-version=1.24 (Traefik compatibility)"
  systemctl restart docker 2>/dev/null || true
  # wait briefly for docker to come back
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    docker info >/dev/null 2>&1 && break
    sleep 1
  done
}

copy_tree() {
  log "Installing mrmpanel files to ${INSTALL_ROOT}…"
  [[ -d "$PACKAGE_ROOT" && -f "${PACKAGE_ROOT}/scripts/install-full.sh" ]] \
    || die "Package source missing at ${PACKAGE_ROOT} (bootstrap deleted it?). Re-download install.sh and retry."
  mkdir -p "$INSTALL_ROOT" "$DATA_ROOT"/{secrets,sites,mail/{data,state,logs,config},roundcube/mysql,webmail-sso,mariadb,postgres,dismissed}
  if command -v rsync &>/dev/null; then
    rsync -a --delete \
      --exclude '.git' \
      --exclude 'panel/.venv' \
      --exclude '__pycache__' \
      --exclude 'dist' \
      "${PACKAGE_ROOT}/" "${INSTALL_ROOT}/" \
      || die "rsync to ${INSTALL_ROOT} failed"
  else
    cp -a "${PACKAGE_ROOT}/." "${INSTALL_ROOT}/" || die "cp to ${INSTALL_ROOT} failed"
  fi
  [[ -f "${INSTALL_ROOT}/panel/requirements.txt" ]] \
    || die "Package copy failed — missing panel/requirements.txt"
  log "Files installed to ${INSTALL_ROOT}"
}

write_features() {
  mkdir -p "$DATA_ROOT"
  local pub http_public="true"
  pub="$(get_public_ip)"
  [[ -n "$NS_BASE_DOMAIN" ]] || derive_nameservers
  [[ -n "$NS1_IP" ]] || NS1_IP="$pub"
  [[ -n "$NS2_IP" ]] || NS2_IP="$pub"
  if [[ -f "$FEATURES_FILE" ]]; then
    http_public="$(python3 -c "import json; d=json.load(open('${FEATURES_FILE}')); print('true' if d.get('panel_http_public', True) else 'false')" 2>/dev/null || echo true)"
  fi
  PANEL_HTTP_PUBLIC="$http_public"
  cat > "$FEATURES_FILE" <<EOF
{
  "version": "${MRMPANEL_VERSION}",
  "hostname": "${SERVER_HOSTNAME}",
  "operator_user": "${OPERATOR_USER}",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "web": $([[ "$FEATURE_WEB" == "1" ]] && echo true || echo false),
  "mail": $([[ "$FEATURE_MAIL" == "1" ]] && echo true || echo false),
  "mariadb": $([[ "$FEATURE_MARIADB" == "1" ]] && echo true || echo false),
  "postgres": $([[ "$FEATURE_POSTGRES" == "1" ]] && echo true || echo false),
  "dns": $([[ "$FEATURE_DNS" == "1" ]] && echo true || echo false),
  "public_ip": "${pub}",
  "acme_email": "${ACME_EMAIL}",
  "ns_base_domain": "${NS_BASE_DOMAIN}",
  "ns1_hostname": "${NS1_HOSTNAME}",
  "ns2_hostname": "${NS2_HOSTNAME}",
  "ns1_ip": "${NS1_IP}",
  "ns2_ip": "${NS2_IP}",
  "panel_http_public": ${http_public}
}
EOF
  chmod 644 "$FEATURES_FILE"
  log "Wrote $FEATURES_FILE"
}

gen_secrets() {
  local mariadb_pw postgres_pw roundcube_pw roundcube_des admin_pw
  mkdir -p "${DATA_ROOT}/secrets"

  # Resume / upgrade: keep the existing admin password unless env overrides it
  if [[ -f "${DATA_ROOT}/secrets/admin_password" && -z "${ADMIN_PASSWORD}" ]]; then
    ADMIN_PASSWORD="$(tr -d '\n' < "${DATA_ROOT}/secrets/admin_password")"
    [[ -n "$ADMIN_PASSWORD" ]] && log "Keeping existing admin password"
  fi
  if [[ -f "${DATA_ROOT}/secrets/mariadb_root_password" && -z "${MRMPANEL_MARIADB_ROOT_PASSWORD:-}" ]]; then
    MRMPANEL_MARIADB_ROOT_PASSWORD="$(tr -d '\n' < "${DATA_ROOT}/secrets/mariadb_root_password")"
  fi
  if [[ -f "${DATA_ROOT}/secrets/postgres_password" && -z "${MRMPANEL_POSTGRES_PASSWORD:-}" ]]; then
    MRMPANEL_POSTGRES_PASSWORD="$(tr -d '\n' < "${DATA_ROOT}/secrets/postgres_password")"
  fi
  if [[ -f "${DATA_ROOT}/secrets/roundcube_des_key" && -z "${MRMPANEL_ROUNDCUBE_DES_KEY:-}" ]]; then
    MRMPANEL_ROUNDCUBE_DES_KEY="$(tr -d '\n' < "${DATA_ROOT}/secrets/roundcube_des_key")"
  fi
  if [[ -f "${DATA_ROOT}/secrets/roundcube_db_password" && -z "${MRMPANEL_ROUNDCUBE_DB_PASSWORD:-}" ]]; then
    MRMPANEL_ROUNDCUBE_DB_PASSWORD="$(tr -d '\n' < "${DATA_ROOT}/secrets/roundcube_db_password")"
  fi

  local pdns_key
  mariadb_pw="${MRMPANEL_MARIADB_ROOT_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"
  postgres_pw="${MRMPANEL_POSTGRES_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"
  roundcube_pw="${MRMPANEL_ROUNDCUBE_DB_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"
  # Roundcube needs exactly 24 characters for its session cipher key.
  roundcube_des="${MRMPANEL_ROUNDCUBE_DES_KEY:-$(openssl rand -base64 32 | tr -d '/+=' | head -c 24)}"
  if [[ -f "${DATA_ROOT}/secrets/pdns_api_key" && -z "${MRMPANEL_PDNS_API_KEY:-}" ]]; then
    pdns_key="$(tr -d '\n' < "${DATA_ROOT}/secrets/pdns_api_key")"
  else
    pdns_key="${MRMPANEL_PDNS_API_KEY:-$(openssl rand -hex 24)}"
  fi
  if [[ -n "$ADMIN_PASSWORD" ]]; then
    admin_pw="$ADMIN_PASSWORD"
  elif [[ "$NON_INTERACTIVE" == "1" ]]; then
    die "Set MRMPANEL_ADMIN_PASSWORD for non-interactive install"
  else
    log "Set the panel admin password (input hidden):"
    prompt_read "  Admin password: " admin_pw 1
    if [[ -z "$admin_pw" ]]; then
      admin_pw="$(openssl rand -base64 18 | tr -d '/+=' | head -c 16)"
      warn "No password entered — generated one (shown at end of install)"
    fi
  fi
  if [[ -z "$ACME_EMAIL" ]]; then
    if [[ "$NON_INTERACTIVE" == "1" || "$RESUMING" == "1" ]]; then
      ACME_EMAIL="admin@${SERVER_HOSTNAME}"
    else
      prompt_read "  Let's Encrypt email [admin@${SERVER_HOSTNAME}]: " ACME_EMAIL
      ACME_EMAIL="${ACME_EMAIL:-admin@${SERVER_HOSTNAME}}"
    fi
  fi
  printf '%s' "$mariadb_pw" > "${DATA_ROOT}/secrets/mariadb_root_password"
  printf '%s' "$postgres_pw" > "${DATA_ROOT}/secrets/postgres_password"
  printf '%s' "$roundcube_pw" > "${DATA_ROOT}/secrets/roundcube_db_password"
  printf '%s' "$roundcube_des" > "${DATA_ROOT}/secrets/roundcube_des_key"
  printf '%s' "$admin_pw" > "${DATA_ROOT}/secrets/admin_password"
  printf '%s' "$pdns_key" > "${DATA_ROOT}/secrets/pdns_api_key"
  # Placeholder so Roundcube can bind-mount this path before first SSO use.
  if [[ ! -f "${DATA_ROOT}/secrets/webmail_master_password" ]]; then
    openssl rand -base64 24 | tr -d '/+=' | head -c 32 > "${DATA_ROOT}/secrets/webmail_master_password"
  fi
  mkdir -p "${DATA_ROOT}/webmail-sso"
  chmod 775 "${DATA_ROOT}/webmail-sso" 2>/dev/null || true
  chmod 600 "${DATA_ROOT}/secrets/"*
  # Roundcube (uid 33) must read the master IMAP password.
  chown root:33 "${DATA_ROOT}/secrets/webmail_master_password" 2>/dev/null || chmod 644 "${DATA_ROOT}/secrets/webmail_master_password"
  chmod 640 "${DATA_ROOT}/secrets/webmail_master_password" 2>/dev/null || true
  chown root:33 "${DATA_ROOT}/webmail-sso" 2>/dev/null || true
  export MRMPANEL_MARIADB_ROOT_PASSWORD="$mariadb_pw"
  export MRMPANEL_POSTGRES_PASSWORD="$postgres_pw"
  export MRMPANEL_ROUNDCUBE_DB_PASSWORD="$roundcube_pw"
  export MRMPANEL_ROUNDCUBE_DES_KEY="$roundcube_des"
  export MRMPANEL_ADMIN_PASSWORD="$admin_pw"
  export MRMPANEL_PDNS_API_KEY="$pdns_key"
  ADMIN_PASSWORD="$admin_pw"
  export MRMPANEL_HOSTNAME="$SERVER_HOSTNAME"
  export MRMPANEL_MAIL_DOMAIN="${SERVER_HOSTNAME#*.}"
  [[ "$MRMPANEL_MAIL_DOMAIN" == "$SERVER_HOSTNAME" ]] && MRMPANEL_MAIL_DOMAIN="$SERVER_HOSTNAME"
  log "Secrets written"
}

patch_traefik_email() {
  local cfg="${COMPOSE_DIR}/traefik/traefik.yml"
  if [[ -f "$cfg" ]]; then
    sed -i "s/admin@localhost/${ACME_EMAIL}/g" "$cfg" || true
  fi
}

# Serve the panel at https://<hostname> with a real Let's Encrypt certificate.
# The panel itself stays plain HTTP on :8080; Traefik terminates TLS in front of it.
write_traefik_panel_route() {
  local dir="${COMPOSE_DIR}/traefik/dynamic"
  local out="${dir}/panel.yml"
  mkdir -p "$dir"
  rm -f "$out"
  # Needs a public FQDN — ACME cannot issue for "localhost" or a bare label.
  if [[ "$FEATURE_WEB" != "1" || "$SERVER_HOSTNAME" != *.* || "$SERVER_HOSTNAME" == localhost* ]]; then
    return 0
  fi
  cat >"$out" <<EOF
# Generated by install-full.sh — panel HTTPS front end for ${SERVER_HOSTNAME}.
http:
  routers:
    mrmpanel-panel-secure:
      rule: "Host(\`${SERVER_HOSTNAME}\`)"
      entryPoints: [websecure]
      service: mrmpanel-panel
      tls:
        certResolver: letsencrypt
    mrmpanel-panel-web:
      rule: "Host(\`${SERVER_HOSTNAME}\`)"
      entryPoints: [web]
      service: mrmpanel-panel
      middlewares: [mrmpanel-panel-https]

  middlewares:
    mrmpanel-panel-https:
      redirectScheme:
        scheme: https
        permanent: true

  services:
    mrmpanel-panel:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:8080"
EOF
  log "Panel HTTPS route written for https://${SERVER_HOSTNAME}"
}

compose_profiles() {
  local profiles=()
  [[ "$FEATURE_WEB" == "1" ]] && profiles+=("web")
  [[ "$FEATURE_MAIL" == "1" ]] && profiles+=("mail")
  [[ "$FEATURE_WEB" == "1" && "$FEATURE_MAIL" == "1" ]] && profiles+=("webmail")
  [[ "$FEATURE_MARIADB" == "1" ]] && profiles+=("mariadb")
  [[ "$FEATURE_POSTGRES" == "1" ]] && profiles+=("postgres")
  [[ "$FEATURE_DNS" == "1" ]] && profiles+=("dns")
  if [[ ${#profiles[@]} -eq 0 ]]; then
    die "Select at least one feature"
  fi
  local joined
  joined="$(IFS=,; echo "${profiles[*]}")"
  echo "$joined"
}

bootstrap_admin() {
  python3 - <<'PY'
import json, hashlib, secrets, time
from pathlib import Path
data = Path("/var/lib/mrmpanel")
data.mkdir(parents=True, exist_ok=True)
pw = Path("/var/lib/mrmpanel/secrets/admin_password").read_text().strip()
salt = secrets.token_hex(8)
digest = hashlib.sha256((salt + pw).encode()).hexdigest()
admin = {
    "username": "admin",
    "salt": salt,
    "password_sha256": digest,
    "role": "admin",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
path = data / "admin.json"
path.write_text(json.dumps(admin, indent=2))
path.chmod(0o600)
print("admin bootstrap written")
PY
}

install_panel_venv() {
  log "Installing panel Python environment…"
  if [[ "$PKG_MGR" == "dnf" ]]; then
    dnf -y install python3 python3-pip python3-devel gcc 2>/dev/null || dnf -y install python3
  else
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-venv
  fi
  python3 -m venv "${INSTALL_ROOT}/panel/.venv"
  "${INSTALL_ROOT}/panel/.venv/bin/pip" install --upgrade pip
  "${INSTALL_ROOT}/panel/.venv/bin/pip" install -r "${INSTALL_ROOT}/panel/requirements.txt"
}

install_jail_helpers() {
  mkdir -p /etc/mrmpanel /usr/local/bin
  install -m 0755 "${INSTALL_ROOT}/scripts/mrmpanel-jail-shell" /usr/local/bin/mrmpanel-jail-shell
  install -m 0644 "${INSTALL_ROOT}/scripts/bashrc-jail" /etc/mrmpanel/bashrc-jail
  install -m 0755 "${INSTALL_ROOT}/scripts/mrmpanel-backup.sh" /usr/sbin/mrmpanel-backup
  install -m 0755 "${INSTALL_ROOT}/scripts/mrmpanel-restore.sh" /usr/sbin/mrmpanel-restore
  # Compatibility for hosts that already look in /usr/local/bin
  ln -sfn /usr/sbin/mrmpanel-backup /usr/local/bin/mrmpanel-backup
  ln -sfn /usr/sbin/mrmpanel-restore /usr/local/bin/mrmpanel-restore
  if ! grep -q '^/usr/local/bin/mrmpanel-jail-shell$' /etc/shells 2>/dev/null; then
    echo '/usr/local/bin/mrmpanel-jail-shell' >> /etc/shells
  fi
}

# Collect ports as port/proto (DNS needs UDP+TCP 53)
required_ports() {
  # Skip public 8080 when admin disabled panel HTTP (Traefik HTTPS still works)
  if [[ "${PANEL_HTTP_PUBLIC:-true}" != "false" ]]; then
    echo "8080/tcp"
  fi
  if [[ "$FEATURE_WEB" == "1" ]]; then
    echo "80/tcp"
    echo "443/tcp"
  fi
  if [[ "$FEATURE_MAIL" == "1" ]]; then
    # Matches compose: SMTP, SMTPS, submission, IMAPS (no POP3/IMAP plain)
    echo "25/tcp"
    echo "465/tcp"
    echo "587/tcp"
    echo "993/tcp"
  fi
  if [[ "$FEATURE_MARIADB" == "1" ]]; then
    echo "3306/tcp"
  fi
  if [[ "$FEATURE_POSTGRES" == "1" ]]; then
    echo "5432/tcp"
  fi
  if [[ "$FEATURE_DNS" == "1" ]]; then
    echo "53/tcp"
    echo "53/udp"
  fi
}

open_firewall_ports() {
  local -a ports=()
  local spec p proto
  mapfile -t ports < <(required_ports)
  OPENED_PORTS="${ports[*]}"
  log "Opening firewall ports for selected features: ${OPENED_PORTS}"

  if command -v firewall-cmd >/dev/null 2>&1; then
    if ! systemctl is-active --quiet firewalld 2>/dev/null; then
      systemctl enable --now firewalld >/dev/null 2>&1 || true
    fi
    if systemctl is-active --quiet firewalld 2>/dev/null; then
      for spec in "${ports[@]}"; do
        p="${spec%/*}"
        proto="${spec#*/}"
        firewall-cmd --permanent --add-port="${p}/${proto}" >/dev/null || \
          warn "firewalld: could not add port ${p}/${proto}"
      done
      firewall-cmd --reload >/dev/null || warn "firewalld reload failed"
      log "firewalld updated"
      return 0
    fi
  fi

  if command -v ufw >/dev/null 2>&1; then
    for spec in "${ports[@]}"; do
      p="${spec%/*}"
      proto="${spec#*/}"
      ufw allow "${p}/${proto}" >/dev/null || warn "ufw: could not allow ${p}/${proto}"
    done
    if ! ufw status 2>/dev/null | grep -qi 'Status: active'; then
      # Enable only if inactive; do not disrupt existing SSH (ufw keeps OpenSSH if already allowed)
      ufw --force enable >/dev/null 2>&1 || warn "ufw enable failed (ports may still be listed)"
    fi
    log "ufw updated"
    return 0
  fi

  warn "No firewalld/ufw detected — ensure cloud security groups allow: ${OPENED_PORTS}"
}

install_systemd_unit() {
  install -m 0644 "${INSTALL_ROOT}/panel/mrmpanel.service" /etc/systemd/system/mrmpanel.service
  mkdir -p /etc/mrmpanel
  # Keep the existing key on upgrades so logged-in sessions survive
  if ! grep -q '^MRMPANEL_SECRET=' /etc/mrmpanel/panel.env 2>/dev/null; then
    echo "MRMPANEL_SECRET=$(openssl rand -hex 32)" > /etc/mrmpanel/panel.env
  fi
  chmod 600 /etc/mrmpanel/panel.env
  systemctl daemon-reload
  systemctl enable mrmpanel.service >/dev/null 2>&1 || true
  # restart, not "enable --now": an upgrade must pick up the new panel code
  systemctl restart mrmpanel.service
  log "Panel service started (http://0.0.0.0:8080)"
}

bootstrap_dns_zone() {
  [[ "$FEATURE_DNS" == "1" ]] || return 0
  log "Waiting for PowerDNS API…"
  local i key
  key="${MRMPANEL_PDNS_API_KEY:-}"
  [[ -n "$key" ]] || key="$(tr -d '\n' < "${DATA_ROOT}/secrets/pdns_api_key" 2>/dev/null || true)"
  [[ -n "$key" ]] || { warn "No PowerDNS API key — skip base zone"; return 0; }
  for i in $(seq 1 30); do
    if curl -fsS -H "X-API-Key: ${key}" "http://127.0.0.1:8081/api/v1/servers/localhost" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  if ! curl -fsS -H "X-API-Key: ${key}" "http://127.0.0.1:8081/api/v1/servers/localhost" >/dev/null 2>&1; then
    warn "PowerDNS API not ready — create base zone from panel Settings later"
    return 0
  fi
  # Use panel helper (same code path as Settings / site deploy)
  if [[ -x "${INSTALL_ROOT}/panel/.venv/bin/python" ]]; then
    MRMPANEL_DATA="$DATA_ROOT" MRMPANEL_FEATURES="$FEATURES_FILE" \
      "${INSTALL_ROOT}/panel/.venv/bin/python" - <<'PY' || warn "Base DNS zone creation failed"
from app.services import dns
dns.ensure_server_zone()
print("server DNS zone ready")
PY
  else
    warn "Panel venv missing — skip base DNS zone"
  fi
}

prepare_mail_tls() {
  [[ "$FEATURE_MAIL" == "1" ]] || return 0
  local dir="${DATA_ROOT}/mail/config/ssl"
  mkdir -p "$dir"
  if [[ -s "${dir}/fullchain.pem" && -s "${dir}/privkey.pem" ]]; then
    return 0
  fi
  # Temporary fallback so docker-mailserver can start before ACME is ready.
  # sync_mail_tls replaces this with Traefik's trusted certificate.
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
    -subj "/CN=${SERVER_HOSTNAME}" \
    -keyout "${dir}/privkey.pem" \
    -out "${dir}/fullchain.pem" >/dev/null 2>&1
  chmod 600 "${dir}/privkey.pem"
  chmod 644 "${dir}/fullchain.pem"
  log "Prepared temporary mail TLS certificate"
}

sync_mail_tls() {
  [[ "$FEATURE_MAIL" == "1" && "$FEATURE_WEB" == "1" ]] || return 0
  local volume acme out i
  volume="$(docker volume inspect mrmpanel_traefik_letsencrypt \
    --format '{{.Mountpoint}}' 2>/dev/null || true)"
  [[ -n "$volume" ]] || { warn "Traefik certificate volume not found; mail uses temporary TLS"; return 0; }
  acme="${volume}/acme.json"
  out="${DATA_ROOT}/mail/config/ssl"
  for i in $(seq 1 45); do
    if python3 "${INSTALL_ROOT}/scripts/sync-mail-tls.py" \
      "$acme" "$SERVER_HOSTNAME" "$out" >/dev/null 2>&1; then
      log "Trusted mail TLS certificate synced from Traefik"
      (
        cd "$COMPOSE_DIR"
        # docker-mailserver imports manual certs during setup. A plain restart
        # skips setup and would keep the temporary self-signed certificate.
        docker compose --profile mail up -d --force-recreate mail >/dev/null
      ) || warn "Could not recreate mail after TLS sync"
      return 0
    fi
    sleep 2
  done
  warn "Traefik certificate not ready; mail uses temporary TLS until next update"
}

bootstrap_mail_account() {
  [[ "$FEATURE_MAIL" == "1" ]] || return 0
  local accounts="${DATA_ROOT}/mail/config/postfix-accounts.cf"
  local secret="${DATA_ROOT}/secrets/postmaster_password"
  local address="postmaster@${MRMPANEL_MAIL_DOMAIN}"
  local password i

  # docker-mailserver v14 intentionally shuts down when there are no accounts.
  # Keep SMTP alive on a fresh install with the RFC-required postmaster address.
  if [[ -s "$accounts" ]] && grep -q '^[^#[:space:]]' "$accounts"; then
    log "Mail account configuration already exists"
    return 0
  fi

  if [[ -s "$secret" ]]; then
    password="$(tr -d '\n' < "$secret")"
  else
    password="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
    printf '%s' "$password" > "$secret"
    chmod 600 "$secret"
  fi

  log "Creating required mail account ${address}…"
  for i in $(seq 1 30); do
    if docker exec mrmpanel-mail setup email add "$address" "$password" >/dev/null 2>&1; then
      log "Postmaster mailbox ready (password: ${secret})"
      return 0
    fi
    sleep 2
  done
  warn "Could not create ${address}; mail will not accept SMTP until a mailbox is added"
}

sync_webmail_routes() {
  [[ "$FEATURE_MAIL" == "1" && "$FEATURE_WEB" == "1" ]] || return 0
  (
    cd "${INSTALL_ROOT}/panel"
    export PYTHONPATH="${INSTALL_ROOT}/panel${PYTHONPATH:+:$PYTHONPATH}"
    MRMPANEL_DATA="$DATA_ROOT" MRMPANEL_FEATURES="$FEATURES_FILE" \
      "${INSTALL_ROOT}/panel/.venv/bin/python" - <<'PY'
from app.services import webmail

result = webmail.sync_routes()
print(f"webmail routes ready for {len(result['hostnames'])} hostname(s)")
PY
  ) || warn "Could not generate webmail routes"
}

bootstrap_webmail_sso() {
  [[ "$FEATURE_MAIL" == "1" && "$FEATURE_WEB" == "1" ]] || return 0
  mkdir -p "${DATA_ROOT}/webmail-sso" "${DATA_ROOT}/secrets"
  if [[ ! -f "${DATA_ROOT}/secrets/webmail_master_password" ]]; then
    openssl rand -base64 24 | tr -d '/+=' | head -c 32 > "${DATA_ROOT}/secrets/webmail_master_password"
  fi
  chown root:33 "${DATA_ROOT}/secrets/webmail_master_password" 2>/dev/null || chmod 644 "${DATA_ROOT}/secrets/webmail_master_password"
  chmod 640 "${DATA_ROOT}/secrets/webmail_master_password" 2>/dev/null || true
  chown root:33 "${DATA_ROOT}/webmail-sso" 2>/dev/null || true
  chmod 775 "${DATA_ROOT}/webmail-sso" 2>/dev/null || true
  (
    cd "${INSTALL_ROOT}/panel"
    export PYTHONPATH="${INSTALL_ROOT}/panel${PYTHONPATH:+:$PYTHONPATH}"
    MRMPANEL_DATA="$DATA_ROOT" MRMPANEL_FEATURES="$FEATURES_FILE" \
      "${INSTALL_ROOT}/panel/.venv/bin/python" - <<'PY'
from app.services.webmail_sso import ensure_webmail_master

ensure_webmail_master()
print("webmail SSO master account ready")
PY
  ) || warn "Could not bootstrap webmail SSO master account"
}

sync_operator_home_access() {
  (
    cd "${INSTALL_ROOT}/panel"
    export PYTHONPATH="${INSTALL_ROOT}/panel${PYTHONPATH:+:$PYTHONPATH}"
    MRMPANEL_DATA="$DATA_ROOT" MRMPANEL_FEATURES="$FEATURES_FILE" \
      "${INSTALL_ROOT}/panel/.venv/bin/python" - <<'PY'
from pathlib import Path

from app.config import load_features
from app.services.users import grant_operator_access, operator_user

features = load_features()
if operator_user():
    grant_operator_access(Path("/home"))
    print(f"{operator_user()} can manage all hosting homes")
if features.get("mail"):
    from app.services import mail

    fixed = mail.repair_mailbox_permissions()
    print(f"mail permissions repaired for {len(fixed)} mailbox(es)")
PY
  ) || warn "Could not sync home/mail access permissions"
}

start_stack() {
  local profiles
  profiles="$(compose_profiles)"
  log "Starting compose profiles: $profiles"
  cd "$COMPOSE_DIR"
  export MRMPANEL_HOSTNAME="$SERVER_HOSTNAME"
  export MRMPANEL_MARIADB_ROOT_PASSWORD MRMPANEL_POSTGRES_PASSWORD MRMPANEL_ROUNDCUBE_DB_PASSWORD MRMPANEL_ROUNDCUBE_DES_KEY
  export MRMPANEL_MAIL_DOMAIN MRMPANEL_PDNS_API_KEY
  mkdir -p "${DATA_ROOT}/dns"
  # PowerDNS container runs as uid/gid 953
  chown 953:953 "${DATA_ROOT}/dns" 2>/dev/null || true
  chmod 775 "${DATA_ROOT}/dns" 2>/dev/null || true
  bootstrap_admin
  install_panel_venv
  install_jail_helpers
  install_systemd_unit
  sync_operator_home_access
  # Seed hosting plans and backfill missing plan_id on upgrades
  (
    cd "${INSTALL_ROOT}/panel"
    export PYTHONPATH="${INSTALL_ROOT}/panel${PYTHONPATH:+:$PYTHONPATH}"
    export MRMPANEL_DATA="${DATA_ROOT}"
    export MRMPANEL_FEATURES="${FEATURES_FILE}"
    "${INSTALL_ROOT}/panel/.venv/bin/python" - <<'PY' || true
from app.services import plans
plans.ensure_plans()
n = plans.backfill_user_plans()
print(f"[mrmpanel] plans ready (backfilled {n} user(s))")
PY
  ) || warn "Could not seed hosting plans"

  prepare_mail_tls
  local args=()
  IFS=',' read -ra PARTS <<< "$profiles"
  for p in "${PARTS[@]}"; do
    args+=(--profile "$p")
  done
  if [[ ${#args[@]} -gt 0 ]]; then
    docker network create mrmpanel 2>/dev/null || true
    docker compose "${args[@]}" up -d
    # Ensure Traefik can list containers on SELinux-enforcing hosts
    if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce 2>/dev/null)" == "Enforcing" ]]; then
      if [[ "$FEATURE_WEB" == "1" ]]; then
        log "SELinux enforcing — recreating Traefik with label=disable for Docker socket access"
        docker compose --profile web up -d --force-recreate traefik >/dev/null 2>&1 || true
      fi
    fi
  fi
  bootstrap_mail_account
  sync_mail_tls
  sync_webmail_routes
  bootstrap_webmail_sso
  # PYTHONPATH for bootstrap_dns_zone
  if [[ "$FEATURE_DNS" == "1" ]]; then
    (
      cd "${INSTALL_ROOT}/panel"
      export PYTHONPATH="${INSTALL_ROOT}/panel${PYTHONPATH:+:$PYTHONPATH}"
      bootstrap_dns_zone
    )
  fi
}

create_system_user() {
  if ! id mrmpanel &>/dev/null; then
    useradd --system --home "$DATA_ROOT" --shell /usr/sbin/nologin mrmpanel || true
  fi
}

print_summary() {
  local panel_ip panel_url
  panel_ip="$(get_public_ip)"
  [[ -n "$panel_ip" ]] || panel_ip="$(get_local_ip)"
  [[ -n "$panel_ip" ]] || panel_ip="SERVER_IP"
  panel_url="http://${panel_ip}:8080"

  echo
  echo -e "${CYAN}════════════════════════════════════════${NC}"
  echo -e "${CYAN} mrmpanel ${MRMPANEL_VERSION} installed${NC}"
  echo -e "${CYAN}════════════════════════════════════════${NC}"
  echo "  Hostname : $SERVER_HOSTNAME"
  [[ -n "$OPERATOR_USER" ]] && echo "  Operator : $OPERATOR_USER (SFTP access to all /home files)"
  echo "  Features : web=$FEATURE_WEB mail=$FEATURE_MAIL mariadb=$FEATURE_MARIADB postgres=$FEATURE_POSTGRES dns=$FEATURE_DNS"
  echo
  echo "  Dashboard: ${panel_url}"
  if [[ -n "${SERVER_HOSTNAME:-}" && "$SERVER_HOSTNAME" != "$panel_ip" ]]; then
    echo "             http://${SERVER_HOSTNAME}:8080"
    if [[ "$FEATURE_WEB" == "1" && "$SERVER_HOSTNAME" == *.* ]]; then
      echo "             https://${SERVER_HOSTNAME} (free certificate, ready in ~1 min)"
    fi
  fi
  echo "  Username : admin"
  echo "  Password : see ${DATA_ROOT}/secrets/admin_password"
  if [[ -n "${OPENED_PORTS:-}" ]]; then
    echo "  Firewall : opened ${OPENED_PORTS}"
  fi
  echo
  echo "  Data     : $DATA_ROOT"
  echo "  App      : $INSTALL_ROOT"
  if [[ "$FEATURE_MAIL" == "1" ]]; then
    echo "  Mail     : ensure MX/SPF/DKIM/DMARC (see panel dashboard)"
    if [[ "$FEATURE_WEB" == "1" ]]; then
      echo "  Webmail  : https://<managed-domain>/webmail/"
    fi
  fi
  echo "  Backup   : sudo mrmpanel-backup"
  echo "  Restore  : sudo mrmpanel-restore /var/backups/mrmpanel/<archive>.tar.gz"
  if [[ "$FEATURE_DNS" == "1" ]]; then
    echo
    echo "  DNS nameservers (set at your registrar with glue A records):"
    echo "    ${NS1_HOSTNAME:-ns1} → ${NS1_IP:-$panel_ip}"
    echo "    ${NS2_HOSTNAME:-ns2} → ${NS2_IP:-$panel_ip}"
    echo "  For .co.za: create child NS/glue at the registrar first, then set nameservers."
    echo "  Zone must already be authoritative here before ZACR will accept the change."
    echo "  Child domains on this server should use those NS hostnames."
    echo "  Change NS IPs later in panel Settings."
  fi
  echo
}

main() {
  parse_args "$@"
  need_root
  detect_os

  # Auto-resume if a previous boot scheduled continuation
  if [[ "$RESUMING" != "1" ]] && [[ -f "$RESUME_CONF" ]]; then
    load_resume || true
  fi

  # Feature prompts before preflight so port checks match selection
  if [[ "$NON_INTERACTIVE" == "0" && "$RESUMING" != "1" ]] && [[ -z "${MRMPANEL_SKIP_FEATURE_PROMPT:-}" ]]; then
    # If user only passed --all or nothing specific beyond defaults, offer menu
    if [[ $# -eq 0 ]] || [[ "$*" != *--web* && "$*" != *--mail* && "$*" != *--mariadb* && "$*" != *--postgres* && "$*" != *--dns* && "$*" != *--all* && "$*" != *--resume* ]]; then
      interactive_features
    fi
  fi
  preflight
  ensure_dns_tools
  hostname_flow
  install_deps
  # Ensure rsync available for copy
  if [[ "$PKG_MGR" == "dnf" ]]; then dnf -y install rsync python3 >/dev/null; else apt-get install -y rsync python3 >/dev/null; fi
  detect_operator_user

  # Collect secrets before Docker so an EL10 reboot can resume without prompts
  mkdir -p "$DATA_ROOT"
  create_system_user
  log "Configuring secrets…"
  gen_secrets
  write_resume_conf

  install_docker
  copy_tree
  log "Configuring services…"
  write_features
  patch_traefik_email
  write_traefik_panel_route
  open_firewall_ports
  start_stack
  clear_resume
  print_summary
}

main "$@"
