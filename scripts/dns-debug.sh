#!/usr/bin/env bash
# Nameserver acceptance debug — any TLD (run on the mrmpanel server).
# Usage:
#   sudo bash scripts/dns-debug.sh
#   sudo bash scripts/dns-debug.sh example.com
#   sudo bash scripts/dns-debug.sh example.co.za
set -euo pipefail

DOMAIN="${1:-}"
PANEL_ROOT="${MRMPANEL_ROOT:-/opt/mrmpanel}"
VENV="$PANEL_ROOT/panel/.venv/bin/python"
APP="$PANEL_ROOT/panel"

if [[ ! -x "$VENV" ]]; then
  # Dev checkout
  HERE="$(cd "$(dirname "$0")/.." && pwd)"
  VENV="$HERE/panel/.venv/bin/python"
  APP="$HERE/panel"
fi

if [[ ! -x "$VENV" ]]; then
  echo "Could not find panel venv at $PANEL_ROOT/panel/.venv or repo checkout." >&2
  exit 1
fi

export PYTHONPATH="$APP${PYTHONPATH:+:$PYTHONPATH}"
"$VENV" - <<PY
from app.services import dns
import json
import sys
domain = ${DOMAIN@Q} or None
if domain == "":
    domain = None
r = dns.diagnose_ns_acceptance(domain)
print("=== DNS debug:", r.get("domain"), "===")
print(r.get("summary"))
print()
for c in r.get("checks", []):
    mark = "PASS" if c["pass"] else "FAIL"
    crit = "*" if c.get("critical") else " "
    print(f"[{mark}]{crit} {c['name']}: {c['detail']}")
print()
if r.get("next_steps"):
    print("Next steps:")
    for i, s in enumerate(r["next_steps"], 1):
        print(f"  {i}. {s}")
# machine-readable
print()
print("--- json ---")
print(json.dumps(r, indent=2))
PY
