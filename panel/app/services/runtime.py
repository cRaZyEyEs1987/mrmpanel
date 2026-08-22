from __future__ import annotations

import subprocess
from typing import Any

from . import sites

MAIL_CONTAINER = "mrmpanel-mail"
_ACTIONS = frozenset({"start", "stop", "kill"})


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def container_status(name: str) -> dict[str, Any]:
    """Return status for a Docker container by name."""
    try:
        proc = _docker(
            "inspect",
            "--format",
            "{{.State.Status}}|{{.State.Running}}|{{.State.Error}}",
            name,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "name": name,
            "status": "error",
            "running": False,
            "exists": False,
            "detail": str(exc),
        }
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        missing = "No such object" in err or "no such object" in err.lower()
        return {
            "name": name,
            "status": "missing" if missing else "error",
            "running": False,
            "exists": False,
            "detail": err or "Container not found",
        }
    parts = (proc.stdout or "").strip().split("|")
    state = parts[0] if parts else "unknown"
    running = len(parts) > 1 and parts[1].lower() == "true"
    err_msg = parts[2] if len(parts) > 2 else ""
    return {
        "name": name,
        "status": state or "unknown",
        "running": running,
        "exists": True,
        "detail": err_msg or state,
    }


def container_logs_tail(name: str, lines: int = 40) -> str:
    try:
        proc = _docker("logs", "--tail", str(lines), name, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return out.strip()[-2000:]


def _brief_reason(action: str, name: str, proc: subprocess.CompletedProcess[str]) -> str:
    err = (proc.stderr or proc.stdout or "").strip()
    if action == "start":
        logs = container_logs_tail(name)
        bits = [p for p in (err, logs) if p]
        if bits:
            text = "\n".join(bits)
            return text[-800:] if len(text) > 800 else text
        st = container_status(name)
        return st.get("detail") or f"Failed to start {name}"
    return err or f"docker {action} {name} failed (exit {proc.returncode})"


def control_container(name: str, action: str) -> dict[str, Any]:
    action = action.strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"Unsupported action: {action}")
    if action in {"stop", "kill"} and not container_status(name).get("exists"):
        return {
            "ok": False,
            "action": action,
            "name": name,
            "status": "missing",
            "error": f"Container {name} not found",
        }
    try:
        proc = _docker(action, name, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "action": action,
            "name": name,
            "status": "error",
            "error": str(exc),
        }
    if proc.returncode != 0:
        return {
            "ok": False,
            "action": action,
            "name": name,
            "status": container_status(name).get("status"),
            "error": _brief_reason(action, name, proc),
        }
    st = container_status(name)
    if action == "start" and not st.get("running"):
        reason = st.get("detail") or ""
        logs = container_logs_tail(name)
        err = "\n".join(p for p in (reason, logs) if p) or f"{name} did not stay running"
        return {
            "ok": False,
            "action": action,
            "name": name,
            "status": st.get("status"),
            "error": err[-800:],
        }
    return {
        "ok": True,
        "action": action,
        "name": name,
        "status": st.get("status"),
        "error": None,
    }


def mail_service_status() -> dict[str, Any]:
    st = container_status(MAIL_CONTAINER)
    return {
        **st,
        "label": "Mail server",
        "container": MAIL_CONTAINER,
    }


def control_mail(action: str) -> dict[str, Any]:
    return control_container(MAIL_CONTAINER, action)


