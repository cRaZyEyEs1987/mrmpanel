#!/usr/bin/env bash
# Dev helper: run panel against local data dir (no root required for UI)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${MRMPANEL_DATA:-$ROOT/.dev-data}"
mkdir -p "$DATA/secrets" "$DATA/sites" "$DATA/dismissed" "$DATA/users"
if [[ ! -f "$DATA/features.json" ]]; then
  cat > "$DATA/features.json" <<EOF
{
  "version": "0.1.2",
  "hostname": "server.example.com",
  "web": true,
  "mail": false,
  "mariadb": true,
  "postgres": true,
  "dns": false,
  "public_ip": "127.0.0.1",
  "acme_email": "admin@localhost",
  "ns_base_domain": "example.com",
  "ns1_hostname": "ns1.example.com",
  "ns2_hostname": "ns2.example.com",
  "ns1_ip": "127.0.0.1",
  "ns2_ip": "127.0.0.1"
}
EOF
fi
if [[ ! -f "$DATA/secrets/admin_password" ]]; then
  echo "admin" > "$DATA/secrets/admin_password"
fi
cd "$ROOT/panel"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
export MRMPANEL_DATA="$DATA"
export MRMPANEL_STACKS="$ROOT/stacks"
export MRMPANEL_ASSETS="$ROOT/assets"
export MRMPANEL_FEATURES="$DATA/features.json"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
