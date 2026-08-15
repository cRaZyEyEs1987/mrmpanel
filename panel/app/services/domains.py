from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..config import get_settings, load_features

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def normalize_domain(value: str) -> str:
    domain = (value or "").strip().rstrip(".").lower()
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError(f"Invalid domain name: {value}")
    return domain


def _dir() -> Path:
    path = get_settings().data_dir / "domains"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(domain: str) -> Path:
    return _dir() / f"{domain}.json"


def list_domains() -> list[dict[str, Any]]:
    sync_from_sites()
    out: list[dict[str, Any]] = []
    for path in sorted(_dir().glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda item: (item.get("username", ""), item.get("domain", "")))


def get_domain(domain: str) -> dict[str, Any] | None:
    try:
        path = _path(normalize_domain(domain))
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save(meta: dict[str, Any]) -> None:
    path = _path(meta["domain"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n")
    tmp.replace(path)


def sync_from_sites() -> None:
    """Create ownership records for sites made before the Domains feature."""
    from . import dns
    from .sites import list_sites

    existing: dict[str, dict[str, Any]] = {}
    for path in _dir().glob("*.json"):
        try:
            item = json.loads(path.read_text())
            existing[item["domain"]] = item
        except (OSError, json.JSONDecodeError, KeyError):
            continue

    for site in list_sites():
        hostname = str(site.get("domain") or "").lower()
        username = str(site.get("username") or "")
        if not hostname or not username:
            continue
        candidate = dns.parent_domain(hostname)
        current = existing.get(candidate)
        # Preserve an existing owner's parent domain. An older site belonging to
        # somebody else becomes an independently managed subdomain instead.
        if current and current.get("username") != username:
            candidate = hostname
            current = existing.get(candidate)
        if current:
            continue
        meta = {
            "domain": candidate,
            "username": username,
            "source": "migrated-site",
            "created_at": site.get("created_at")
            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _save(meta)
        existing[candidate] = meta


def list_domains_for_user(username: str) -> list[dict[str, Any]]:
    return [item for item in list_domains() if item.get("username") == username]


def domain_names_for_user(username: str) -> list[str]:
    return [item["domain"] for item in list_domains_for_user(username)]


def owner_for_hostname(hostname: str) -> dict[str, Any] | None:
    """Return the most-specific managed domain covering this hostname."""
    host = normalize_domain(hostname)
    matches = [
        item
        for item in list_domains()
        if host == item["domain"] or host.endswith(f".{item['domain']}")
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item["domain"]))


def validate_site_hostname(username: str, hostname: str) -> str:
    """Require ownership and enforce one site container per exact hostname."""
    host = normalize_domain(hostname)
    owner = owner_for_hostname(host)
    if not owner:
        raise ValueError(
            f"{host} is not under a managed domain. Add the domain first."
        )
    if owner.get("username") != username:
        raise ValueError(
            f"{host} is reserved for user {owner.get('username')}. "
            "Choose another hostname or remove that domain assignment first."
        )

    from .sites import list_sites

    existing = next(
        (site for site in list_sites() if str(site.get("domain", "")).lower() == host),
        None,
    )
    if existing:
        raise ValueError(
            f"A site already uses {host} (owner: {existing.get('username')}). "
            "Delete that site before reusing the hostname."
        )
    return host


def add_domain(
    domain: str,
    username: str,
    *,
    allow_delegation: bool = False,
) -> dict[str, Any]:
    from .users import get_hosting_user

    name = normalize_domain(domain)
    sync_from_sites()
    if not get_hosting_user(username):
        raise ValueError(f"Hosting user not found: {username}")
    if get_domain(name):
        raise ValueError(
            f"{name} is already a managed domain. Delete its existing assignment first."
        )
    covering = owner_for_hostname(name)
    if (
        covering
        and covering.get("username") != username
        and not allow_delegation
    ):
        raise ValueError(
            f"{name} is inside {covering['domain']}, which belongs to "
            f"{covering.get('username')}. An administrator must assign this subdomain."
        )
    from .sites import list_sites

    existing_site = next(
        (
            site
            for site in list_sites()
            if str(site.get("domain", "")).strip().lower() == name
        ),
        None,
    )
    if existing_site:
        raise ValueError(
            f"A site already uses {name} (owner: {existing_site.get('username')}). "
            "Delete that site before assigning the same hostname as a separate domain."
        )

    meta: dict[str, Any] = {
        "domain": name,
        "username": username,
        "source": "manual",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save(meta)

    features = load_features()
    if features.get("dns"):
        try:
            from . import dns

            dns.ensure_domain_zone(name, features)
            meta["dns_zone"] = "ready"
        except Exception as exc:
            meta["dns_zone"] = "warning"
            meta["dns_error"] = str(exc)
        _save(meta)
    if features.get("web") and features.get("mail"):
        try:
            from . import webmail

            webmail.sync_routes()
        except Exception as exc:
            meta["webmail_warning"] = str(exc)
            _save(meta)
    return meta


def delete_domain(domain: str) -> None:
    name = normalize_domain(domain)
    from .sites import list_sites

    dependent = []
    for site in list_sites():
        site_domain = str(site.get("domain", "")).lower()
        owner = owner_for_hostname(site_domain)
        if owner and owner.get("domain") == name:
            dependent.append(site_domain)
    if dependent:
        raise ValueError(
            f"Delete these sites before deleting {name}: {', '.join(sorted(dependent))}"
        )
    path = _path(name)
    if path.exists():
        path.unlink()
    features = load_features()
    if features.get("web") and features.get("mail"):
        try:
            from . import webmail

            webmail.sync_routes()
        except Exception:
            pass
