from __future__ import annotations

import crypt
import json
import os
import pwd
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..config import get_settings, load_features

HOME_SUBDIRS = ("domains", "config", "mail", "plugins", "logs")
JAIL_SHELL = "/usr/local/bin/mrmpanel-jail-shell"


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def operator_user() -> str:
    """Return the non-root sudo account allowed to manage all hosting homes."""
    username = str(load_features().get("operator_user") or "").strip()
    if not username or username == "root":
        return ""
    try:
        pwd.getpwnam(username)
    except KeyError:
        return ""
    return username


def grant_operator_access(path: Path, recursive: bool = True) -> None:
    """Grant the installer account access without changing owner/mode semantics."""
    username = operator_user()
    if not username or not path.exists() or not shutil.which("setfacl"):
        return

    access_cmd = ["setfacl"]
    if recursive:
        access_cmd.append("-R")
    access_cmd.extend(["-m", f"u:{username}:rwX", str(path)])
    _run(access_cmd)

    directories = [path] if path.is_dir() else []
    if recursive and path.is_dir():
        directories.extend(Path(root) / name for root, names, _ in os.walk(path) for name in names)
    # Default ACLs make access persistent for files and directories created later.
    for start in range(0, len(directories), 200):
        batch = directories[start : start + 200]
        _run(["setfacl", "-m", f"d:u:{username}:rwx", *map(str, batch)])


def _user_meta_path(username: str) -> Path:
    return get_settings().data_dir / "users" / f"{username}.json"


def get_hosting_user(username: str) -> dict | None:
    path = _user_meta_path(username)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_hosting_user(meta: dict) -> None:
    path = _user_meta_path(meta["username"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2))
    path.chmod(0o600)


def verify_panel_password(meta: dict, password: str) -> bool:
    from ..auth import verify_bcrypt

    ph = meta.get("panel_password_hash")
    if not ph:
        return False
    return verify_bcrypt(password, ph)


def set_panel_password(username: str, password: str) -> None:
    from ..auth import hash_password

    meta = get_hosting_user(username)
    if not meta:
        raise ValueError("User not found")
    meta["panel_password_hash"] = hash_password(password)
    save_hosting_user(meta)
    # Keep OS login in sync when possible
    try:
        hashed = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
        _run(["usermod", "-p", hashed, username])
    except Exception:
        pass


def list_hosting_users() -> list[dict]:
    users = []
    marker = get_settings().data_dir / "users"
    marker.mkdir(parents=True, exist_ok=True)
    for p in sorted(marker.glob("*.json")):
        data = json.loads(p.read_text())
        # Never expose password hash to templates accidentally — strip in copies for list
        safe = {k: v for k, v in data.items() if k != "panel_password_hash"}
        users.append(safe)
    return users


def create_hosting_user(
    username: str,
    password: str,
    display_name: str = "",
    plan_id: str | None = None,
) -> dict:
    from ..auth import hash_password
    from . import plans as plans_svc

    if not re.fullmatch(r"[a-z][a-z0-9_]{2,31}", username):
        raise ValueError("Username must be 3–32 chars: lowercase, digit, underscore")
    try:
        pwd.getpwnam(username)
        raise ValueError(f"OS user {username} already exists")
    except KeyError:
        pass

    plans_svc.ensure_plans()
    chosen = (plan_id or plans_svc.DEFAULT_PLAN_ID).strip().lower()
    if not plans_svc.get_plan(chosen):
        raise ValueError(f"Unknown plan: {chosen}")

    home = get_settings().home_root / username
    hashed = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    shell = JAIL_SHELL if Path(JAIL_SHELL).exists() else "/bin/bash"
    _run(["useradd", "-m", "-d", str(home), "-s", shell, "-p", hashed, username])

    for sub in HOME_SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)

    php_ini = home / "config" / "php.ini"
    if not php_ini.exists():
        php_ini.write_text(
            "; mrmpanel per-user PHP overrides\n"
            "memory_limit = 256M\n"
            "upload_max_filesize = 64M\n"
        )

    _run(["chown", "-R", f"{username}:{username}", str(home)])
    os.chmod(home, 0o750)
    grant_operator_access(home)
    if load_features().get("mail"):
        from .mail import ensure_vmail_home_access

        ensure_vmail_home_access(username)
    _ensure_sshd_match(username)
    _write_user_bashrc(home, username)

    meta = {
        "username": username,
        "display_name": display_name or username,
        "home": str(home),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mail_enabled": bool(load_features().get("mail")),
        "plan_id": chosen,
        "panel_password_hash": hash_password(password),
    }
    save_hosting_user(meta)
    safe = {k: v for k, v in meta.items() if k != "panel_password_hash"}
    return safe


def delete_hosting_user(username: str, remove_home: bool = False) -> None:
    path = _user_meta_path(username)
    if path.exists():
        path.unlink()
    _remove_sshd_match(username)
    if remove_home:
        _run(["userdel", "-r", username])
    else:
        _run(["userdel", username])


def _sshd_dropin_dir() -> Path:
    d = Path("/etc/ssh/sshd_config.d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_sshd_match(username: str) -> None:
    content = f"""# mrmpanel restrictions for {username}
Match User {username}
    AllowTcpForwarding no
    X11Forwarding no
    PermitTunnel no
    AllowAgentForwarding no
    AcceptEnv LANG LC_*
"""
    dropin = _sshd_dropin_dir() / f"zz-mrmpanel-{username}.conf"
    try:
        dropin.write_text(content)
        subprocess.run(["sshd", "-t"], check=False, capture_output=True)
        subprocess.run(["systemctl", "reload", "sshd"], check=False, capture_output=True)
    except Exception:
        alt = get_settings().data_dir / "ssh" / f"{username}.conf"
        alt.parent.mkdir(parents=True, exist_ok=True)
        alt.write_text(content)


def _remove_sshd_match(username: str) -> None:
    dropin = _sshd_dropin_dir() / f"zz-mrmpanel-{username}.conf"
    if dropin.exists():
        dropin.unlink()
    alt = get_settings().data_dir / "ssh" / f"{username}.conf"
    if alt.exists():
        alt.unlink()
    subprocess.run(["systemctl", "reload", "sshd"], check=False, capture_output=True)


def _write_user_bashrc(home: Path, username: str) -> None:
    jail_rc = Path("/etc/mrmpanel/bashrc-jail")
    bashrc = home / ".bashrc"
    snippet = f"""
# mrmpanel home jail helpers
export HOME={home}
cd "$HOME" 2>/dev/null || true
if [[ -f /etc/mrmpanel/bashrc-jail ]]; then
  source /etc/mrmpanel/bashrc-jail
fi
"""
    existing = bashrc.read_text() if bashrc.exists() else ""
    if "mrmpanel home jail" not in existing:
        bashrc.write_text(existing + snippet)
        try:
            pw = pwd.getpwnam(username)
            os.chown(bashrc, pw.pw_uid, pw.pw_gid)
        except KeyError:
            pass
    _ = jail_rc  # installed by install.sh


def user_home(username: str) -> Path:
    return get_settings().home_root / username


def ensure_domain_dir(username: str, domain: str) -> Path:
    path = user_home(username) / "domains" / domain
    path.mkdir(parents=True, exist_ok=True)
    try:
        pw = pwd.getpwnam(username)
        for root, dirs, files in os.walk(path):
            os.chown(root, pw.pw_uid, pw.pw_gid)
            for name in dirs + files:
                os.chown(os.path.join(root, name), pw.pw_uid, pw.pw_gid)
    except KeyError:
        pass
    grant_operator_access(path)
    return path
