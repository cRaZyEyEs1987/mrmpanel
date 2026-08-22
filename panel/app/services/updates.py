"""Host OS package updates and mrmpanel release upgrade checks."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import get_settings, load_features
from . import deploy_jobs

DEFAULT_MIRROR = "https://mrmpanel.hostingandstuff.online"
CACHE_TTL_SEC = 300
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _cache_path() -> Path:
    d = get_settings().data_dir / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "updates.json"


def _read_cache() -> dict[str, Any] | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    checked = float(data.get("checked_at") or 0)
    if time.time() - checked > CACHE_TTL_SEC:
        return None
    return data


def _write_cache(data: dict[str, Any]) -> None:
    data = {**data, "checked_at": time.time()}
    path = _cache_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def parse_version(value: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(value or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def version_newer(remote: str, local: str) -> bool:
    a = parse_version(remote)
    b = parse_version(local)
    if not a or not b:
        return False
    return a > b


def detect_pkg_mgr() -> str | None:
    if Path("/usr/bin/dnf").exists() or Path("/bin/dnf").exists():
        return "dnf"
    if Path("/usr/bin/yum").exists():
        return "yum"
    if Path("/usr/bin/apt-get").exists():
        return "apt"
    return None


def _os_id() -> str:
    path = Path("/etc/os-release")
    if not path.exists():
        return "linux"
    text = path.read_text()
    for key in ("PRETTY_NAME", "NAME"):
        m = re.search(rf'^{key}="?([^"\n]+)"?', text, re.M)
        if m:
            return m.group(1).strip()
    return "linux"


def check_os_updates() -> dict[str, Any]:
    mgr = detect_pkg_mgr()
    base = {
        "manager": mgr or "unknown",
        "os": _os_id(),
        "available": 0,
        "packages": [],
        "up_to_date": True,
        "error": None,
        "detail": "",
    }
    if not mgr:
        base["error"] = "No supported package manager (dnf/yum/apt)"
        base["detail"] = base["error"]
        return base
    try:
        if mgr in {"dnf", "yum"}:
            proc = _run([mgr, "check-update", "-q"], timeout=180)
            # dnf: 100 = updates available, 0 = none, other = error
            lines = [
                ln.strip()
                for ln in (proc.stdout or "").splitlines()
                if ln.strip() and not ln.startswith("Last metadata")
            ]
            # Filter header-ish lines
            pkgs = [
                ln.split()[0]
                for ln in lines
                if ln and not ln.startswith("Obsoleting") and " " in ln
            ]
            if proc.returncode == 100 or pkgs:
                base["available"] = len(pkgs) or max(1, len(lines))
                base["packages"] = pkgs[:40]
                base["up_to_date"] = False
                base["detail"] = f"{base['available']} package update(s) available"
            elif proc.returncode == 0:
                base["detail"] = "Host OS packages are up to date"
            else:
                err = (proc.stderr or proc.stdout or "").strip()
                base["error"] = err[-400:] or f"{mgr} check-update failed"
                base["detail"] = base["error"]
        else:
            _run(["apt-get", "update", "-qq"], timeout=180)
            proc = _run(
                ["apt", "list", "--upgradable"],
                timeout=120,
            )
            lines = [
                ln.strip()
                for ln in (proc.stdout or "").splitlines()
                if ln.strip() and not ln.startswith("Listing")
            ]
            pkgs = [ln.split("/", 1)[0] for ln in lines if "/" in ln]
            base["available"] = len(pkgs)
            base["packages"] = pkgs[:40]
            base["up_to_date"] = len(pkgs) == 0
            base["detail"] = (
                "Host OS packages are up to date"
                if base["up_to_date"]
                else f"{base['available']} package update(s) available"
            )
            if proc.returncode not in (0,):
                err = (proc.stderr or "").strip()
                if err and not pkgs:
                    base["error"] = err[-400:]
                    base["detail"] = base["error"]
    except (OSError, subprocess.SubprocessError) as exc:
        base["error"] = str(exc)
        base["detail"] = str(exc)
    return base


def mirror_url() -> str:
    return (
        os.environ.get("MRMPANEL_MIRROR")
        or DEFAULT_MIRROR
    ).rstrip("/")


def fetch_mirror_version() -> dict[str, Any]:
    url = f"{mirror_url()}/"
    out: dict[str, Any] = {
        "mirror": mirror_url(),
        "latest": None,
        "error": None,
    }
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            text = r.text
        m = re.search(
            r"Release\s*<strong>\s*([0-9]+\.[0-9]+\.[0-9]+)\s*</strong>",
            text,
            re.I,
        )
        if not m:
            m = re.search(r"\b([0-9]+\.[0-9]+\.[0-9]+)\b", text)
        if m:
            out["latest"] = m.group(1)
        else:
            out["error"] = "Could not parse mirror release version"
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def check_panel_updates() -> dict[str, Any]:
    features = load_features()
    current = str(features.get("version") or "").strip()
    remote = fetch_mirror_version()
    latest = remote.get("latest")
    newer = bool(latest and current and version_newer(str(latest), current))
    detail = ""
    if remote.get("error"):
        detail = str(remote["error"])
    elif not latest:
        detail = "Mirror version unknown"
    elif newer:
        detail = f"Update available: v{current} → v{latest}"
    else:
        detail = f"mrmpanel is current (v{current or '—'})"
    return {
        "current": current or None,
        "latest": latest,
        "mirror": remote.get("mirror"),
        "update_available": newer,
        "detail": detail,
        "error": remote.get("error"),
    }


def updates_status(*, refresh: bool = False) -> dict[str, Any]:
    if not refresh:
        cached = _read_cache()
        if cached:
            cached["cached"] = True
            return cached
    os_st = check_os_updates()
    panel_st = check_panel_updates()
    data = {
        "os": os_st,
        "panel": panel_st,
        "cached": False,
        "checked_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_cache(data)
    return data


def _feature_install_flags(features: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    mapping = (
        ("web", "--web"),
        ("mail", "--mail"),
        ("mariadb", "--mariadb"),
        ("postgres", "--postgres"),
        ("dns", "--dns"),
    )
    for key, flag in mapping:
        if features.get(key):
            flags.append(flag)
    if not flags:
        flags.append("--web")
    flags.extend(["--force", "--non-interactive"])
    hostname = str(features.get("hostname") or "").strip()
    if hostname:
        flags.extend(["--hostname", hostname])
    return flags


def run_os_upgrade(progress: deploy_jobs.ProgressFn) -> dict[str, Any]:
    mgr = detect_pkg_mgr()
    if not mgr:
        raise RuntimeError("No supported package manager")
    progress("Starting host OS package upgrade…", 5)
    if mgr in {"dnf", "yum"}:
        cmd = [mgr, "-y", "upgrade"]
    else:
        progress("Refreshing apt indexes…", 10)
        upd = _run(["apt-get", "update", "-y"], timeout=300)
        if upd.returncode != 0:
            raise RuntimeError((upd.stderr or upd.stdout or "apt-get update failed")[-800:])
        cmd = [
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "-y",
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
            "upgrade",
        ]
    progress(f"Running {' '.join(cmd)}…", 20)
    proc = _run(cmd, timeout=3600)
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    for chunk in out.splitlines()[-30:]:
        progress(chunk[:240], None)
    if proc.returncode != 0:
        raise RuntimeError(out[-800:] or f"{mgr} upgrade failed")
    progress("Host OS upgrade finished", 100)
    # Invalidate cache
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True, "manager": mgr, "log_tail": out[-1500:]}


def run_panel_upgrade(progress: deploy_jobs.ProgressFn) -> dict[str, Any]:
    features = load_features()
    mirror = mirror_url()
    flags = _feature_install_flags(features)
    data_dir = get_settings().data_dir
    secrets = data_dir / "secrets" / "admin_password"
    if not secrets.is_file():
        raise RuntimeError("Admin password file missing — cannot run non-interactive upgrade")

    progress("Backing up panel before upgrade…", 5)
    backup = _run(["mrmpanel-backup"], timeout=600)
    if backup.returncode != 0:
        raise RuntimeError(
            (backup.stderr or backup.stdout or "mrmpanel-backup failed")[-800:]
        )
    backup_path = (backup.stdout or "").strip().splitlines()[-1] if backup.stdout else ""
    progress(f"Backup ready: {backup_path or 'ok'}", 30)

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "panel-upgrade.log"
    script = data_dir / "cache" / "panel-upgrade.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    flag_str = " ".join(flags)
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec >>{log_file} 2>&1\n"
        "echo \"=== panel upgrade $(date -u +%Y-%m-%dT%H:%M:%SZ) ===\"\n"
        f"export MRMPANEL_MIRROR={mirror!r}\n"
        f"export MRMPANEL_ADMIN_PASSWORD=\"$(tr -d '\\n' < {secrets})\"\n"
        "unset MRMPANEL_VERSION\n"
        "sleep 2\n"
        f'curl -fsSL "$MRMPANEL_MIRROR/install.sh" | bash -s -- {flag_str}\n'
        "echo \"=== upgrade finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ===\"\n"
    )
    script.chmod(0o700)
    progress("Launching upgrade outside the panel process (service will restart)…", 70)
    subprocess.Popen(
        ["bash", str(script)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        pass
    progress(
        "Upgrade started — wait about a minute, then refresh. Log: "
        f"{log_file}",
        100,
    )
    return {
        "ok": True,
        "detached": True,
        "backup": backup_path,
        "log": str(log_file),
    }


def start_os_upgrade_job() -> dict[str, Any]:
    job = deploy_jobs.create_job("os_upgrade", {})

    def work(progress: deploy_jobs.ProgressFn) -> dict[str, Any]:
        return run_os_upgrade(progress)

    deploy_jobs.run_in_background(job["id"], work)
    return job


def start_panel_upgrade_job() -> dict[str, Any]:
    job = deploy_jobs.create_job("panel_upgrade", {})

    def work(progress: deploy_jobs.ProgressFn) -> dict[str, Any]:
        return run_panel_upgrade(progress)

    deploy_jobs.run_in_background(job["id"], work)
    return job
