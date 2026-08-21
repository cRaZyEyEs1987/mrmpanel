from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    data_dir: Path = Field(default=Path("/var/lib/mrmpanel"))
    stacks_dir: Path = Field(default=Path("/opt/mrmpanel/stacks"))
    assets_dir: Path = Field(default=Path("/opt/mrmpanel/assets"))
    features_file: Path = Field(default=Path("/var/lib/mrmpanel/features.json"))
    secret_key: str = Field(default="mrmpanel-dev-change-me")
    session_cookie: str = "mrmpanel_session"
    docker_network: str = "mrmpanel"
    home_root: Path = Path("/home")
    panel_title: str = "mrmpanel"


def _env_settings() -> Settings:
    data = Path(os.environ.get("MRMPANEL_DATA", "/var/lib/mrmpanel"))
    return Settings(
        data_dir=data,
        stacks_dir=Path(os.environ.get("MRMPANEL_STACKS", "/opt/mrmpanel/stacks")),
        assets_dir=Path(os.environ.get("MRMPANEL_ASSETS", "/opt/mrmpanel/assets")),
        features_file=Path(os.environ.get("MRMPANEL_FEATURES", str(data / "features.json"))),
        secret_key=os.environ.get("MRMPANEL_SECRET", "mrmpanel-dev-change-me"),
    )


@lru_cache
def get_settings() -> Settings:
    return _env_settings()


def default_features() -> dict[str, Any]:
    return {
        "version": "0.1.0",
        "hostname": "localhost",
        "operator_user": "",
        "web": True,
        "mail": False,
        "mariadb": False,
        "postgres": False,
        "dns": True,
        "public_ip": "",
        "acme_email": "",
        "ns_base_domain": "",
        "ns1_hostname": "",
        "ns2_hostname": "",
        "ns1_ip": "",
        "ns2_ip": "",
        "mail_security_spf": False,
        "mail_security_dkim": False,
        "mail_security_dmarc": False,
        "mail_dmarc_policy": "quarantine",
    }


def load_features() -> dict[str, Any]:
    settings = get_settings()
    path = settings.features_file
    if not path.exists():
        features = default_features()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(features, indent=2))
        except OSError:
            return features
        return features
    data = json.loads(path.read_text())
    # Merge defaults so upgrades pick up new keys
    out = default_features()
    out.update(data)
    return out


def save_features(features: dict[str, Any]) -> None:
    settings = get_settings()
    path = settings.features_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(features, indent=2) + "\n")
    path.chmod(0o644)


def ensure_data_dirs() -> None:
    s = get_settings()
    for sub in (
        "",
        "secrets",
        "sites",
        "domains",
        "databases",
        "mail/config",
        "dismissed",
        "plugins",
        "users",
        "dns",
        "webmail-sso",
    ):
        try:
            (s.data_dir / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    try:
        from .services import plans as plans_svc

        plans_svc.ensure_plans()
    except Exception:
        pass
