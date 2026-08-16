#!/usr/bin/env bash
# One-command mrmpanel backup for same-server upgrade rollback.
# Captures panel data, hosting homes, /etc/mrmpanel, and Traefik ACME volume.
set -euo pipefail

DATA_ROOT="${MRMPANEL_DATA:-/var/lib/mrmpanel}"
INSTALL_ROOT="${MRMPANEL_INSTALL:-/opt/mrmpanel}"
COMPOSE_DIR="${INSTALL_ROOT}/compose"
ETC_DIR="/etc/mrmpanel"
BACKUP_ROOT="${MRMPANEL_BACKUP_DIR:-/var/backups/mrmpanel}"
TRAEFIK_VOLUME="${MRMPANEL_TRAEFIK_VOLUME:-mrmpanel_traefik_letsencrypt}"

[[ $(id -u) -eq 0 ]] || { echo "Run as root (sudo mrmpanel-backup)" >&2; exit 1; }
[[ -d "$DATA_ROOT" ]] || { echo "Missing data root: $DATA_ROOT" >&2; exit 1; }

OUT="${1:-}"
if [[ -z "$OUT" ]]; then
  mkdir -p "$BACKUP_ROOT"
  OUT="${BACKUP_ROOT}/mrmpanel-$(date -u +%Y%m%d-%H%M%S).tar.gz"
elif [[ "$OUT" == */ ]] || [[ -d "$OUT" ]]; then
  mkdir -p "$OUT"
  OUT="${OUT%/}/mrmpanel-$(date -u +%Y%m%d-%H%M%S).tar.gz"
else
  mkdir -p "$(dirname "$OUT")"
fi

STAGE="$(mktemp -d /tmp/mrmpanel-backup-XXXXXX)"
cleanup() {
  # Always try to bring stopped services back up.
  if [[ -n "${QUIESCED:-}" && -d "$COMPOSE_DIR" ]]; then
    (
      cd "$COMPOSE_DIR"
      # shellcheck disable=SC2086
      docker compose $COMPOSE_PROFILE_ARGS start ${QUIESCE_SERVICES[*]} >/dev/null 2>&1 || true
    )
  fi
  rm -rf "$STAGE"
}
trap cleanup EXIT

log() { echo "[mrmpanel-backup] $*"; }

# Build compose profile flags from features.json when present.
COMPOSE_PROFILE_ARGS=""
build_profile_args() {
  local features="$DATA_ROOT/features.json"
  [[ -f "$features" ]] || return 0
  local args
  args="$(python3 - "$features" <<'PY'
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
  COMPOSE_PROFILE_ARGS="$args"
}

list_hosting_users() {
  python3 - "$DATA_ROOT" <<'PY'
import json, sys
from pathlib import Path
users_dir = Path(sys.argv[1]) / "users"
names = []
if users_dir.is_dir():
    for path in sorted(users_dir.glob("*.json")):
        try:
            meta = json.loads(path.read_text())
            name = str(meta.get("username") or path.stem).strip()
        except Exception:
            name = path.stem
        if name and name not in ("root",):
            names.append(name)
print("\n".join(names))
PY
}

build_profile_args

QUIESCE_SERVICES=()
for svc in mariadb postgres roundcube-db mail pdns roundcube; do
  if docker inspect "mrmpanel-${svc}" >/dev/null 2>&1 \
    || docker inspect "mrmpanel-${svc}-1" >/dev/null 2>&1; then
    QUIESCE_SERVICES+=("$svc")
  fi
done
# Compose service names: roundcube-db stays roundcube-db; others match.
# Map container names to compose service names where needed.
COMPOSE_STOP=()
for svc in "${QUIESCE_SERVICES[@]+"${QUIESCE_SERVICES[@]}"}"; do
  COMPOSE_STOP+=("$svc")
done

