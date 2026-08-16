"""One-click Roundcube SSO tokens for the panel mail UI."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from ..config import get_settings, load_features

MASTER_USER = "mrmpanel"
# Roundcube Apache image runs as www-data (uid/gid 33).
ROUNDCUBE_UID = 33
ROUNDCUBE_GID = 33
TOKEN_TTL_SECONDS = 60


def _secrets_dir() -> Path:
    path = get_settings().data_dir / "secrets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _token_dir() -> Path:
    path = get_settings().data_dir / "webmail-sso"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(path, 0, ROUNDCUBE_GID)
        path.chmod(0o775)
    except OSError:
        path.chmod(0o777)
    return path


def master_password_path() -> Path:
    return _secrets_dir() / "webmail_master_password"


def ensure_webmail_master() -> str:
    """Ensure the Dovecot master account used for Roundcube SSO exists."""
    if not load_features().get("mail"):
        raise RuntimeError("Mail is not installed")

    path = master_password_path()
    if path.exists() and path.read_text().strip():
        password = path.read_text().strip()
    else:
        password = secrets.token_urlsafe(24)
        path.write_text(password + "\n")
    try:
        os.chown(path, 0, ROUNDCUBE_GID)
        path.chmod(0o640)
    except OSError:
        path.chmod(0o644)

    container = None
    for name in ("mrmpanel-mail", "compose-mail-1", "mail"):
        probe = subprocess.run(
            ["docker", "inspect", name],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            container = name
            break
    if not container:
        raise RuntimeError("Mail container is not running")

    listed = subprocess.run(
        ["docker", "exec", container, "setup", "dovecot-master", "list"],
        capture_output=True,
        text=True,
    )
    # DMS prints lines like "* mrmpanel" or "mrmpanel|…".
    have = False
    for line in listed.stdout.splitlines():
        cleaned = line.strip().lstrip("*").strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        name = cleaned.split("|", 1)[0].strip()
        if name == MASTER_USER:
            have = True
            break
    if have:
        updated = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "setup",
                "dovecot-master",
                "update",
                MASTER_USER,
                password,
            ],
            capture_output=True,
            text=True,
        )
        if updated.returncode != 0:
            err = (updated.stderr or updated.stdout or "failed").strip()
            raise RuntimeError(f"Could not update webmail master account: {err[-500:]}")
    else:
        created = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "setup",
                "dovecot-master",
                "add",
                MASTER_USER,
                password,
            ],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            err = (created.stderr or created.stdout or "failed").strip()
            # Race / prior install: treat "already exists" as success and sync password.
            if "already exists" in err.lower():
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        container,
                        "setup",
                        "dovecot-master",
                        "update",
                        MASTER_USER,
                        password,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                raise RuntimeError(
                    f"Could not create webmail master account: {err[-500:]}"
                )
    return password


def create_sso_token(email: str) -> str:
    """Create a one-time SSO token for the mailbox address."""
    ensure_webmail_master()
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("Invalid mailbox address")
    token = secrets.token_hex(24)
    payload = {
        "email": email,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = _token_dir() / f"{token}.json"
    path.write_text(json.dumps(payload) + "\n")
    try:
        os.chown(path, ROUNDCUBE_UID, ROUNDCUBE_GID)
        path.chmod(0o600)
    except OSError:
        path.chmod(0o666)
    return token


def webmail_sso_url(email: str) -> str:
    """Return the Roundcube URL that completes passwordless login."""
    email = email.strip().lower()
    domain = email.split("@", 1)[1]
    token = create_sso_token(email)
    return f"https://{domain}/webmail/?_sso={token}"
