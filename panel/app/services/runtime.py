from __future__ import annotations

import os
import subprocess
import time
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


# Infrastructure services (ops dashboard)
INFRA_SERVICES: dict[str, dict[str, Any]] = {
    "traefik": {
        "label": "Traefik",
        "kind": "docker",
        "feature": "web",
        "candidates": ("mrmpanel-traefik-1", "compose-traefik-1", "traefik"),
        "warning": "Stopping Traefik drops HTTPS and site routing.",
    },
    "mariadb": {
        "label": "MariaDB",
        "kind": "docker",
        "feature": "mariadb",
        "candidates": ("mrmpanel-mariadb",),
        "warning": "Stopping MariaDB breaks sites that use it.",
    },
    "postgres": {
        "label": "PostgreSQL",
        "kind": "docker",
        "feature": "postgres",
        "candidates": ("mrmpanel-postgres",),
        "warning": "Stopping PostgreSQL breaks sites that use it.",
    },
    "pdns": {
        "label": "PowerDNS",
        "kind": "docker",
        "feature": "dns",
        "candidates": ("mrmpanel-pdns",),
        "warning": "Stopping PowerDNS breaks authoritative DNS for hosted zones.",
    },
    "fail2ban": {
        "label": "Fail2ban",
        "kind": "systemd",
        "feature": None,
        "unit": "fail2ban",
        "warning": "Stopping Fail2ban pauses SSH/brute-force bans until restarted.",
    },
}

_SYSTEMD_ACTIONS = frozenset({"start", "stop", "restart", "kill"})


def resolve_container_name(candidates: tuple[str, ...] | list[str]) -> str | None:
    for name in candidates:
        st = container_status(name)
        if st.get("exists"):
            return name
    # Prefer first candidate even if missing (for start after remove)
    return candidates[0] if candidates else None


def _host_mem_total_bytes() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _format_iec_bytes(num: int) -> str:
    n = float(max(0, num))
    for unit, div in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= div or unit == "KiB":
            val = n / div
            if val >= 10:
                return f"{val:.1f}{unit}"
            return f"{val:.2f}{unit}"
    return f"{int(n)}B"


def _cgroup_path(control_group: str) -> str | None:
    cg = (control_group or "").strip()
    if not cg or cg == "/":
        return None
    # Unified hierarchy (EL9+): /sys/fs/cgroup + ControlGroup
    path = f"/sys/fs/cgroup{cg}"
    try:
        if os.path.isdir(path):
            return path
    except OSError:
        pass
    return None


