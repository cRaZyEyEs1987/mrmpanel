from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import get_settings, load_features
from .sites import new_db_credentials


def _run_mariadb(sql: str) -> None:
    from pathlib import Path

    root_pw = Path("/var/lib/mrmpanel/secrets/mariadb_root_password").read_text().strip()
    for name in ("mrmpanel-mariadb", "compose-mariadb-1", "mariadb"):
        cmd = [
            "docker",
            "exec",
            "-i",
            name,
            "mariadb",
            "-uroot",
            f"-p{root_pw}",
            "-e",
            sql,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return
    raise RuntimeError(f"MariaDB create failed: {r.stderr}")


def _run_postgres(sql: str) -> None:
    cmd_base = ["docker", "exec", "-i"]
    for name in ("mrmpanel-postgres", "compose-postgres-1", "postgres"):
        cmd = cmd_base + [name, "psql", "-U", "mrmpanel", "-c", sql]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return
    raise RuntimeError(f"Postgres create failed: {r.stderr}")


def create_database(engine: str, prefix: str = "site") -> dict[str, Any]:
    features = load_features()
    creds = new_db_credentials(prefix)
    if engine == "mariadb":
        if not features.get("mariadb"):
            raise RuntimeError("MariaDB is not installed")
        db, user, pw = creds["db_name"], creds["db_user"], creds["db_password"]
        sql = (
            f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            f"CREATE USER '{user}'@'%' IDENTIFIED BY '{pw}';"
            f"GRANT ALL PRIVILEGES ON `{db}`.* TO '{user}'@'%';"
            f"FLUSH PRIVILEGES;"
        )
        _run_mariadb(sql)
        return {**creds, "engine": "mariadb", "host": "mrmpanel-mariadb", "port": "3306"}
    if engine == "postgres":
        if not features.get("postgres"):
            raise RuntimeError("PostgreSQL is not installed")
        db, user, pw = creds["db_name"], creds["db_user"], creds["db_password"]
        # Create role + db
        _run_postgres(f"CREATE USER {user} WITH PASSWORD '{pw}';")
        _run_postgres(f"CREATE DATABASE {db} OWNER {user};")
        return {**creds, "engine": "postgres", "host": "mrmpanel-postgres", "port": "5432"}
    raise ValueError("engine must be mariadb or postgres")


def _registry_dir() -> Path:
    path = get_settings().data_dir / "databases"
    path.mkdir(parents=True, exist_ok=True)
    return path


def register_database(info: dict[str, Any], username: str) -> dict[str, Any]:
    """Persist ownership metadata for a database created outside a site."""
    meta = {
        key: value
        for key, value in info.items()
        if key not in {"db_password"}
    }
    meta.update(
        {
            "id": f"{info['engine']}__{info['db_name']}",
            "username": username,
            "source": "standalone",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    path = _registry_dir() / f"{meta['id']}.json"
    path.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def _safe_ident(value: str) -> str:
    name = (value or "").strip()
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"Unsafe database identifier: {value!r}")
    return name


def delete_database(info: dict[str, Any]) -> None:
    """Drop a MariaDB/Postgres database and its dedicated user."""
    engine = (info.get("engine") or "").strip().lower()
    db = _safe_ident(str(info.get("db_name") or ""))
    user = _safe_ident(str(info.get("db_user") or ""))
    if engine == "mariadb":
        _run_mariadb(
            f"DROP DATABASE IF EXISTS `{db}`;"
            f"DROP USER IF EXISTS '{user}'@'%';"
            f"FLUSH PRIVILEGES;"
        )
    elif engine == "postgres":
        # Terminate sessions so DROP DATABASE can succeed.
        _run_postgres(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{db}' AND pid <> pg_backend_pid();"
        )
        _run_postgres(f"DROP DATABASE IF EXISTS {db};")
        _run_postgres(f"DROP ROLE IF EXISTS {user};")
    else:
        raise ValueError(f"Unsupported database engine: {engine}")

    registry = _registry_dir() / f"{engine}__{db}.json"
    if registry.exists():
        registry.unlink()


def list_databases(username: str = "") -> list[dict[str, Any]]:
    """List standalone and site databases, optionally scoped by owner."""
    out: list[dict[str, Any]] = []
    for path in sorted(_registry_dir().glob("*.json")):
        try:
            item = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not username or item.get("username") == username:
            out.append(item)

    from .sites import list_sites

    for site in list_sites():
        owner = str(site.get("username") or "")
        db = site.get("db")
        if not db or (username and owner != username):
            continue
        out.append(
            {
                "id": f"site__{site.get('id')}",
                "username": owner,
                "engine": db.get("engine"),
                "db_name": db.get("db_name"),
                "db_user": db.get("db_user"),
                "host": db.get("host"),
                "port": db.get("port"),
                "source": "site",
                "site": site.get("domain"),
                "created_at": site.get("created_at"),
            }
        )
    return sorted(out, key=lambda item: (item.get("username", ""), item.get("db_name", "")))


def pick_engine_for_stack(stack: dict, preferred: str | None = None) -> str | None:
    features = load_features()
    requires = stack.get("requires") or []
    if preferred:
        if preferred == "mariadb" and features.get("mariadb"):
            return "mariadb"
        if preferred == "postgres" and features.get("postgres"):
            return "postgres"
    pref = stack.get("preferred_db")
    if pref and features.get(pref):
        return pref
    for req in requires:
        if features.get(req):
            return req
    if features.get("mariadb"):
        return "mariadb"
    if features.get("postgres"):
        return "postgres"
    return None
