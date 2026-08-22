from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import dns.exception
import dns.resolver

from ..config import get_settings, load_features


def mail_config_dir() -> Path:
    d = get_settings().data_dir / "mail" / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def dismissed_path(username: str | None = None) -> Path:
    if username:
        d = get_settings().data_dir / "dismissed" / "users"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{username}.json"
    return get_settings().data_dir / "dismissed" / "mail_warnings.json"


def load_dismissed(username: str | None = None) -> dict[str, bool]:
    p = dismissed_path(username)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def dismiss_warning(key: str, username: str | None = None) -> None:
    data = load_dismissed(username)
    data[key] = True
    path = dismissed_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def dns_guidance(domain: str) -> dict[str, Any]:
    features = load_features()
    ip = features.get("public_ip") or "YOUR.SERVER.IP"
    hostname = features.get("hostname") or "mail.example.com"
    ns1 = features.get("ns1_hostname") or ""
    ns2 = features.get("ns2_hostname") or ""
    ns1_ip = features.get("ns1_ip") or ip
    ns2_ip = features.get("ns2_ip") or ip
    dmarc_policy = features.get("mail_dmarc_policy") or "quarantine"
    out: dict[str, Any] = {
        "domain": domain,
        "a_record": f"{domain}. IN A {ip}",
        "mx": f"{domain}. IN MX 10 {hostname}.",
        "spf": f'{domain}. IN TXT "v=spf1 mx a:{hostname} ip4:{ip} ~all"',
        "dmarc": (
            f'_dmarc.{domain}. IN TXT "v=DMARC1; p={dmarc_policy}; pct=100; '
            f'rua=mailto:dmarc@{domain}"'
        ),
        "dkim_note": "Enable DKIM for this domain to generate the DKIM TXT record.",
        "ptr_note": f"PTR for {ip} should resolve to {hostname} (server-wide; ask your admin if unsure).",
    }
    if features.get("dns") and ns1 and ns2:
        out["ns"] = [
            f"{domain}. IN NS {ns1}.",
            f"{domain}. IN NS {ns2}.",
        ]
        out["ns_glue"] = [
            f"{ns1}. IN A {ns1_ip}",
            f"{ns2}. IN A {ns2_ip}",
        ]
        out["ns_note"] = (
            f"At the registrar, set this domain’s nameservers to {ns1} and {ns2} "
            f"(glue/host records → {ns1_ip} / {ns2_ip}). "
            "mrmpanel creates the zone automatically when you deploy a site."
        )
    return out


def mail_status() -> dict[str, Any]:
    """Admin-level mail warnings (server-wide)."""
    features = load_features()
    dismissed = load_dismissed()
    if not features.get("mail"):
        return {
            "installed": False,
            "message": "Mail module was not selected during install.",
            "warnings": [],
        }
    warnings = []
    dkim_marker = mail_config_dir() / "opendkim"
    if not dkim_marker.exists() and not dismissed.get("dkim"):
        warnings.append(
            {
                "key": "dkim",
                "level": "warning",
                "title": "DKIM not configured",
                "detail": "Install DKIM for better deliverability. You can ignore this warning.",
                "domain": None,
            }
        )
    if not dismissed.get("dmarc"):
        warnings.append(
            {
                "key": "dmarc",
                "level": "info",
                "title": "Add DMARC DNS record",
                "detail": "Publish a DMARC TXT record (panel shows an example). Can be ignored.",
                "domain": None,
            }
        )
    if not dismissed.get("spf"):
        warnings.append(
            {
                "key": "spf",
                "level": "info",
                "title": "Confirm SPF + MX",
                "detail": "SPF and MX should point to this server. Defaults are suggested per domain.",
                "domain": None,
            }
        )
    return {"installed": True, "warnings": warnings, "dismissed": dismissed}