def _read_cgroup_usage_usec(cg_path: str) -> int | None:
    try:
        with open(f"{cg_path}/cpu.stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_cgroup_memory_current(cg_path: str) -> int | None:
    try:
        with open(f"{cg_path}/memory.current", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _pid_rss_bytes(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _ps_cpu_percent(pid: int) -> float | None:
    try:
        proc = subprocess.run(
            ["ps", "-o", "pcpu=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return None


def systemd_unit_stats(unit: str) -> dict[str, str]:
    """CPU/memory snapshot for a host systemd unit (cgroup preferred)."""
    empty = {"cpu": "—", "mem": "—", "mem_perc": "—"}
    unit = (unit or "").strip()
    if not unit:
        return empty
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "--property=MainPID,MemoryCurrent,ControlGroup,ActiveState",
                "--no-page",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return empty
    props: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    if props.get("ActiveState") != "active":
        return empty
    try:
        main_pid = int(props.get("MainPID") or "0")
    except ValueError:
        main_pid = 0

    cg_path = _cgroup_path(props.get("ControlGroup") or "")
    mem_bytes: int | None = None
    try:
        mc = int(props.get("MemoryCurrent") or "")
        # systemd uses very large sentinel when accounting is unavailable
        if 0 < mc < (1 << 62):
            mem_bytes = mc
    except ValueError:
        pass
    if mem_bytes is None and cg_path:
        mem_bytes = _read_cgroup_memory_current(cg_path)
    if mem_bytes is None and main_pid > 0:
        mem_bytes = _pid_rss_bytes(main_pid)
    if mem_bytes is None:
        return empty

    host_total = _host_mem_total_bytes()
    if host_total and host_total > 0:
        mem_str = f"{_format_iec_bytes(mem_bytes)} / {_format_iec_bytes(host_total)}"
        mem_perc = f"{(100.0 * mem_bytes / host_total):.2f}%"
    else:
        mem_str = _format_iec_bytes(mem_bytes)
        mem_perc = "—"

    cpu_str = "—"
    if cg_path:
        u0 = _read_cgroup_usage_usec(cg_path)
        t0 = time.monotonic()
        if u0 is not None:
            time.sleep(0.15)
            u1 = _read_cgroup_usage_usec(cg_path)
            t1 = time.monotonic()
            if u1 is not None and t1 > t0:
                delta_usec = max(0, u1 - u0)
                elapsed_usec = (t1 - t0) * 1_000_000.0
                # Percent of one CPU (matches docker stats style for low usage)
                pct = 100.0 * delta_usec / elapsed_usec
                cpu_str = f"{pct:.2f}%"
    if cpu_str == "—" and main_pid > 0:
        pcpu = _ps_cpu_percent(main_pid)
        if pcpu is not None:
            cpu_str = f"{pcpu:.2f}%"

    return {"cpu": cpu_str, "mem": mem_str, "mem_perc": mem_perc}


def systemd_unit_status(unit: str) -> dict[str, Any]:
    """Return status for a host systemd unit (e.g. fail2ban)."""
    unit = (unit or "").strip()
    if not unit:
        return {
            "name": unit,
            "status": "missing",
            "running": False,
            "exists": False,
            "detail": "No unit name",
        }
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "--property=LoadState,ActiveState,SubState,UnitFileState",
                "--no-page",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "name": unit,
            "status": "error",
            "running": False,
            "exists": False,
            "detail": str(exc),
        }
    props: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    load = props.get("LoadState", "")
    active = props.get("ActiveState", "")
    sub = props.get("SubState", "")
    exists = load == "loaded"
    running = active == "active"
    if not exists:
        status = "missing"
        detail = "Not installed (package fail2ban)"
    else:
        status = active or "unknown"
        detail = f"{active}/{sub}" if sub else active
    return {
        "name": unit,
        "status": status,
        "running": running,
        "exists": exists,
        "detail": detail,
    }


def control_systemd(unit: str, action: str) -> dict[str, Any]:
    action = action.strip().lower()
    if action not in _SYSTEMD_ACTIONS:
        raise ValueError(f"Unsupported action: {action}")
    # Map kill → kill for systemd (SIGKILL), restart is preferred in UI
    sys_action = action
    st = systemd_unit_status(unit)
    if not st.get("exists") and action != "start":
        return {
            "ok": False,
            "action": action,
            "name": unit,
            "status": "missing",
            "error": f"Unit {unit} is not installed",
        }
    if action == "start" and not st.get("exists"):
        return {
            "ok": False,
            "action": action,
            "name": unit,
            "status": "missing",
            "error": "Fail2ban is not installed. Re-run the panel installer or: dnf/apt install fail2ban",
        }
    try:
        if action == "start":
            proc = subprocess.run(
                ["systemctl", "enable", "--now", unit],
                capture_output=True,
                text=True,
                timeout=60,
            )
        else:
            proc = subprocess.run(
                ["systemctl", sys_action, unit],
                capture_output=True,
                text=True,
                timeout=60,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "action": action,
            "name": unit,
            "status": "error",
            "error": str(exc),
        }
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return {
            "ok": False,
            "action": action,
            "name": unit,
            "status": systemd_unit_status(unit).get("status"),
            "error": err or f"systemctl {action} {unit} failed",
        }
    st2 = systemd_unit_status(unit)
    return {
        "ok": True,
        "action": action,
        "name": unit,
        "status": st2.get("status"),
        "error": None,
    }


def infra_service_rows() -> list[dict[str, Any]]:
    from ..config import load_features

    features = load_features()
    rows: list[dict[str, Any]] = []
    docker_names: list[str] = []
    for key, meta in INFRA_SERVICES.items():
        feature = meta.get("feature")
        if feature and not features.get(feature):
            continue
        kind = meta.get("kind") or "docker"
        if kind == "systemd":
            unit = str(meta.get("unit") or key)
            st = systemd_unit_status(unit)
            usage = systemd_unit_stats(unit) if st.get("running") else {
                "cpu": "—",
                "mem": "—",
                "mem_perc": "—",
            }
            rows.append(
                {
                    "id": key,
                    "label": meta["label"],
                    "kind": "systemd",
                    "warning": meta["warning"],
                    "container": unit,
                    "status": st.get("status") or "missing",
                    "running": bool(st.get("running")),
                    "detail": st.get("detail") or "",
                    "cpu": usage["cpu"],
                    "mem": usage["mem"],
                    "mem_perc": usage["mem_perc"],
                    "kill_label": "Restart",
                    "kill_action": "restart",
                }
            )
            continue

        cname = resolve_container_name(meta["candidates"]) or meta["candidates"][0]
        docker_names.append(cname)
        st = container_status(cname)
        rows.append(
            {
                "id": key,
                "label": meta["label"],
                "kind": "docker",
                "warning": meta["warning"],
                "container": cname,
                "status": st.get("status") or "missing",
                "running": bool(st.get("running")),
                "detail": st.get("detail") or "",
                "cpu": "—",
                "mem": "—",
                "mem_perc": "—",
                "kill_label": "Kill",
                "kill_action": "kill",
            }
        )
    stats = _stats_map(docker_names)
    for row in rows:
        if row.get("kind") != "docker":
            continue
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
    feature = meta.get("feature")
    if feature and not load_features().get(feature):
        raise ValueError(f"{meta['label']} is not enabled on this server")
    kind = meta.get("kind") or "docker"
    if kind == "systemd":
        unit = str(meta.get("unit") or service_id)
        result = control_systemd(unit, action)
        result["service"] = service_id
        result["label"] = meta["label"]
        return result
    cname = resolve_container_name(meta["candidates"]) or meta["candidates"][0]
    result = control_container(cname, action)
    result["service"] = service_id
    result["label"] = meta["label"]
    return result
