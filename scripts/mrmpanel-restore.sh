#!/usr/bin/env bash
# Same-server mrmpanel restore (upgrade rollback).
# Requires mrmpanel already installed; replaces data/homes/certs and restarts services.
set -euo pipefail

DATA_ROOT="${MRMPANEL_DATA:-/var/lib/mrmpanel}"
INSTALL_ROOT="${MRMPANEL_INSTALL:-/opt/mrmpanel}"
COMPOSE_DIR="${INSTALL_ROOT}/compose"
ETC_DIR="/etc/mrmpanel"
TRAEFIK_VOLUME="${MRMPANEL_TRAEFIK_VOLUME:-mrmpanel_traefik_letsencrypt}"

YES=0
ARCHIVE=""

usage() {
  cat <<EOF
Usage: sudo mrmpanel-restore [--yes] <archive.tar.gz>

Restores panel data, hosting homes, /etc/mrmpanel, and Traefik certificates
onto this server (same-server rollback). mrmpanel must already be installed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      ARCHIVE="$1"
      shift
      ;;
  esac
done

[[ $(id -u) -eq 0 ]] || { echo "Run as root (sudo mrmpanel-restore …)" >&2; exit 1; }
[[ -n "$ARCHIVE" ]] || { usage >&2; exit 1; }
[[ -f "$ARCHIVE" ]] || { echo "Archive not found: $ARCHIVE" >&2; exit 1; }
[[ -d "$INSTALL_ROOT" ]] || { echo "mrmpanel not installed at $INSTALL_ROOT" >&2; exit 1; }

STAGE="$(mktemp -d /tmp/mrmpanel-restore-XXXXXX)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

log() { echo "[mrmpanel-restore] $*"; }

log "Extracting archive…"
tar -C "$STAGE" -xzf "$ARCHIVE"

[[ -f "$STAGE/manifest.json" ]] || { echo "Invalid archive: missing manifest.json" >&2; exit 1; }
[[ -d "$STAGE/data" ]] || { echo "Invalid archive: missing data/" >&2; exit 1; }

python3 - "$STAGE/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(f"format={m.get('format')} version={m.get('panel_version')} host={m.get('hostname')} users={','.join(m.get('hosting_users') or [])}")
if int(m.get("format") or 0) != 1:
    raise SystemExit("Unsupported backup format (need format 1)")
PY

if [[ "$YES" != "1" ]]; then
  echo
  echo "This will REPLACE:"
  echo "  - ${DATA_ROOT}"
  echo "  - hosting homes listed in the archive"
  echo "  - ${ETC_DIR} (if present in archive)"
  echo "  - Docker volume ${TRAEFIK_VOLUME} (if present in archive)"
  echo
  read -r -p "Continue? [y/N] " ans
  [[ "${ans,,}" == "y" ]] || { echo "Aborted."; exit 0; }
fi

COMPOSE_PROFILE_ARGS=""
if [[ -f "$STAGE/data/features.json" ]]; then
  COMPOSE_PROFILE_ARGS="$(python3 - "$STAGE/data/features.json" <<'PY'
import json, sys
f = json.load(open(sys.argv[1]))
profiles = []
if f.get("web"): profiles.append("web")
if f.get("mail"): profiles.append("mail")
if f.get("web") and f.get("mail"): profiles.append("webmail")
if f.get("mariadb"): profiles.append("mariadb")
if f.get("postgres"): profiles.append("postgres")
if f.get("dns"): profiles.append("dns")
print(" ".join(f"--profile {p}" for p in profiles))
PY
)"
fi

log "Stopping panel and site containers…"
systemctl stop mrmpanel 2>/dev/null || true
docker ps -aq --filter "label=mrmpanel.site" | xargs -r docker rm -f >/dev/null 2>&1 || true

if [[ -d "$COMPOSE_DIR" ]]; then
  log "Stopping compose stack…"
  (
    cd "$COMPOSE_DIR"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_PROFILE_ARGS stop >/dev/null 2>&1 || true
  )
fi

log "Restoring ${DATA_ROOT}…"
mkdir -p "$DATA_ROOT"
# Replace contents; keep the directory inode/mount if any.
find "$DATA_ROOT" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
rsync -aHAX "$STAGE/data/" "$DATA_ROOT/"

if [[ -d "$STAGE/etc" ]] && [[ -n "$(ls -A "$STAGE/etc" 2>/dev/null || true)" ]]; then
  log "Restoring ${ETC_DIR}…"
  mkdir -p "$ETC_DIR"
  find "$ETC_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  rsync -aHAX "$STAGE/etc/" "$ETC_DIR/"
fi

