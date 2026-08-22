"""PowerDNS authoritative DNS client for mrmpanel."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import get_settings, load_features, save_features

PDNS_API = "http://127.0.0.1:8081/api/v1"
IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)$"
)
# Multi-label public suffixes (not a full PSL — covers common hosting TLDs).
MULTI_PART_SUFFIXES = frozenset(
    {
        "co.za",
        "org.za",
        "net.za",
        "web.za",
        "gov.za",
        "ac.za",
        "alt.za",
        "co.uk",
        "org.uk",
        "me.uk",
        "net.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "org.nz",
        "net.nz",
    }
)


def _api_key() -> str:
    path = get_settings().data_dir / "secrets" / "pdns_api_key"
    if path.is_file():
        return path.read_text().strip()
    return ""


def dns_enabled(features: dict[str, Any] | None = None) -> bool:
    features = features or load_features()
    return bool(features.get("dns"))


def is_public_suffix(name: str) -> bool:
    """True if name is a TLD / multi-part public suffix (e.g. com, co.za)."""
    name = (name or "").strip().rstrip(".").lower()
    if not name:
        return True
    if name in MULTI_PART_SUFFIXES:
        return True
    return "." not in name


def registry_parent_zone(domain: str) -> str:
    """Parent zone that delegates this apex (example.com → com, example.co.za → co.za)."""
    domain = (domain or "").strip().rstrip(".").lower()
    if not domain or "." not in domain:
        return domain
    parts = domain.split(".")
    if len(parts) >= 2:
        two = ".".join(parts[-2:])
        if two in MULTI_PART_SUFFIXES:
            return two
    return parts[-1]


def _ns_hostnames_from_dig(result: dict[str, Any]) -> set[str]:
    """Collect NS target hostnames from dig answer/authority lines."""
    found: set[str] = set()
    for ln in result.get("answers") or []:
        parts = ln.split()
        if len(parts) >= 5 and parts[3].upper() == "NS":
            found.add(parts[4].rstrip(".").lower())
    for ln in (result.get("raw") or "").splitlines():
        if ln.startswith(";") or not ln.strip():
            continue
        parts = ln.split()
        if len(parts) >= 5 and parts[3].upper() == "NS":
            found.add(parts[4].rstrip(".").lower())
    return found


def lookup_parent_nameservers(suffix: str) -> list[str]:
    """NS hostnames for a TLD / public suffix via a public resolver."""
    suffix = (suffix or "").strip().rstrip(".").lower()
    if not suffix:
        return []
    q = _dig("NS", suffix, server="1.1.1.1", norecurse=False)
    names = sorted(_ns_hostnames_from_dig(q))
    # Known fallbacks when public NS lookup fails (offline resolver, etc.)
    if not names and suffix in ("co.za", "org.za", "net.za", "web.za"):
        return ["coza1.dnsnode.net", "coza2.dnsnode.net"]
    return names


def parent_domain(hostname: str) -> str:
    """Strip one left label, but never peel into a public suffix.

    server.example.com → example.com
    server.example.co.za → example.co.za
    example.co.za → example.co.za  (not co.za)
    example.com → example.com
    """
    host = (hostname or "").strip().rstrip(".").lower()
    if not host or "." not in host:
        return host
    parent = host.split(".", 1)[1]
    if is_public_suffix(parent):
        return host
    return parent


def derive_ns_from_hostname(hostname: str) -> dict[str, str]:
    """server.example.com → base example.com, ns1/ns2.example.com."""
    host = (hostname or "").strip().rstrip(".").lower()
    if not host:
        base = "example.com"
    elif "." not in host:
        base = host
    else:
        base = parent_domain(host)
    if not base or is_public_suffix(base):
        base = host or "example.com"
    return {
        "ns_base_domain": base,
        "ns1_hostname": f"ns1.{base}",
        "ns2_hostname": f"ns2.{base}",
    }


def _fqdn(name: str) -> str:
    name = name.strip().rstrip(".")
    return f"{name}." if name else "."


def _headers() -> dict[str, str]:
    key = _api_key()
    if not key:
        raise RuntimeError("PowerDNS API key missing (secrets/pdns_api_key)")
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _client() -> httpx.Client:
    return httpx.Client(base_url=PDNS_API, headers=_headers(), timeout=15.0)


def zone_exists(zone: str) -> bool:
    zname = _fqdn(zone)
    with _client() as c:
        r = c.get(f"/servers/localhost/zones/{zname}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


def _rrset(
    name: str,
    rtype: str,
    records: list[str],
    ttl: int = 3600,
    changetype: str = "REPLACE",
) -> dict[str, Any]:
    return {
        "name": _fqdn(name),
        "type": rtype,
        "ttl": ttl,
        "changetype": changetype,
        "records": [{"content": content, "disabled": False} for content in records],
    }


def _serial_after(current: int) -> int:
    """Next YYYYMMDDnn serial that is both higher than *current* and not future.

    The date part trails UTC by 12 hours so it is never ahead of the calendar
    date in any timezone; validators such as MxToolbox compare the serial date
    against their own clock and warn when it looks like a future date.
    """
    stamp = datetime.now(timezone.utc) - timedelta(hours=12)
    day_base = int(stamp.strftime("%Y%m%d")) * 100
    if current >= day_base + 1:
        return current + 1
    return day_base + 1


def _bump_soa_content(content: str) -> str:
    """Return *content* with its serial advanced, or "" if unparseable."""
    parts = content.split()
    if len(parts) < 7:
        return ""
    try:
        current = int(parts[2])
    except ValueError:
        return ""
    parts[2] = str(_serial_after(current))
    return " ".join(parts)


def _next_soa_serial(zone: str) -> int:
    """Return a monotonic YYYYMMDDnn serial for a managed zone."""
    current = 0
    try:
        records = zone_records(zone)
        soa_values = records.get(zone.rstrip(".").lower(), {}).get("SOA", [])
        if soa_values:
            parts = soa_values[0].split()
            if len(parts) >= 3:
                current = int(parts[2])
    except (ValueError, TypeError, RuntimeError, httpx.HTTPError):
        current = 0
    return _serial_after(current)


def _soa_content(zone: str, primary_ns: str) -> str:
    serial = _next_soa_serial(zone)
    # refresh 3h, retry 1h, expire 14d, negative TTL 1h
    return (
        f"{_fqdn(primary_ns)} hostmaster.{_fqdn(zone)} "
        f"{serial} 10800 3600 1209600 3600"
    )


def _zone_soa_content(zone_data: dict[str, Any], zname: str) -> str:
    for rrset in zone_data.get("rrsets", []):
        if rrset.get("type") == "SOA" and rrset.get("name") == zname:
            records = rrset.get("records") or []
            if records:
                return records[0].get("content", "")
    return ""


def create_or_replace_zone(zone: str, rrsets: list[dict[str, Any]]) -> None:
    zname = _fqdn(zone)
    with _client() as c:
        existing = c.get(f"/servers/localhost/zones/{zname}")
        if existing.status_code == 404:
            # POST create: omit changetype (PATCH-only field)
            create_sets = []
            for rr in rrsets:
                item = {k: v for k, v in rr.items() if k != "changetype"}
                create_sets.append(item)
            payload = {
                "name": zname,
                "kind": "Native",
                "nameservers": [],
                "rrsets": create_sets,
                # Serials are managed by _next_soa_serial; PowerDNS would
                # otherwise rewrite them from its own UTC clock.
                "soa_edit": "",
                "soa_edit_api": "",
            }
            r = c.post("/servers/localhost/zones", json=payload)
            r.raise_for_status()
            return
        existing.raise_for_status()
        zone_data = existing.json()
        if zone_data.get("soa_edit_api") or zone_data.get("soa_edit"):
            c.put(
                f"/servers/localhost/zones/{zname}",
                json={"soa_edit": "", "soa_edit_api": ""},
            )
        patch_sets = list(rrsets)
        if not any(rr.get("type") == "SOA" for rr in patch_sets):
            bumped = _bump_soa_content(_zone_soa_content(zone_data, zname))
            if bumped:
                patch_sets.append(_rrset(zone, "SOA", [bumped]))
        r = c.patch(f"/servers/localhost/zones/{zname}", json={"rrsets": patch_sets})
        r.raise_for_status()


def ensure_server_zone(features: dict[str, Any] | None = None) -> None:
    """Create/update the NS base zone (glue for ns1/ns2 + server A)."""
    features = features or load_features()
    if not dns_enabled(features):
        return
    base = features.get("ns_base_domain") or ""
    ns1 = features.get("ns1_hostname") or f"ns1.{base}"
    ns2 = features.get("ns2_hostname") or f"ns2.{base}"
    ns1_ip = features.get("ns1_ip") or features.get("public_ip") or ""
    ns2_ip = features.get("ns2_ip") or features.get("public_ip") or ""
    hostname = features.get("hostname") or ""
    public_ip = features.get("public_ip") or ns1_ip
    if not base or not ns1_ip or not ns2_ip:
        raise RuntimeError("DNS base domain / NS IPs not configured in features.json")

    soa = _soa_content(base, ns1)
    rrsets = [
        _rrset(base, "SOA", [soa]),
        _rrset(base, "NS", [_fqdn(ns1), _fqdn(ns2)]),
        _rrset(ns1, "A", [ns1_ip]),
        _rrset(ns2, "A", [ns2_ip]),
    ]
    # Apex A is required so the domain itself resolves after NS cutover (.co.za
    # registrars also often probe the zone beyond SOA/NS).
    if public_ip:
        rrsets.append(_rrset(base, "A", [public_ip]))
        rrsets.append(_rrset(f"www.{base}", "A", [public_ip]))
    if (
        hostname
        and public_ip
        and hostname != base
        and hostname != f"www.{base}"
        and hostname.endswith(f".{base}")
        and hostname not in (ns1, ns2)
    ):
        rrsets.append(_rrset(hostname, "A", [public_ip]))

    create_or_replace_zone(base, rrsets)


def ensure_domain_zone(domain: str, features: dict[str, Any] | None = None) -> None:
    """Ensure a hosted domain zone with NS → panel ns1/ns2 and A → public IP."""
    features = features or load_features()
    if not dns_enabled(features):
        return
    domain = domain.strip().rstrip(".").lower()
    if not domain:
        raise ValueError("empty domain")

    base = features.get("ns_base_domain") or ""
    ns1 = features.get("ns1_hostname") or f"ns1.{base}"
    ns2 = features.get("ns2_hostname") or f"ns2.{base}"
    public_ip = features.get("public_ip") or features.get("ns1_ip") or ""
    hostname = features.get("hostname") or ns1
    if not ns1 or not ns2 or not public_ip:
        raise RuntimeError("NS hostnames / public IP not configured")

    # Server base zone is managed separately (glue lives there)
    if domain == base:
        ensure_server_zone(features)
        return

    soa = _soa_content(domain, ns1)
    rrsets = [
        _rrset(domain, "SOA", [soa]),
        _rrset(domain, "NS", [_fqdn(ns1), _fqdn(ns2)]),
        _rrset(domain, "A", [public_ip]),
        _rrset(f"www.{domain}", "A", [public_ip]),
    ]
    if features.get("mail"):
        rrsets.append(_rrset(domain, "MX", [f"10 {_fqdn(hostname)}"]))
        rrsets.append(
            _rrset(
                domain,
                "TXT",
                [f'"v=spf1 mx a:{hostname} ip4:{public_ip} ~all"'],
            )
        )
    create_or_replace_zone(domain, rrsets)


def ensure_host_record(
    zone: str,
    hostname: str,
    features: dict[str, Any] | None = None,
) -> None:
    """Add an A record for a site hostname inside an existing managed zone."""
    features = features or load_features()
    if not dns_enabled(features):
        return
    zone = zone.strip().rstrip(".").lower()
    hostname = hostname.strip().rstrip(".").lower()
    if hostname != zone and not hostname.endswith(f".{zone}"):
        raise ValueError(f"{hostname} is not inside DNS zone {zone}")
    public_ip = features.get("public_ip") or features.get("ns1_ip") or ""
    if not public_ip:
        raise RuntimeError("Public IP is not configured")
    if not zone_exists(zone):
        ensure_domain_zone(zone, features)
    create_or_replace_zone(zone, [_rrset(hostname, "A", [public_ip])])


def zone_records(zone: str) -> dict[str, dict[str, list[str]]]:
    """Return {name: {rtype: [content, ...]}} for a PowerDNS zone.

    TXT contents are returned dequoted / concatenated so callers can treat them
    as plain policy strings (v=spf1…, v=DKIM1…).
    """
    zone = zone.strip().rstrip(".").lower()
    if not zone_exists(zone):
        return {}
    zname = _fqdn(zone)
    out: dict[str, dict[str, list[str]]] = {}
    with _client() as client:
        response = client.get(f"/servers/localhost/zones/{zname}")
        response.raise_for_status()
        for rrset in response.json().get("rrsets", []):
            name = str(rrset.get("name") or "").rstrip(".").lower()
            rtype = str(rrset.get("type") or "").upper()
            if not name or not rtype:
                continue
            values: list[str] = []
            for record in rrset.get("records") or []:
                if record.get("disabled"):
                    continue
                content = str(record.get("content") or "").strip()
                if not content:
                    continue
                if rtype == "TXT":
                    chunks = re.findall(r'"([^"]*)"', content)
                    content = "".join(chunks) if chunks else content.strip('"')
                values.append(content)
            if not values:
                continue
            out.setdefault(name, {})[rtype] = values
    return out


def zone_record_rows(zone: str) -> list[dict[str, Any]]:
    """Return every enabled PowerDNS record with its name, type, TTL, and value."""
    zone = zone.strip().rstrip(".").lower()
    if not zone_exists(zone):
        return []
    zname = _fqdn(zone)
    rows: list[dict[str, Any]] = []
    with _client() as client:
        response = client.get(f"/servers/localhost/zones/{zname}")
        response.raise_for_status()
        for rrset in response.json().get("rrsets", []):
            name = str(rrset.get("name") or "").rstrip(".").lower()
            rtype = str(rrset.get("type") or "").upper()
            ttl = int(rrset.get("ttl") or 0)
            if not name or not rtype:
                continue
            for record in rrset.get("records") or []:
                if record.get("disabled"):
                    continue
                content = str(record.get("content") or "").strip()
                if not content:
                    continue
                if rtype == "TXT":
                    chunks = re.findall(r'"([^"]*)"', content)
                    content = "".join(chunks) if chunks else content.strip('"')
                rows.append(
                    {
                        "name": name,
                        "type": rtype,
                        "ttl": ttl,
                        "value": content,
                    }
                )
    return sorted(rows, key=lambda row: (row["name"], row["type"], row["value"]))


def upsert_txt_record(
    zone: str,
    name: str,
    value: str,
    record_prefix: str,
) -> None:
    """Replace one policy TXT value while preserving unrelated TXT records."""
    zone = zone.strip().rstrip(".").lower()
    name = name.strip().rstrip(".").lower()
    if not zone_exists(zone):
        raise ValueError(f"{zone} is not hosted by this PowerDNS server")
    zname = _fqdn(zone)
    fqdn = _fqdn(name)
    existing_values: list[str] = []
    with _client() as client:
        response = client.get(f"/servers/localhost/zones/{zname}")
        response.raise_for_status()
        for rrset in response.json().get("rrsets", []):
            if rrset.get("name") == fqdn and rrset.get("type") == "TXT":
                existing_values = [
                    record.get("content", "")
                    for record in rrset.get("records", [])
                    if not record.get("disabled")
                ]
                break

    marker = record_prefix.lower()
    kept = [
        content
        for content in existing_values
        if not content.strip('"').lower().startswith(marker)
    ]
    quoted = value if value.startswith('"') else f'"{value}"'
    kept.append(quoted)
    create_or_replace_zone(zone, [_rrset(name, "TXT", kept)])


def ensure_mx_record(
    zone: str,
    mail_hostname: str,
    priority: int = 10,
) -> None:
    """Publish the managed server as MX for a locally hosted zone."""
    zone = zone.strip().rstrip(".").lower()
    mail_hostname = mail_hostname.strip().rstrip(".").lower()
    if not zone_exists(zone):
        raise ValueError(f"{zone} is not hosted by this PowerDNS server")
    create_or_replace_zone(
        zone,
        [_rrset(zone, "MX", [f"{int(priority)} {_fqdn(mail_hostname)}"])],
    )


def update_ns_settings(
    *,
    ns1_hostname: str | None = None,
    ns2_hostname: str | None = None,
    ns1_ip: str | None = None,
    ns2_ip: str | None = None,
) -> dict[str, Any]:
    """Persist NS settings and refresh the server base zone in PowerDNS."""
    features = load_features()
    if ns1_hostname is not None:
        features["ns1_hostname"] = ns1_hostname.strip().rstrip(".").lower()
    if ns2_hostname is not None:
        features["ns2_hostname"] = ns2_hostname.strip().rstrip(".").lower()
    if ns1_ip is not None:
        if not IPV4_RE.match(ns1_ip.strip()):
            raise ValueError(f"Invalid ns1 IP: {ns1_ip}")
        features["ns1_ip"] = ns1_ip.strip()
    if ns2_ip is not None:
        if not IPV4_RE.match(ns2_ip.strip()):
            raise ValueError(f"Invalid ns2 IP: {ns2_ip}")
        features["ns2_ip"] = ns2_ip.strip()

    # Keep base domain aligned with ns1 hostname when possible
    ns1 = features.get("ns1_hostname") or ""
    if ns1 and "." in ns1:
        base = parent_domain(ns1)
        if base and not is_public_suffix(base):
            features["ns_base_domain"] = base

    save_features(features)
    if dns_enabled(features):
        ensure_server_zone(features)
    return features


def pdns_reachable() -> bool:
    try:
        with _client() as c:
            r = c.get("/servers/localhost")
            return r.status_code == 200
    except Exception:
        return False


def _dig(
    *args: str,
    server: str | None = None,
    timeout: int = 3,
    norecurse: bool = False,
) -> dict[str, Any]:
    """Run dig; return {ok, status, flags, answers, aa, raw, error}."""
    import subprocess

    cmd = [
        "dig",
        f"+time={timeout}",
        "+tries=1",
        "+nocookie",
        "+noall",
        "+answer",
        "+authority",
        "+additional",
        "+comments",
    ]
    if norecurse:
        cmd.append("+norecurse")
    if server:
        cmd.append(f"@{server}")
    cmd.extend(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
    except FileNotFoundError:
        return {"ok": False, "error": "dig not installed", "raw": "", "status": "", "flags": "", "aa": False, "answers": []}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "dig timed out", "raw": "", "status": "", "flags": "", "aa": False, "answers": []}
    raw = (proc.stdout or "") + (proc.stderr or "")
    status = ""
    flags = ""
    for line in raw.splitlines():
        if "status:" in line and "HEADER" in line:
            for part in line.split(","):
                part = part.strip()
                if part.startswith("status:"):
                    status = part.split(":", 1)[1].strip()
        # Only the header flags line (not EDNS "flags:;")
        if line.lstrip().startswith(";; flags:"):
            idx = line.find("flags:")
            flags = line[idx + 6 :].split(";", 1)[0].strip()
        elif "status:" in line and not status:
            for part in line.split(","):
                part = part.strip()
                if part.startswith("status:"):
                    status = part.split(":", 1)[1].strip()
    if not status:
        for line in raw.splitlines():
            if "status:" in line:
                for part in line.split(","):
                    part = part.strip()
                    if part.startswith("status:"):
                        status = part.split(":", 1)[1].strip()
                        break
            if status:
                break
    rr_lines = []
    for ln in raw.splitlines():
        if ln.startswith(";") or not ln.strip():
            continue
        if " IN " in ln or "\tIN\t" in ln:
            rr_lines.append(ln.strip())
    return {
        "ok": proc.returncode == 0 and bool(status),
        "status": status or "unknown",
        "flags": flags,
        "aa": "aa" in flags.split(),
        "answers": rr_lines,
        "raw": raw.strip(),
        "error": None if proc.returncode == 0 else (proc.stderr or "dig failed").strip(),
    }


def nameserver_pointing_status(domain: str) -> dict[str, Any]:
    """Lightweight public check: does this domain's NS match panel ns1/ns2?

    One recursive dig against 1.1.1.1 — suitable for Domains list rows.
    Full registry/server checklist remains in diagnose_ns_acceptance().
    """
    features = load_features()
    domain = (domain or "").strip().rstrip(".").lower()
    ns1 = (features.get("ns1_hostname") or "").strip().rstrip(".").lower()
    ns2 = (features.get("ns2_hostname") or "").strip().rstrip(".").lower()
    expected = {name for name in (ns1, ns2) if name}

    if not domain:
        return {
            "status": "unknown",
            "label": "—",
            "detail": "No domain",
            "ok": False,
            "found": [],
            "expected": sorted(expected),
        }
    if not expected:
        return {
            "status": "unconfigured",
            "label": "n/a",
            "detail": "Set ns1/ns2 in Settings first",
            "ok": False,
            "found": [],
            "expected": [],
        }

    pub = _dig("NS", domain, server="1.1.1.1", norecurse=False)
    found = sorted(_ns_hostnames_from_dig(pub))
    matched = expected & set(found)

    if matched == expected:
        status, label, ok = "pointed", "pointed", True
        detail = f"Public NS match panel: {', '.join(found)}"
    elif matched:
        status, label, ok = "partial", "partial", False
        detail = (
            f"Has {', '.join(sorted(matched))} but not all panel NS. "
            f"Public: {', '.join(found) or '—'}"
        )
    elif found:
        status, label, ok = "other", "other NS", False
        detail = f"Public NS: {', '.join(found)} (want {ns1} / {ns2})"
    elif pub.get("error"):
        status, label, ok = "unknown", "unknown", False
        detail = str(pub.get("error") or "lookup failed")
    else:
        status, label, ok = "none", "not pointed", False
        detail = (
            f"No public NS yet (status={pub.get('status') or '—'}). "
            f"Want {ns1} / {ns2}"
        )

    return {
        "status": status,
        "label": label,
        "detail": detail,
        "ok": ok,
        "found": found,
        "expected": sorted(expected),
        "ns1": ns1,
        "ns2": ns2,
    }


def attach_nameserver_status(
    domain_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return domain rows with a public ns pointing check attached as ``ns``."""
    return [
        {**item, "ns": nameserver_pointing_status(str(item.get("domain") or ""))}
        for item in domain_list
    ]


