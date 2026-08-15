"""TLS / certificate status helpers for the admin dashboard."""

from __future__ import annotations

import re
import socket
import ssl
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import load_features


def _parse_openssl_dates(host: str, timeout: float = 4.0) -> dict[str, str | None]:
    """Best-effort issuer / notAfter via openssl (works for self-signed too)."""
    out: dict[str, str | None] = {"expires": None, "issuer": None}
    try:
        proc = subprocess.run(
            [
                "openssl",
                "s_client",
                "-connect",
                f"{host}:443",
                "-servername",
                host,
            ],
            input=b"",
            capture_output=True,
            timeout=timeout + 2,
        )
        pem = proc.stdout
        if b"BEGIN CERTIFICATE" not in pem:
            return out
        x509 = subprocess.run(
            ["openssl", "x509", "-noout", "-dates", "-issuer"],
            input=pem,
            capture_output=True,
            timeout=3,
        )
        text = (x509.stdout or b"").decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("notAfter="):
                raw = line.split("=", 1)[1].strip()
                try:
                    exp = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(
                        tzinfo=timezone.utc
                    )
                    out["expires"] = exp.date().isoformat()
                except ValueError:
                    out["expires"] = raw
            elif line.startswith("issuer="):
                issuer = line.split("=", 1)[1].strip()
                m = re.search(r"O\s*=\s*([^/,]+)", issuer) or re.search(
                    r"CN\s*=\s*([^/,]+)", issuer
                )
                out["issuer"] = (m.group(1).strip() if m else issuer)[:120]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return out


def hostname_ssl_status(hostname: str | None, timeout: float = 4.0) -> dict[str, Any]:
    """Probe hostname:443 and report whether a certificate is present.

    Returns keys: hostname, has_cert, trusted, detail, expires, issuer, error.
    """
    host = (hostname or "").strip().rstrip(".").lower()
    out: dict[str, Any] = {
        "hostname": host,
        "has_cert": False,
        "trusted": False,
        "detail": "",
        "expires": None,
        "issuer": None,
        "error": None,
    }
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        out["detail"] = "No public hostname configured."
        out["error"] = "no_hostname"
        return out

    # First: any certificate at all (even self-signed / Traefik default)
    try:
        insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with insecure.wrap_socket(sock, server_hostname=host) as ssock:
                der = ssock.getpeercert(binary_form=True)
        if not der:
            out["detail"] = f"No certificate presented on {host}:443."
            out["error"] = "no_peer_cert"
            return out
        out["has_cert"] = True
    except OSError as exc:
        out["detail"] = f"Could not reach {host}:443 ({exc})."
        out["error"] = "connect"
        return out
    except ssl.SSLError as exc:
        out["detail"] = f"TLS handshake failed for {host}:443 ({exc})."
        out["error"] = "tls"
        return out

    meta = _parse_openssl_dates(host, timeout=timeout)
    out["expires"] = meta.get("expires")
    out["issuer"] = meta.get("issuer")

    # Second: is it trusted for this hostname? (real LE / public CA)
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                pass
        out["trusted"] = True
        exp_note = f" Expires {out['expires']}." if out.get("expires") else ""
        issuer_note = f" Issuer: {out['issuer']}." if out.get("issuer") else ""
        out["detail"] = (
            f"{host} has a trusted SSL certificate on port 443.{issuer_note}{exp_note}"
        )
    except ssl.SSLCertVerificationError:
        out["trusted"] = False
        exp_note = f" Expires {out['expires']}." if out.get("expires") else ""
        issuer_note = f" Issuer: {out['issuer']}." if out.get("issuer") else ""
        out["detail"] = (
            f"{host} presents an SSL certificate on port 443, but it is not trusted "
            f"(likely Traefik's default/self-signed cert).{issuer_note}{exp_note}"
        )
    except OSError as exc:
        out["trusted"] = False
        out["detail"] = f"{host} has a certificate, but a trusted check failed ({exc})."

    return out


def _install_root() -> Path:
    installed = Path("/opt/mrmpanel")
    if installed.is_dir():
        return installed
    return Path(__file__).resolve().parents[3]