if [[ -d "$STAGE/homes" ]]; then
  for user_dir in "$STAGE/homes"/*; do
    [[ -d "$user_dir" ]] || continue
    user="$(basename "$user_dir")"
    log "Restoring /home/${user}…"
    if ! id "$user" >/dev/null 2>&1; then
      log "Warning: system user ${user} does not exist — creating home only"
    fi
    mkdir -p "/home/${user}"
    find "/home/${user}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    rsync -aHAX "${user_dir}/" "/home/${user}/"
  done
fi

VOL_ARCHIVE="$STAGE/volumes/traefik_letsencrypt.tar.gz"
if [[ -f "$VOL_ARCHIVE" ]]; then
  log "Restoring Docker volume ${TRAEFIK_VOLUME}…"
  docker volume create "$TRAEFIK_VOLUME" >/dev/null
  docker run --rm \
    -v "${TRAEFIK_VOLUME}:/to" \
    -v "${STAGE}/volumes:/from:ro" \
    alpine:3.20 \
    sh -c 'rm -rf /to/..?* /to/.[!.]* /to/* 2>/dev/null; tar -C /to -xzf /from/traefik_letsencrypt.tar.gz'
fi

if [[ -d "$COMPOSE_DIR" ]]; then
  log "Starting compose stack…"
  (
    cd "$COMPOSE_DIR"
    # Load passwords/env if present so compose interpolates correctly.
    set +u
    # shellcheck disable=SC1091
    [[ -f /etc/mrmpanel/panel.env ]] && . /etc/mrmpanel/panel.env
    if [[ -f "$DATA_ROOT/secrets/mariadb_root_password" ]]; then
      export MRMPANEL_MARIADB_ROOT_PASSWORD
      MRMPANEL_MARIADB_ROOT_PASSWORD="$(tr -d '\n' < "$DATA_ROOT/secrets/mariadb_root_password")"
    fi
    if [[ -f "$DATA_ROOT/secrets/postgres_password" ]]; then
      export MRMPANEL_POSTGRES_PASSWORD
      MRMPANEL_POSTGRES_PASSWORD="$(tr -d '\n' < "$DATA_ROOT/secrets/postgres_password")"
    fi
    if [[ -f "$DATA_ROOT/secrets/roundcube_db_password" ]]; then
      export MRMPANEL_ROUNDCUBE_DB_PASSWORD
      MRMPANEL_ROUNDCUBE_DB_PASSWORD="$(tr -d '\n' < "$DATA_ROOT/secrets/roundcube_db_password")"
    fi
    if [[ -f "$DATA_ROOT/secrets/roundcube_des_key" ]]; then
      export MRMPANEL_ROUNDCUBE_DES_KEY
      MRMPANEL_ROUNDCUBE_DES_KEY="$(tr -d '\n' < "$DATA_ROOT/secrets/roundcube_des_key")"
    fi
    if [[ -f "$DATA_ROOT/secrets/pdns_api_key" ]]; then
      export MRMPANEL_PDNS_API_KEY
      MRMPANEL_PDNS_API_KEY="$(tr -d '\n' < "$DATA_ROOT/secrets/pdns_api_key")"
    fi
    if [[ -f "$DATA_ROOT/features.json" ]]; then
      export MRMPANEL_HOSTNAME
      MRMPANEL_HOSTNAME="$(python3 -c "import json; print(json.load(open('${DATA_ROOT}/features.json')).get('hostname',''))" 2>/dev/null || true)"
    fi
    set -u
    # Ensure external network exists
    docker network create mrmpanel >/dev/null 2>&1 || true
    # shellcheck disable=SC2086
    docker compose $COMPOSE_PROFILE_ARGS up -d
  )
fi

log "Starting panel…"
systemctl start mrmpanel 2>/dev/null || true
systemctl is-active mrmpanel >/dev/null 2>&1 && log "Panel service is active" || log "Warning: panel service not active"

# Best-effort: recreate site containers from saved metadata.
if [[ -x "${INSTALL_ROOT}/panel/.venv/bin/python" ]]; then
  log "Refreshing site containers from saved metadata…"
  (
    cd "${INSTALL_ROOT}/panel"
    export PYTHONPATH="${INSTALL_ROOT}/panel${PYTHONPATH:+:$PYTHONPATH}"
    MRMPANEL_DATA="$DATA_ROOT" .venv/bin/python - <<'PY' || true
from pathlib import Path

from app.services import sites
from app.services.stacks import get_stack
from app.services.users import ensure_domain_dir

for site in sites.list_sites():
    site_id = site.get("id")
    domain = site.get("domain") or site_id
    try:
        stack_id = site.get("stack") or ""
        stack = get_stack(stack_id)
        if not stack or not site_id:
            print(f"skip {domain}: missing stack/id")
            continue
        domain_dir = Path(site.get("path") or "")
        if not domain_dir.is_dir():
            domain_dir = ensure_domain_dir(site["username"], site["domain"])
        container, cname, _image = sites._run_container(
            username=site["username"],
            domain=site["domain"],
            stack=stack,
            stack_id=stack_id,
            version=site.get("version"),
            domain_dir=domain_dir,
            db_info=site.get("db"),
            site_id=site_id,
        )
        site["container"] = cname
        site["container_id"] = container.id
        sites._save_site(site)
        print(f"redeployed {domain}")
    except Exception as exc:
        print(f"skip {domain}: {exc}")
PY
  ) || true
fi

log "Restore complete."
echo
echo "Verify:"
echo "  - Panel UI"
echo "  - Sites / HTTPS"
echo "  - Mail + webmail SSO"
echo "  - DNS (if enabled)"