def diagnose_ns_acceptance(domain: str | None = None) -> dict[str, Any]:
    """Nameserver acceptance checklist (any TLD).

    Server-side: PowerDNS zone + authoritative SOA/NS/glue on ns IPs.
    Parent: NS published by the registry parent zone (com, co.za, …).
    """
    features = load_features()
    domain = (domain or features.get("ns_base_domain") or "").strip().rstrip(".").lower()
    ns1 = (features.get("ns1_hostname") or "").strip().rstrip(".").lower()
    ns2 = (features.get("ns2_hostname") or "").strip().rstrip(".").lower()
    ns1_ip = (features.get("ns1_ip") or features.get("public_ip") or "").strip()
    ns2_ip = (features.get("ns2_ip") or features.get("public_ip") or "").strip()
    public_ip = (features.get("public_ip") or ns1_ip or "").strip()
    parent_zone = registry_parent_zone(domain)

    report: dict[str, Any] = {
        "domain": domain,
        "ns1": ns1,
        "ns2": ns2,
        "ns1_ip": ns1_ip,
        "ns2_ip": ns2_ip,
        "parent_zone": parent_zone,
        "checks": [],
        "summary": "",
        "next_steps": [],
    }

    def add(name: str, passed: bool, detail: str, critical: bool = True) -> None:
        report["checks"].append(
            {"name": name, "pass": passed, "detail": detail, "critical": critical}
        )

    if not features.get("dns"):
        add("dns_enabled", False, "DNS feature is off in features.json")
        report["summary"] = "DNS module disabled."
        return report
    if not domain or not ns1 or not ns2 or not ns1_ip:
        add("config", False, "Missing ns_base_domain / ns hostnames / IPs in features.json")
        report["summary"] = "Incomplete DNS config."
        return report

    add("pdns_api", pdns_reachable(), "PowerDNS API on 127.0.0.1:8081")
    try:
        add("zone_exists", zone_exists(domain), f"PowerDNS has zone {domain}.")
    except Exception as exc:  # noqa: BLE001
        add("zone_exists", False, f"Zone lookup failed: {exc}")

    # Local authoritative checks (strict registries probe glue IPs the same way)
    for label, ip in (("ns1", ns1_ip), ("ns2", ns2_ip)):
        soa = _dig("SOA", domain, server=ip, norecurse=True)
        aa_ok = bool(soa.get("aa") and soa.get("status") == "NOERROR")
        add(
            f"auth_soa_{label}",
            aa_ok,
            f"SOA @{ip}: status={soa.get('status')} flags={soa.get('flags') or '—'} "
            f"{'(AA OK)' if aa_ok else '(need AA + NOERROR — registries reject without this)'}"
            + (f" err={soa.get('error')}" if soa.get("error") else ""),
        )
        ns_q = _dig("NS", domain, server=ip, norecurse=True)
        ns_answers = " ".join(ns_q.get("answers") or []).lower()
        has_ns1 = ns1 in ns_answers
        has_ns2 = ns2 in ns_answers
        add(
            f"auth_ns_{label}",
            bool(ns_q.get("aa") and has_ns1 and has_ns2),
            f"NS @{ip}: aa={ns_q.get('aa')} has {ns1}={has_ns1} has {ns2}={has_ns2}",
        )
        for host in (ns1, ns2):
            a_q = _dig("A", host, server=ip, norecurse=True)
            contents = " ".join(a_q.get("answers") or [])
            expect = ns1_ip if host == ns1 else ns2_ip
            ok_a = bool(a_q.get("aa") and expect in contents and a_q.get("status") == "NOERROR")
            add(
                f"glue_a_{label}_{host.split('.')[0]}",
                ok_a,
                f"A {host} @{ip} → expect {expect}; got status={a_q.get('status')} answers={a_q.get('answers') or []}",
            )

    same_ip = ns1_ip == ns2_ip
    add(
        "ns_ip_same",
        True,
        f"ns1_ip={ns1_ip} ns2_ip={ns2_ip} "
        + ("(same IP — OK for single server; some UIs still complain)" if same_ip else "(distinct IPs)"),
        critical=False,
    )

    # Parent / registry view — NS the parent zone publishes for this domain
    parent_nss = lookup_parent_nameservers(parent_zone)
    add(
        "parent_zone_ns",
        bool(parent_nss),
        f"Parent zone {parent_zone or '—'} NS: {', '.join(parent_nss) if parent_nss else 'lookup failed'}",
        critical=False,
    )

    published = False
    parent_detail = "no parent NS to query"
    queried: list[str] = []
    for pns in parent_nss[:4]:
        parent = _dig("NS", domain, server=pns, norecurse=True)
        queried.append(pns)
        names = _ns_hostnames_from_dig(parent)
        raw_l = (parent.get("raw") or "").lower()
        # Match exact NS targets, or substring in raw (glue/additional edge cases)
        hit = (ns1 in names and ns2 in names) or (ns1 in raw_l and ns2 in raw_l)
        if hit:
            published = True
            parent_detail = (
                f"@{pns} (parent of {parent_zone}) already publishes {ns1}/{ns2}"
            )
            break
        ns_lines = " | ".join(
            ln.strip()
            for ln in (parent.get("raw") or "").splitlines()
            if "NS" in ln.upper() and not ln.strip().startswith(";")
        )[:500]
        parent_detail = (
            f"@{pns}: NOT yet on your NS. "
            + (ns_lines or parent.get("error") or parent.get("status") or "no NS data")
        )

    add(
        "parent_delegation",
        published,
        parent_detail
        + (f" (queried {', '.join(queried)})" if queried and not published else ""),
        critical=False,  # registry/registrar publish — not a PowerDNS zone bug
    )

    # Recursive public view (cache may lag parent by minutes)
    pub_ns = _dig("NS", domain, server="1.1.1.1", norecurse=False)
    pub_names = _ns_hostnames_from_dig(pub_ns)
    pub_delegated = ns1 in pub_names and ns2 in pub_names
    add(
        "public_ns_view",
        pub_delegated,
        f"Public resolver NS {domain}: {sorted(pub_names) or pub_ns.get('status') or '—'}"
        + (" (matches panel ns1/ns2)" if pub_delegated else " (not your NS yet, or still cached)"),
        critical=False,
    )

    pub_ns1 = _dig("A", ns1, server="1.1.1.1")
    pub_ok = ns1_ip in " ".join(pub_ns1.get("answers") or [])
    add(
        "public_ns1_resolves",
        pub_ok,
        f"Public A {ns1}: status={pub_ns1.get('status')} answers={pub_ns1.get('answers') or []} "
        f"(need {ns1_ip} once glue+delegation publish)",
        critical=False,
    )

    # Apex A on local (typical panel zones include this)
    apex = _dig("A", domain, server=ns1_ip)
    apex_ok = bool(apex.get("aa") and public_ip in " ".join(apex.get("answers") or []))
    add(
        "apex_a",
        apex_ok,
        f"Apex A @{ns1_ip}: {apex.get('answers') or []} (panel zones usually include this)",
        critical=False,
    )

    # Local authoritative checks only (pdns + answers @ glue IPs)
    server_checks = [
        c
        for c in report["checks"]
        if c["critical"]
        and c["name"]
        not in (
            "parent_zone_ns",
            "parent_delegation",
            "public_ns_view",
            "public_ns1_resolves",
            "ns_ip_same",
            "apex_a",
        )
    ]
    failed_critical = [c for c in server_checks if not c["pass"]]
    failed_soft = [c for c in report["checks"] if not c["critical"] and not c["pass"]]

    if not failed_critical and published:
        report["summary"] = (
            f"Server-side checks PASS and parent zone ({parent_zone}) already delegates to your NS. "
            "If a domain still fails, check that domain’s own zone exists here before setting NS."
        )
    elif not failed_critical and not published:
        report["summary"] = (
            "Server-side checks PASS. "
            f"The {parent_zone or 'parent'} zone still does NOT list your NS — problem is "
            "registrar/registry publish (or pending glue), not PowerDNS zone content on this box."
        )
        report["next_steps"] = [
            "At the registrar: set the domain nameservers to ns1/ns2 (glue hosts alone are not enough).",
            "If nameservers are in-bailiwick, register glue/child hosts with this server’s IP first.",
            "Check for pending/error status on glue or NS change.",
            f"Verify from outside: dig +norecurse SOA {domain} @{ns1_ip}  → flags must include aa.",
        ]
        if parent_zone.endswith(".za") or parent_zone == "za":
            report["next_steps"].insert(
                0,
                "For .za / ZACR: ask the registrar for the registry poll/error if the change stays pending.",
            )
    else:
        report["summary"] = (
            f"{len(failed_critical)} critical server-side check(s) FAILED — fix these before "
            "a strict registry will accept your nameservers."
        )
        report["next_steps"] = [
            c["name"] + ": " + c["detail"] for c in failed_critical
        ]

    if failed_soft and not failed_critical:
        report["next_steps"] = report.get("next_steps") or []
        report["next_steps"].append(
            "Also note: " + "; ".join(f"{c['name']} FAIL" for c in failed_soft)
        )

    return report