def _stats_map(names: list[str]) -> dict[str, dict[str, str]]:
    if not names:
        return {}
    try:
        proc = _docker(
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
            *names,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    out: dict[str, dict[str, str]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        out[parts[0]] = {
            "cpu": parts[1].strip(),
            "mem": parts[2].strip(),
            "mem_perc": parts[3].strip(),
        }
    return out


def site_runtime_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    all_sites = sites.list_sites()
    names: list[str] = []
    for site in all_sites:
        cname = str(site.get("container") or "").strip()
        if not cname:
            username = str(site.get("username") or "")
            domain = str(site.get("domain") or "")
            if username and domain:
                cname = sites.container_name_for(username, domain)
        if cname:
            names.append(cname)
        rows.append(
            {
                "id": site.get("id"),
                "domain": site.get("domain"),
                "username": site.get("username"),
                "stack": site.get("stack"),
                "container": cname or None,
                "status": "missing",
                "running": False,
                "cpu": "—",
                "mem": "—",
                "mem_perc": "—",
            }
        )
    stats = _stats_map([n for n in names if n])
    status_cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        cname = row.get("container")
        if not cname:
            continue
        if cname not in status_cache:
            status_cache[cname] = container_status(cname)
        st = status_cache[cname]
        row["status"] = st.get("status") or "unknown"
        row["running"] = bool(st.get("running"))
        if cname in stats:
            row["cpu"] = stats[cname]["cpu"]
            row["mem"] = stats[cname]["mem"]
            row["mem_perc"] = stats[cname]["mem_perc"]
    return rows


def control_site(site_id: str, action: str) -> dict[str, Any]:
    site = sites.get_site(site_id)
    if not site:
        raise ValueError("Site not found")
    cname = str(site.get("container") or "").strip()
    if not cname:
        cname = sites.container_name_for(
            str(site.get("username") or ""),
            str(site.get("domain") or ""),
        )
    result = control_container(cname, action)
    result["site_id"] = site_id
    result["domain"] = site.get("domain")
    return result


def mail_security_gaps() -> list[dict[str, Any]]:
    """Domains missing SPF, DKIM, or DMARC (for admin dashboard warning)."""
    from ..config import load_features
    from . import domains as domains_svc
    from . import mail as mail_svc

    if not load_features().get("mail"):
        return []

    names = [d["domain"] for d in domains_svc.list_domains() if d.get("domain")]
    if not names:
        return []
    gaps: list[dict[str, Any]] = []
    for row in mail_svc.mail_security_audit(names):
        missing: list[str] = []
        if not row["mx"]["valid"]:
            missing.append("MX")
        if not row["spf"]["valid"]:
            missing.append("SPF")
        if not row["dkim"]["valid"]:
            missing.append("DKIM")
        if not row["dmarc"]["valid"]:
            missing.append("DMARC")
        if missing:
            gaps.append({"domain": row["domain"], "missing": missing, "row": row})
    return gaps


# Infrastructure containers (ops dashboard)
INFRA_SERVICES: dict[str, dict[str, Any]] = {
    "traefik": {
        "label": "Traefik",
        "feature": "web",
        "candidates": ("mrmpanel-traefik-1", "compose-traefik-1", "traefik"),
        "warning": "Stopping Traefik drops HTTPS and site routing.",
    },
    "mariadb": {
        "label": "MariaDB",
        "feature": "mariadb",
        "candidates": ("mrmpanel-mariadb",),
        "warning": "Stopping MariaDB breaks sites that use it.",
    },
    "postgres": {
        "label": "PostgreSQL",
        "feature": "postgres",
        "candidates": ("mrmpanel-postgres",),
        "warning": "Stopping PostgreSQL breaks sites that use it.",
    },
    "pdns": {
        "label": "PowerDNS",
        "feature": "dns",
        "candidates": ("mrmpanel-pdns",),
        "warning": "Stopping PowerDNS breaks authoritative DNS for hosted zones.",
    },
}


def resolve_container_name(candidates: tuple[str, ...] | list[str]) -> str | None:
    for name in candidates:
        st = container_status(name)
        if st.get("exists"):
            return name
    # Prefer first candidate even if missing (for start after remove)
    return candidates[0] if candidates else None


def infra_service_rows() -> list[dict[str, Any]]:
    from ..config import load_features

    features = load_features()
    rows: list[dict[str, Any]] = []
    names: list[str] = []
    for key, meta in INFRA_SERVICES.items():
        if not features.get(meta["feature"]):
            continue
        cname = resolve_container_name(meta["candidates"]) or meta["candidates"][0]
        names.append(cname)
        st = container_status(cname)
        rows.append(
            {
                "id": key,
                "label": meta["label"],
                "warning": meta["warning"],
                "container": cname,
                "status": st.get("status") or "missing",
                "running": bool(st.get("running")),
                "detail": st.get("detail") or "",
                "cpu": "—",
                "mem": "—",
                "mem_perc": "—",
            }
        )
    stats = _stats_map(names)
    for row in rows:
        cname = row["container"]
        if cname in stats:
            row["cpu"] = stats[cname]["cpu"]
            row["mem"] = stats[cname]["mem"]
            row["mem_perc"] = stats[cname]["mem_perc"]
    return rows


def control_infra(service_id: str, action: str) -> dict[str, Any]:
    from ..config import load_features

    meta = INFRA_SERVICES.get(service_id)
    if not meta:
        raise ValueError(f"Unknown infrastructure service: {service_id}")
    if not load_features().get(meta["feature"]):
        raise ValueError(f"{meta['label']} is not enabled on this server")
    cname = resolve_container_name(meta["candidates"]) or meta["candidates"][0]
    result = control_container(cname, action)
    result["service"] = service_id
    result["label"] = meta["label"]
    return result