def user_domain_warnings(username: str, domains: list[str]) -> dict[str, Any]:
    """Warnings scoped to a hosting user's own domains/subdomains only."""
    features = load_features()
    dismissed = load_dismissed(username)
    warnings: list[dict[str, Any]] = []
    if not domains:
        return {"installed": bool(features.get("mail")), "warnings": warnings}

    for domain in domains:
        # Always remind about A record for their hostname
        a_key = f"{domain}:a"
        if not dismissed.get(a_key):
            ip = features.get("public_ip") or "YOUR.SERVER.IP"
            warnings.append(
                {
                    "key": a_key,
                    "level": "info",
                    "title": f"DNS A record — {domain}",
                    "detail": f"Point {domain} to {ip} so the site and TLS can work.",
                    "domain": domain,
                }
            )
        if features.get("mail"):
            for kind, title, detail in (
                (
                    "spf",
                    f"SPF / MX — {domain}",
                    f"Publish SPF and MX for {domain} (see Mail → DNS for this domain).",
                ),
                (
                    "dmarc",
                    f"DMARC — {domain}",
                    f"Add a DMARC TXT record for {domain}. You can ignore this.",
                ),
                (
                    "dkim",
                    f"DKIM — {domain}",
                    f"Enable DKIM for {domain} in Mail if you send mail from this domain.",
                ),
            ):
                key = f"{domain}:{kind}"
                dkim_ok = (mail_config_dir() / "opendkim" / domain).exists()
                if kind == "dkim" and dkim_ok:
                    continue
                if not dismissed.get(key):
                    warnings.append(
                        {
                            "key": key,
                            "level": "warning" if kind == "dkim" else "info",
                            "title": title,
                            "detail": detail,
                            "domain": domain,
                        }
                    )
    return {"installed": bool(features.get("mail")), "warnings": warnings}


def list_mailboxes_for_domains(domains: list[str]) -> list[str]:
    owned = set(d.lower() for d in domains)
    # Also allow parent apex if user owns a subdomain? Only exact domain part of email.
    out = []
    for email in list_mailboxes():
        if "@" not in email:
            continue
        edomain = email.split("@", 1)[1].lower()
        if edomain in owned:
            out.append(email)
    return out


def email_domain_allowed(email: str, domains: list[str]) -> bool:
    if "@" not in email:
        return False
    edomain = email.split("@", 1)[1].lower()
    return edomain in {d.lower() for d in domains}


def _mail_container() -> str:
    for name in ("mrmpanel-mail", "compose-mail-1", "mail"):
        r = subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True
        )
        if r.returncode == 0:
            return name
    raise RuntimeError("Mail container not running")


# docker-mailserver default vmail identity (also set in compose)
VMAIL_UID = 5000
VMAIL_GID = 5000


def mailbox_address(local_part: str, domain: str) -> str:
    local = local_part.strip().lower()
    mail_domain = domain.strip().rstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        raise ValueError("Invalid mailbox name")
    if not mail_domain or "." not in mail_domain:
        raise ValueError("Invalid mailbox domain")
    return f"{local}@{mail_domain}"


def mailbox_maildir(username: str, email: str) -> Path:
    """Host path: /home/<user>/<emailaddress>/maildir"""
    return get_settings().home_root / username / email.strip().lower() / "maildir"


def dms_mailbox_path(email: str) -> Path:
    """Path inside the mail data volume (domain/localpart) that DMS expects."""
    local, domain = email.strip().lower().split("@", 1)
    return get_settings().data_dir / "mail" / "data" / domain / local


def resolve_mailbox_owner(email: str, username: str | None = None) -> str | None:
    """Hosting user that owns this mailbox (explicit, or from managed domain)."""
    if username:
        return username
    if "@" not in email:
        return None
    domain = email.strip().lower().split("@", 1)[1]
    from .domains import get_domain

    managed = get_domain(domain)
    return str(managed["username"]) if managed and managed.get("username") else None


def _ensure_maildir_tree(path: Path) -> None:
    for sub in ("cur", "new", "tmp"):
        (path / sub).mkdir(parents=True, exist_ok=True)


