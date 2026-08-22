"""Panel public HTTP (:8080) firewall toggle."""

from __future__ import annotations

import subprocess
from typing import Any

from ..config import load_features, save_features
from . import certs


def panel_http_public(features: dict[str, Any] | None = None) -> bool:
    features = features or load_features()
    return bool(features.get("panel_http_public", True))


def panel_http_status() -> dict[str, Any]:
    features = load_features()
    hostname = str(features.get("hostname") or "")
    ssl = certs.hostname_ssl_status(hostname)
    public = panel_http_public(features)
    return {
        "public": public,
        "can_disable": bool(ssl.get("trusted")),
        "ssl_trusted": bool(ssl.get("trusted")),
        "hostname": hostname,
        "detail": (
            "Port 8080 is open on the firewall (direct HTTP panel access)."
            if public
            else "Port 8080 is closed on the firewall — use HTTPS on the hostname."
        ),
    }


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _has_cmd(name: str) -> bool:
    return _run(["bash", "-c", f"command -v {name}"]).returncode == 0


def _firewalld_set_8080(*, allow: bool) -> str:
    if not _has_cmd("firewall-cmd"):
        return ""
    if _run(["systemctl", "is-active", "--quiet", "firewalld"]).returncode != 0:
        _run(["systemctl", "enable", "--now", "firewalld"])
    action = "--add-port" if allow else "--remove-port"
    proc = _run(["firewall-cmd", "--permanent", action, "8080/tcp"])
    _run(["firewall-cmd", "--reload"])
    err = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode != 0:
        upper = err.upper()
        if "ALREADY_ENABLED" in upper or "NOT_ENABLED" in upper or "already" in err.lower():
            return f"firewalld: 8080 {'open' if allow else 'closed'} (ok)"
        return err or "firewalld update failed"
    return "firewalld updated"


def _ufw_set_8080(*, allow: bool) -> str:
    if not _has_cmd("ufw"):
        return ""
    if allow:
        proc = _run(["ufw", "allow", "8080/tcp"])
    else:
        proc = _run(["ufw", "--force", "delete", "allow", "8080/tcp"])
    err = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode != 0:
        if "Could not delete" in err or "Skipping" in err or "nonexistent" in err.lower():
            return "ufw: no matching 8080 rule (ok)"
        return err or "ufw update failed"
    return "ufw updated"


def _schedule_panel_restart() -> str:
    """Restart after response so SessionMiddleware https_only picks up the flag."""
    try:
        subprocess.Popen(
            ["bash", "-c", "sleep 2; systemctl restart mrmpanel.service"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "panel restart scheduled (~2s)"
    except OSError as exc:
        return f"restart panel manually: {exc}"


def set_panel_http_public(enabled: bool, *, require_ssl: bool = True) -> dict[str, Any]:
    """Open or close WAN access to panel :8080 via firewalld/ufw."""
    features = load_features()
    hostname = str(features.get("hostname") or "")
    ssl = certs.hostname_ssl_status(hostname)
    if not enabled and require_ssl and not ssl.get("trusted"):
        raise ValueError(
            "Refuse to disable public HTTP until the hostname has a trusted SSL certificate"
        )

    notes: list[str] = []
    for fn in (_firewalld_set_8080, _ufw_set_8080):
        try:
            msg = fn(allow=enabled)
            if msg:
                notes.append(msg)
        except (OSError, subprocess.SubprocessError) as exc:
            notes.append(str(exc))

    if not notes:
        notes.append(
            "No firewalld/ufw detected — close 8080 in your cloud security group manually"
        )

    features["panel_http_public"] = bool(enabled)
    save_features(features)
    notes.append(_schedule_panel_restart())

    return {
        "ok": True,
        "public": bool(enabled),
        "notes": notes,
        "detail": (
            "Port 8080 is open on the firewall (direct HTTP panel access)."
            if enabled
            else "Port 8080 is closed on the firewall — use HTTPS on the hostname."
        ),
    }