def _resolved_addresses(host: str) -> set[str]:
    try:
        return {
            item[4][0]
            for item in socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)
        }
    except socket.gaierror:
        return set()


def _panel_route(host: str) -> str:
    return f"""# Generated by mrmpanel — HTTPS front end for {host}.
http:
  routers:
    mrmpanel-panel-secure:
      rule: "Host(`{host}`)"
      entryPoints: [websecure]
      service: mrmpanel-panel
      tls:
        certResolver: letsencrypt
    mrmpanel-panel-web:
      rule: "Host(`{host}`)"
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
"""


def activate_hostname_ssl(progress: Any) -> dict[str, Any]:
    """Configure panel HTTPS and wait for a trusted certificate."""
    features = load_features()
    host = str(features.get("hostname") or "").strip().rstrip(".").lower()
    public_ip = str(features.get("public_ip") or "").strip()

    progress(f"Checking configured hostname: {host or 'not set'}", 5)
    if not features.get("web"):
        raise ValueError("The Web module is not enabled, so Traefik is unavailable.")
    if not host or host == "localhost" or "." not in host:
        raise ValueError("Set a public server hostname before activating SSL.")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
        raise ValueError(f"The configured hostname is invalid: {host}")

    existing = hostname_ssl_status(host, timeout=3)
    if existing.get("trusted"):
        progress("A trusted SSL certificate is already active.", 100)
        return {"url": f"https://{host}/", "status": existing}

    progress("Checking public DNS…", 12)
    addresses = _resolved_addresses(host)
    if not addresses:
        raise ValueError(
            f"{host} does not resolve in public DNS. Add its A record first."
        )
    progress(f"DNS resolves {host} to {', '.join(sorted(addresses))}", 18)
    if public_ip and public_ip not in addresses:
        raise ValueError(
            f"{host} resolves to {', '.join(sorted(addresses))}, not this server "
            f"({public_ip}). Correct the A record and try again."
        )

    root = _install_root()
    compose_dir = root / "compose"
    traefik_cfg = compose_dir / "traefik" / "traefik.yml"
    if not traefik_cfg.is_file():
        raise ValueError("Traefik configuration is missing. Re-run the panel upgrade.")
    cfg_text = traefik_cfg.read_text()
    if "certificatesResolvers:" not in cfg_text or "letsencrypt:" not in cfg_text:
        raise ValueError("The Let's Encrypt resolver is missing from Traefik.")

    dynamic_dir = compose_dir / "traefik" / "dynamic"
    dynamic_dir.mkdir(parents=True, exist_ok=True)
    route = dynamic_dir / "panel.yml"
    tmp = route.with_suffix(".tmp")
    tmp.write_text(_panel_route(host))
    tmp.replace(route)
    progress("HTTPS route written. Starting Traefik…", 25)

    proc = subprocess.run(
        ["docker", "compose", "--profile", "web", "up", "-d", "traefik"],
        cwd=compose_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = "\n".join(
        line.strip()
        for line in (proc.stdout + "\n" + proc.stderr).splitlines()
        if line.strip()
    )
    if output:
        for line in output.splitlines()[-8:]:
            progress(line, 30)
    if proc.returncode:
        raise ValueError(f"Traefik could not start (exit {proc.returncode}).")

    progress("Requesting a free certificate from Let's Encrypt…", 35)
    deadline = time.monotonic() + 100
    attempt = 0
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        attempt += 1
        last = hostname_ssl_status(host, timeout=3)
        if last.get("trusted"):
            progress(
                f"Success: trusted certificate issued by "
                f"{last.get('issuer') or 'a public certificate authority'}.",
                100,
            )
            if last.get("expires"):
                progress(f"Certificate expires {last['expires']}.", 100)
            return {"url": f"https://{host}/", "status": last}
        pct = min(90, 35 + attempt * 5)
        progress(
            f"Waiting for certificate validation (check {attempt})…",
            pct,
        )
        time.sleep(5)

    detail = last.get("detail") or "No trusted certificate was returned."
    raise ValueError(
        f"Let's Encrypt did not issue a certificate within 100 seconds. {detail} "
        "Check that ports 80 and 443 are publicly reachable, then try again."
    )