def _chown_vmail(path: Path) -> None:
    try:
        subprocess.run(
            ["chown", "-R", f"{VMAIL_UID}:{VMAIL_GID}", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"chown maildir failed: {e.stderr or e}") from e


def _acl_grant_user(path: Path, username: str) -> None:
    """Let the hosting user browse their maildir (DMS still owns files as vmail)."""
    subprocess.run(
        ["setfacl", "-R", "-m", f"u:{username}:rwx", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["setfacl", "-R", "-d", "-m", f"u:{username}:rwx", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


def ensure_vmail_home_access(username: str) -> None:
    """Allow docker-mailserver (uid 5000) to traverse /home/<user> to maildirs.

    Hosting homes are mode 750 / other::--- so Dovecot cannot reach
    /home/<user>/<email>/maildir unless an ACL grants execute on the home.
    Read is intentionally withheld — only path traversal is required.
    """
    home = get_settings().home_root / username
    if not home.is_dir():
        return
    subprocess.run(
        ["setfacl", "-m", f"u:{VMAIL_UID}:--x", str(home)],
        check=False,
        capture_output=True,
        text=True,
    )


def repair_mailbox_permissions() -> list[str]:
    """Re-apply vmail ownership and home traverse ACLs for all known mailboxes."""
    fixed: list[str] = []
    from .users import list_hosting_users

    for account in list_hosting_users():
        username = str(account.get("username") or "")
        if not username:
            continue
        ensure_vmail_home_access(username)
        home = get_settings().home_root / username
        if not home.is_dir():
            continue
        for mailbox_root in home.iterdir():
            if not mailbox_root.is_dir() or "@" not in mailbox_root.name:
                continue
            maildir = mailbox_root / "maildir"
            if not maildir.is_dir():
                continue
            _chown_vmail(mailbox_root)
            _acl_grant_user(mailbox_root, username)
            fixed.append(mailbox_root.name)
    return fixed


def prepare_user_maildir(username: str, email: str) -> Path:
    """
    Create /home/<user>/<email>/maildir and point DMS storage at it via symlink:
    /var/lib/mrmpanel/mail/data/<domain>/<local> -> that maildir
    """
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("Invalid email address")

    home = get_settings().home_root / username
    if not home.is_dir():
        raise RuntimeError(f"Hosting user home missing: {home}")

    ensure_vmail_home_access(username)

    maildir = mailbox_maildir(username, email)
    mailbox_root = maildir.parent  # /home/user/email@domain
    mailbox_root.mkdir(parents=True, exist_ok=True)
    _ensure_maildir_tree(maildir)

    dms_path = dms_mailbox_path(email)
    dms_path.parent.mkdir(parents=True, exist_ok=True)

    if dms_path.is_symlink():
        dms_path.unlink()
    elif dms_path.is_dir():
        # Move any existing DMS mail into the user maildir, then replace with symlink
        for sub in ("cur", "new", "tmp"):
            src = dms_path / sub
            dst = maildir / sub
            dst.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                for item in src.iterdir():
                    target = dst / item.name
                    if not target.exists():
                        item.rename(target)
        # Remove leftover empty tree
        subprocess.run(["rm", "-rf", str(dms_path)], check=False)
    elif dms_path.exists():
        dms_path.unlink()

    dms_path.symlink_to(maildir)

    _chown_vmail(mailbox_root)
    _acl_grant_user(mailbox_root, username)

    meta = get_settings().data_dir / "mail" / "mailbox-homes.json"
    data: dict[str, str] = {}
    if meta.exists():
        try:
            data = json.loads(meta.read_text())
        except json.JSONDecodeError:
            data = {}
    data[email] = str(maildir)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(data, indent=2))
    return maildir


def add_mailbox(email: str, password: str, username: str | None = None) -> Path | None:
    if not load_features().get("mail"):
        raise RuntimeError("Mail not installed")
    email = email.strip().lower()
    owner = resolve_mailbox_owner(email, username)
    if owner:
        from . import plans as plans_svc

        plans_svc.assert_can_add_mailbox(owner)
    maildir: Path | None = None
    if owner:
        maildir = prepare_user_maildir(owner, email)

    c = _mail_container()
    subprocess.run(
        ["docker", "exec", c, "setup", "email", "add", email, password],
        check=True,
        capture_output=True,
        text=True,
    )

    # DMS may recreate a real directory — re-assert symlink layout if we own it
    if owner and maildir is not None:
        prepare_user_maildir(owner, email)
    return maildir


def delete_mailbox(email: str, *, username: str | None = None) -> None:
    """Remove a mailbox from docker-mailserver and clean local bookkeeping."""
    if not load_features().get("mail"):
        raise RuntimeError("Mail not installed")
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("Invalid mailbox address")

    owner = resolve_mailbox_owner(email, username)
    if username and owner and owner != username:
        raise ValueError("Mailbox is not owned by this account")
    if username and not owner:
        # Require ownership when called from a hosting-user route
        domain = email.split("@", 1)[1]
        from .domains import get_domain

        meta = get_domain(domain)
        if not meta or meta.get("username") != username:
            raise ValueError("Mailbox is not owned by this account")

    c = _mail_container()
    proc = subprocess.run(
        ["docker", "exec", c, "setup", "email", "del", "-y", email],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        raise RuntimeError(f"Failed to delete mailbox: {detail}")

    meta = get_settings().data_dir / "mail" / "mailbox-homes.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text())
        except json.JSONDecodeError:
            data = {}
        if email in data:
            data.pop(email, None)
            meta.write_text(json.dumps(data, indent=2))

    # Best-effort clean of hosting-user maildir tree
    if owner:
        home_mail = mailbox_maildir(owner, email)
        parent = home_mail.parent  # /home/<user>/<email>
        try:
            if home_mail.is_symlink() or home_mail.exists():
                if home_mail.is_symlink():
                    home_mail.unlink()
                elif home_mail.is_dir():
                    shutil.rmtree(home_mail, ignore_errors=True)
            if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


def mailbox_storage_path(email: str) -> str | None:
    email = email.strip().lower()
    meta = get_settings().data_dir / "mail" / "mailbox-homes.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text())
            if email in data:
                return data[email]
        except json.JSONDecodeError:
            pass
    owner = resolve_mailbox_owner(email)
    if owner:
        return str(mailbox_maildir(owner, email))
    path = dms_mailbox_path(email)
    return str(path) if path.exists() else None


def enable_dkim(domain: str) -> str:
    if not load_features().get("mail"):
        raise RuntimeError("Mail not installed")
    c = _mail_container()
    result = subprocess.run(
        ["docker", "exec", c, "setup", "config", "dkim", "domain", domain],
        check=False,
        capture_output=True,
        text=True,
    )
    key_paths = (
        mail_config_dir() / "opendkim" / "keys" / domain / "mail.txt",
        Path("/var/lib/mrmpanel/mail/config/opendkim/keys") / domain / "mail.txt",
    )
    if result.returncode and not any(path.exists() for path in key_paths):
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"DKIM generation failed: {detail}")
    # Mark configured
    dkim_dir = mail_config_dir() / "opendkim"
    dkim_dir.mkdir(parents=True, exist_ok=True)
    (dkim_dir / domain).write_text("enabled\n")
    # Try read public key
    for path in key_paths:
        if path.exists():
            return path.read_text()
    return "DKIM requested — check mail container config for the public key TXT record."


def _resolver_txt(name: str, nameserver: str | None = None) -> list[str]:
    try:
        resolver = dns.resolver.Resolver(configure=True)
        resolver.lifetime = 5
        if nameserver:
            resolver.nameservers = [nameserver]
        answers = resolver.resolve(name, "TXT")
    except (dns.exception.DNSException, OSError):
        return []
    records: list[str] = []
    for answer in answers:
        strings = getattr(answer, "strings", None)
        if strings is not None:
            records.append(b"".join(strings).decode("utf-8", errors="replace"))
        else:
            records.append(str(answer).strip('"').replace('" "', ""))
    return records


def _resolver_mx(domain: str, nameserver: str | None = None) -> list[str]:
    try:
        resolver = dns.resolver.Resolver(configure=True)
        resolver.lifetime = 5
        if nameserver:
            resolver.nameservers = [nameserver]
        answers = resolver.resolve(domain, "MX")
    except (dns.exception.DNSException, OSError):
        return []
    return [
        f"{int(answer.preference)} {str(answer.exchange).rstrip('.')}"
        for answer in answers
    ]


def _public_txt(name: str) -> list[str]:
    return _resolver_txt(name)


def _public_mx(domain: str) -> list[str]:
    return _resolver_mx(domain)


def _auth_lookup(
    domain: str,
) -> tuple[dict[str, list[str]], dict[str, list[str]], str]:
    """Prefer PowerDNS for zones we host; otherwise query public DNS.

    Returns (txt_by_name, mx_by_domain, source) where source is
    "authoritative" or "public".
    """
    from . import dns as dns_service

    features = load_features()
    if features.get("dns") and dns_service.zone_exists(domain):
        zone = dns_service.zone_records(domain)
        txt: dict[str, list[str]] = {}
        mx: dict[str, list[str]] = {}
        for name, types in zone.items():
            if "TXT" in types:
                txt[name] = types["TXT"]
            if "MX" in types and name == domain:
                mx[domain] = [
                    " ".join(part.rstrip(".") for part in value.split(None, 1))
                    for value in types["MX"]
                ]
        return txt, mx, "authoritative"

    # External DNS: ask public resolvers.
    names = (domain, f"_dmarc.{domain}", f"mail._domainkey.{domain}")
    txt = {name: _public_txt(name) for name in names}
    mx = {domain: _public_mx(domain)}
    return txt, mx, "public"


def _dkim_value(text: str) -> str | None:
    chunks = re.findall(r'"([^"]*)"', text)
    value = "".join(chunks).strip() if chunks else text.strip()
    start = value.find("v=DKIM1")
    if start < 0:
        return None
    value = value[start:]
    # Remove zone-file syntax after the record when present.
    return value.splitlines()[0].strip().rstrip(")")


def mail_security_audit(domain_names: list[str]) -> list[dict[str, Any]]:
    """Check SPF, DKIM and DMARC for managed mail domains.

    Domains hosted by this server's PowerDNS are read from the authoritative
    zone (avoids stale public-resolver cache after a just-published change).
    External domains still use public DNS.
    """
    unique = sorted(set(domain_names))
    rows: list[dict[str, Any]] = []
    for domain in unique:
        txt, mx_map, source = _auth_lookup(domain)
        spf_records = [
            value
            for value in txt.get(domain, [])
            if value.lower().startswith("v=spf1")
        ]
        dmarc_records = [
            value
            for value in txt.get(f"_dmarc.{domain}", [])
            if value.lower().startswith("v=dmarc1")
        ]
        dkim_records = [
            value
            for value in txt.get(f"mail._domainkey.{domain}", [])
            if value.lower().startswith("v=dkim1")
        ]
        mx_records = mx_map.get(domain, [])
        dmarc_policy = ""
        dmarc_pct = 100
        if len(dmarc_records) == 1:
            policy_match = re.search(
                r"(?:^|;)\s*p\s*=\s*(none|quarantine|reject)(?:\s*;|$)",
                dmarc_records[0],
                re.IGNORECASE,
            )
            pct_match = re.search(
                r"(?:^|;)\s*pct\s*=\s*(\d{1,3})(?:\s*;|$)",
                dmarc_records[0],
                re.IGNORECASE,
            )
            dmarc_policy = policy_match.group(1).lower() if policy_match else ""
            if pct_match:
                dmarc_pct = min(100, int(pct_match.group(1)))
        dkim_ok = False
        if len(dkim_records) == 1:
            key_match = re.search(
                r"(?:^|;)\s*p\s*=\s*([A-Za-z0-9+/]+={0,2})",
                dkim_records[0],
            )
            dkim_ok = bool(key_match and len(key_match.group(1)) >= 16)
        rows.append(
            {
                "domain": domain,
                "source": source,
                "mx": {
                    "valid": bool(mx_records),
                    "records": mx_records,
                },
                "spf": {
                    "valid": len(spf_records) == 1
                    and bool(
                        re.search(
                            r"(?:^|\s)[?~+\-]?all(?:\s|$)|(?:^|\s)redirect=",
                            spf_records[0].lower(),
                        )
                    ),
                    "records": spf_records,
                },
                "dmarc": {
                    "valid": len(dmarc_records) == 1
                    and bool(
                        re.search(
                            r"(?:^|;)\s*p\s*=\s*(?:none|quarantine|reject)(?:\s*;|$)",
                            dmarc_records[0].lower(),
                        )
                    ),
                    "records": dmarc_records,
                    "policy": dmarc_policy,
                    "pct": dmarc_pct,
                    "enforced": dmarc_policy in {"quarantine", "reject"}
                    and dmarc_pct == 100,
                },
                "dkim": {
                    "valid": dkim_ok,
                    "records": dkim_records,
                    "selector": "mail",
                },
            }
        )
    return rows


def configure_mail_security(
    domain_names: list[str],
    *,
    spf: bool,
    dkim: bool,
    dmarc: bool,
    dmarc_policy: str = "quarantine",
) -> list[dict[str, str]]:
    """Preserve valid public policies and configure missing records locally."""
    features = load_features()
    hostname = str(features.get("hostname") or "")
    public_ip = str(features.get("public_ip") or "")
    if dmarc_policy not in {"none", "quarantine", "reject"}:
        raise ValueError("DMARC policy must be none, quarantine, or reject")
    audit = {row["domain"]: row for row in mail_security_audit(domain_names)}
    actions: list[dict[str, str]] = []

    from . import dns as dns_service

    for domain in sorted(set(domain_names)):
        row = audit[domain]
        local_zone = bool(features.get("dns") and dns_service.zone_exists(domain))

        if row["mx"]["valid"]:
            actions.append({"domain": domain, "kind": "MX", "result": "valid; kept"})
        elif local_zone:
            dns_service.ensure_mx_record(domain, hostname)
            actions.append(
                {
                    "domain": domain,
                    "kind": "MX",
                    "result": "published",
                    "record": f"{domain} MX 10 {hostname}.",
                }
            )
        else:
            actions.append(
                {
                    "domain": domain,
                    "kind": "MX",
                    "result": "missing; external DNS must be updated",
                    "record": f"{domain} MX 10 {hostname}.",
                }
            )

        if spf:
            if row["spf"]["valid"]:
                actions.append({"domain": domain, "kind": "SPF", "result": "valid; kept"})
            elif local_zone:
                value = f"v=spf1 mx a:{hostname} ip4:{public_ip} ~all"
                dns_service.upsert_txt_record(domain, domain, value, "v=spf1")
                actions.append(
                    {
                        "domain": domain,
                        "kind": "SPF",
                        "result": "published",
                        "record": f'{domain} TXT "{value}"',
                    }
                )
            else:
                value = f"v=spf1 mx a:{hostname} ip4:{public_ip} ~all"
                actions.append(
                    {
                        "domain": domain,
                        "kind": "SPF",
                        "result": "missing; external DNS must be updated",
                        "record": f'{domain} TXT "{value}"',
                    }
                )

        if dmarc:
            policy_rank = {"none": 0, "quarantine": 1, "reject": 2}
            current_policy = str(row["dmarc"].get("policy") or "")
            current_pct = int(row["dmarc"].get("pct") or 100)
            meets_policy = (
                row["dmarc"]["valid"]
                and policy_rank.get(current_policy, -1)
                >= policy_rank[dmarc_policy]
                and (dmarc_policy == "none" or current_pct == 100)
            )
            if meets_policy:
                actions.append({"domain": domain, "kind": "DMARC", "result": "valid; kept"})
            elif local_zone:
                value = (
                    f"v=DMARC1; p={dmarc_policy}; pct=100; "
                    f"rua=mailto:dmarc@{domain}"
                )
                dns_service.upsert_txt_record(
                    domain, f"_dmarc.{domain}", value, "v=dmarc1"
                )
                actions.append(
                    {
                        "domain": domain,
                        "kind": "DMARC",
                        "result": "published",
                        "record": f'_dmarc.{domain} TXT "{value}"',
                    }
                )
            else:
                value = (
                    f"v=DMARC1; p={dmarc_policy}; pct=100; "
                    f"rua=mailto:dmarc@{domain}"
                )
                actions.append(
                    {
                        "domain": domain,
                        "kind": "DMARC",
                        "result": "missing; external DNS must be updated",
                        "record": f'_dmarc.{domain} TXT "{value}"',
                    }
                )

        if dkim:
            if row["dkim"]["valid"]:
                actions.append({"domain": domain, "kind": "DKIM", "result": "valid; kept"})
            else:
                key_text = enable_dkim(domain)
                value = _dkim_value(key_text)
                if value and local_zone:
                    dns_service.upsert_txt_record(
                        domain,
                        f"mail._domainkey.{domain}",
                        value,
                        "v=dkim1",
                    )
                    actions.append(
                        {
                            "domain": domain,
                            "kind": "DKIM",
                            "result": "generated and published",
                            "record": f'mail._domainkey.{domain} TXT "{value}"',
                        }
                    )
                elif value:
                    actions.append(
                        {
                            "domain": domain,
                            "kind": "DKIM",
                            "result": "generated; external DNS must be updated",
                            "record": f'mail._domainkey.{domain} TXT "{value}"',
                        }
                    )
                else:
                    actions.append(
                        {"domain": domain, "kind": "DKIM", "result": "generation requested; public key not found"}
                    )
    return actions


def list_mailboxes() -> list[str]:
    if not load_features().get("mail"):
        return []
    accounts = mail_config_dir() / "postfix-accounts.cf"
    if not accounts.exists():
        # inside volume path used by docker-mailserver
        alt = Path("/var/lib/mrmpanel/mail/config/postfix-accounts.cf")
        accounts = alt if alt.exists() else accounts
    if not accounts.exists():
        return []
    lines = []
    for line in accounts.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            lines.append(line.split("|")[0])
    return lines
