#!/usr/bin/env python3
"""Export one hostname certificate from Traefik acme.json for docker-mailserver."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: sync-mail-tls.py ACME_JSON HOSTNAME OUTPUT_DIR", file=sys.stderr)
        return 2
    acme_path = Path(sys.argv[1])
    hostname = sys.argv[2].strip().rstrip(".").lower()
    output = Path(sys.argv[3])
    if not acme_path.is_file():
        return 1

    data = json.loads(acme_path.read_text())
    for resolver in data.values():
        if not isinstance(resolver, dict):
            continue
        for item in resolver.get("Certificates", []):
            domain = item.get("domain") or {}
            names = {
                str(domain.get("main") or "").lower(),
                *(str(name).lower() for name in domain.get("sans") or []),
            }
            if hostname not in names:
                continue
            cert = base64.b64decode(item["certificate"])
            key = base64.b64decode(item["key"])
            if b"BEGIN CERTIFICATE" not in cert or b"PRIVATE KEY" not in key:
                raise ValueError("Traefik certificate data is not PEM")
            output.mkdir(parents=True, exist_ok=True)
            cert_tmp = output / "fullchain.pem.tmp"
            key_tmp = output / "privkey.pem.tmp"
            cert_tmp.write_bytes(cert)
            key_tmp.write_bytes(key)
            os.chmod(cert_tmp, 0o644)
            os.chmod(key_tmp, 0o600)
            cert_tmp.replace(output / "fullchain.pem")
            key_tmp.replace(output / "privkey.pem")
            print(f"exported {hostname}")
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
