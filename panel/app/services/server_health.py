from __future__ import annotations

import socket
import subprocess
from typing import Any

from ..config import load_features


def _norm_host(value: str) -> str:
    return (value or "").strip().rstrip(".").lower()


def _dig_a(hostname: str) -> list[str]:
    host = _norm_host(hostname)
    if not host:
        return []
    try:
        proc = subprocess.run(
            ["dig", "+short", "+time=2", "+tries=1", "A", host],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _dig_ptr(ip: str) -> str | None:
    ip = (ip or "").strip()
    if not ip:
        return None
    try:
        proc = subprocess.run(
            ["dig", "+short", "+time=2", "+tries=1", "-x", ip],
            capture_output=True,
            text=True,
            timeout=8,
        )
        for line in proc.stdout.splitlines():
            name = _norm_host(line)
            if name:
                return name
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        name, _aliases, _ips = socket.gethostbyaddr(ip)
        return _norm_host(name)
    except OSError:
        return None


def ptr_status() -> dict[str, Any]:
    features = load_features()
    ip = str(features.get("public_ip") or "").strip()
    expected = _norm_host(str(features.get("hostname") or ""))
    if not ip:
        return {
            "ok": False,
            "status": "missing_ip",
            "detail": "Public IP is not set in features.",
            "ip": "",
            "expected": expected,
            "actual": None,
        }
    actual = _dig_ptr(ip)
    if not actual:
        return {
            "ok": False,
            "status": "missing",
            "detail": f"No PTR/RDNS found for {ip}.",
            "ip": ip,
            "expected": expected,
            "actual": None,
        }
    ok = bool(expected) and actual == expected
    return {
        "ok": ok,
        "status": "ok" if ok else "mismatch",
        "detail": (
            f"PTR {actual} matches hostname."
            if ok
            else f"PTR is {actual}; expected {expected or '(unset hostname)'}."
        ),
        "ip": ip,
        "expected": expected,
        "actual": actual,
    }


def _ns_row(label: str, hostname: str, expected_ip: str, public_ip: str) -> dict[str, Any]:
    host = _norm_host(hostname)
    expect = (expected_ip or public_ip or "").strip()
    if not host:
        return {
            "label": label,
            "hostname": "",
            "ok": False,
            "status": "unset",
            "expected_ip": expect,
            "actual_ips": [],
            "detail": f"{label} hostname is not configured.",
        }
    actual = _dig_a(host)
    if not actual:
        return {
            "label": label,
            "hostname": host,
            "ok": False,
            "status": "nxdomain",
            "expected_ip": expect,
            "actual_ips": [],
            "detail": f"{host} does not resolve (NXDOMAIN / no A).",
        }
    if not expect:
        return {
            "label": label,
            "hostname": host,
            "ok": False,
            "status": "no_expected",
            "expected_ip": "",
            "actual_ips": actual,
            "detail": f"{host} → {', '.join(actual)} (no expected IP configured).",
        }
    ok = expect in actual
    return {
        "label": label,
        "hostname": host,
        "ok": ok,
        "status": "ok" if ok else "mismatch",
        "expected_ip": expect,
        "actual_ips": actual,
        "detail": (
            f"{host} → {expect}"
            if ok
            else f"{host} → {', '.join(actual)}; expected {expect}."
        ),
    }


def nameserver_status() -> list[dict[str, Any]]:
    features = load_features()
    public_ip = str(features.get("public_ip") or "").strip()
    return [
        _ns_row(
            "NS1",
            str(features.get("ns1_hostname") or ""),
            str(features.get("ns1_ip") or ""),
            public_ip,
        ),
        _ns_row(
            "NS2",
            str(features.get("ns2_hostname") or ""),
            str(features.get("ns2_ip") or ""),
            public_ip,
        ),
    ]


def network_health() -> dict[str, Any]:
    ptr = ptr_status()
    nameservers = nameserver_status()
    return {
        "ptr": ptr,
        "nameservers": nameservers,
        "ok": bool(ptr.get("ok")) and all(row.get("ok") for row in nameservers),
    }