if [[ ${#COMPOSE_STOP[@]} -gt 0 && -d "$COMPOSE_DIR" ]]; then
  log "Quiescing services for consistent copy: ${COMPOSE_STOP[*]}"
  (
    cd "$COMPOSE_DIR"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_PROFILE_ARGS stop "${COMPOSE_STOP[@]}" >/dev/null
  )
  QUIESCED=1
else
  log "No database/mail containers to quiesce (or compose missing)"
fi

mkdir -p "$STAGE"/{data,etc,homes,volumes}

log "Copying ${DATA_ROOT}…"
# Preserve ACLs/xattrs; skip noisy/ephemeral paths.
rsync -aHAX \
  --exclude 'webmail-sso/*.json' \
  --exclude 'mail/logs/**' \
  "${DATA_ROOT}/" "$STAGE/data/"

if [[ -d "$ETC_DIR" ]]; then
  log "Copying ${ETC_DIR}…"
  rsync -aHAX "${ETC_DIR}/" "$STAGE/etc/"
fi

mapfile -t USERS < <(list_hosting_users)
if [[ ${#USERS[@]} -eq 0 ]]; then
  log "No hosting users found under ${DATA_ROOT}/users"
else
  for user in "${USERS[@]}"; do
    if [[ -d "/home/${user}" ]]; then
      log "Copying /home/${user}…"
      mkdir -p "$STAGE/homes/${user}"
      rsync -aHAX "/home/${user}/" "$STAGE/homes/${user}/"
    else
      log "Warning: /home/${user} missing — skipped"
    fi
  done
fi

if docker volume inspect "$TRAEFIK_VOLUME" >/dev/null 2>&1; then
  log "Exporting Docker volume ${TRAEFIK_VOLUME}…"
  docker run --rm \
    -v "${TRAEFIK_VOLUME}:/from:ro" \
    -v "${STAGE}/volumes:/to" \
    alpine:3.20 \
    tar -C /from -czf /to/traefik_letsencrypt.tar.gz .
else
  log "Traefik volume ${TRAEFIK_VOLUME} not found — skipped"
fi

VERSION="unknown"
HOSTNAME_VAL="$(hostname -f 2>/dev/null || hostname || echo unknown)"
if [[ -f "$DATA_ROOT/features.json" ]]; then
  VERSION="$(python3 -c "import json; print(json.load(open('${DATA_ROOT}/features.json')).get('version','unknown'))" 2>/dev/null || echo unknown)"
fi

python3 - "$STAGE/manifest.json" "$VERSION" "$HOSTNAME_VAL" "$TRAEFIK_VOLUME" "$DATA_ROOT" <<'PY'
import json, os, sys, time
from pathlib import Path

out, version, hostname, volume, data_root = sys.argv[1:6]
users_dir = Path(data_root) / "users"
users = []
if users_dir.is_dir():
    for path in sorted(users_dir.glob("*.json")):
        try:
            users.append(json.loads(path.read_text()).get("username") or path.stem)
        except Exception:
            users.append(path.stem)
features = {}
feat_path = Path(data_root) / "features.json"
if feat_path.exists():
    try:
        features = json.loads(feat_path.read_text())
    except Exception:
        features = {}
stage = Path(out).parent
manifest = {
    "format": 1,
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "panel_version": version,
    "hostname": hostname,
    "features": features,
    "hosting_users": users,
    "compose_project": "mrmpanel",
    "traefik_volume": volume,
    "includes": {
        "data": True,
        "etc": (stage / "etc").exists() and any((stage / "etc").iterdir()) if (stage / "etc").exists() else False,
        "homes": sorted(p.name for p in (stage / "homes").iterdir()) if (stage / "homes").exists() else [],
        "traefik_volume": (stage / "volumes" / "traefik_letsencrypt.tar.gz").is_file(),
    },
    "excludes": ["webmail-sso/*.json", "mail/logs/**", "/opt/mrmpanel"],
}
Path(out).write_text(json.dumps(manifest, indent=2) + "\n")
PY

log "Creating archive ${OUT}…"
tar -C "$STAGE" -czf "$OUT" manifest.json data etc homes volumes
chmod 600 "$OUT"
SIZE="$(du -h "$OUT" | awk '{print $1}')"
log "Done: ${OUT} (${SIZE})"
echo "$OUT"
