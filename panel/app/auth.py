from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

import bcrypt

from .config import get_settings


def _admin_path() -> Path:
    return get_settings().data_dir / "admin.json"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_bcrypt(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except Exception:
        return False


def create_admin(username: str, password: str) -> None:
    path = _admin_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "username": username,
        "password_hash": hash_password(password),
        "role": "admin",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def load_admin() -> dict[str, Any] | None:
    path = _admin_path()
    if not path.exists():
        secrets_pw = get_settings().data_dir / "secrets" / "admin_password"
        if secrets_pw.exists():
            create_admin("admin", secrets_pw.read_text().strip())
        else:
            create_admin("admin", "admin")
    return json.loads(_admin_path().read_text())


def verify_admin_password(password: str, admin: dict[str, Any]) -> bool:
    if "password_hash" in admin:
        return verify_bcrypt(password, admin["password_hash"])
    if "password_sha256" in admin and "salt" in admin:
        digest = hashlib.sha256((admin["salt"] + password).encode()).hexdigest()
        if secrets.compare_digest(digest, admin["password_sha256"]):
            admin["password_hash"] = hash_password(password)
            admin.pop("password_sha256", None)
            admin.pop("salt", None)
            _admin_path().write_text(json.dumps(admin, indent=2))
            return True
    return False


def authenticate_admin(username: str, password: str) -> dict[str, Any] | None:
    admin = load_admin()
    if not admin or username != admin.get("username"):
        return None
    if not verify_admin_password(password, admin):
        return None
    return {"username": admin["username"], "role": "admin"}


def authenticate_hosting_user(username: str, password: str) -> dict[str, Any] | None:
    from .services.users import get_hosting_user, verify_panel_password

    meta = get_hosting_user(username)
    if not meta:
        return None
    if not verify_panel_password(meta, password):
        return None
    return {
        "username": meta["username"],
        "role": "user",
        "display_name": meta.get("display_name") or meta["username"],
    }


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """Try admin first, then hosting user panel login."""
    admin = authenticate_admin(username, password)
    if admin:
        return admin
    return authenticate_hosting_user(username, password)


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("role") == "admin")


def is_hosting_user(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("role") == "user")
