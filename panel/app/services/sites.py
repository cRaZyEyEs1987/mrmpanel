from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import docker
from docker.types import Mount

from ..config import get_settings, load_features
from .stacks import (
    default_version,
    get_stack,
    resolve_image,
    stack_available,
    stack_versions,
)
from .users import ensure_domain_dir, grant_operator_access, user_home


def _client() -> docker.DockerClient:
    return docker.from_env()


def _clear_dir_contents(path: Path) -> None:
    """Remove all files/dirs inside path (keep the directory itself)."""
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def _read_wp_version(domain_dir: Path) -> str | None:
    vp = domain_dir / "wp-includes" / "version.php"
    if not vp.is_file():
        return None
    text = vp.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"\$wp_version\s*=\s*'([^']+)'", text)
    return m.group(1) if m else None


def _sites_dir() -> Path:
    d = get_settings().data_dir / "sites"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_sites() -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(_sites_dir().glob("*.json"))]


def list_sites_for_user(username: str) -> list[dict[str, Any]]:
    return [s for s in list_sites() if s.get("username") == username]


def user_domains(username: str) -> list[str]:
    # Mail and account-level controls belong to explicitly managed domains,
    # not to every individual site hostname.
    from .domains import domain_names_for_user

    return domain_names_for_user(username)


def site_owned_by(site_id: str, username: str) -> dict[str, Any] | None:
    site = get_site(site_id)
    if site and site.get("username") == username:
        return site
    return None


def get_site(site_id: str) -> dict[str, Any] | None:
    path = _sites_dir() / f"{site_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_site(meta: dict[str, Any]) -> None:
    (_sites_dir() / f"{meta['id']}.json").write_text(json.dumps(meta, indent=2))


def _write_starters(domain_dir: Path, stack: dict[str, Any]) -> None:
    for item in stack.get("starter_files") or []:
        rel = item["path"]
        target = domain_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(item.get("content", ""))


def _container_name(username: str, domain: str) -> str:
    safe = domain.replace(".", "-")
    return f"mrm-{username}-{safe}"[:63]


def _build_env(
    stack: dict[str, Any],
    db_info: dict[str, str] | None,
    username: str = "",
) -> dict[str, str]:
    env = dict(stack.get("env") or {})
    if db_info and stack.get("db_env"):
        for k, v in stack["db_env"].items():
            env[k] = v.format(**db_info)
    image = str(stack.get("image") or "")
    # webdevops images run PHP as "application"; match the hosting account so
    # storage/bootstrap/cache stays writable after we chown the bind-mount.
    if username and image.startswith("webdevops/"):
        try:
            pw = __import__("pwd").getpwnam(username)
            env.setdefault("APPLICATION_UID", str(pw.pw_uid))
            env.setdefault("APPLICATION_GID", str(pw.pw_gid))
        except KeyError:
            pass
    return env


def _build_mounts(
    username: str,
    domain_dir: Path,
    stack: dict[str, Any],
) -> list[Mount]:
    mounts = [
        Mount(
            target=stack.get("volume_mount", "/app"),
            source=str(domain_dir),
            type="bind",
        )
    ]
    home = user_home(username)
    for cm in stack.get("config_mounts") or []:
        host_path = home / cm["host_rel"]
        if host_path.exists() or not cm.get("optional", False):
            host_path.parent.mkdir(parents=True, exist_ok=True)
            if not host_path.exists():
                host_path.write_text("")
            mounts.append(
                Mount(target=cm["container"], source=str(host_path), type="bind")
            )
    return mounts


def _router_name(cname: str) -> str:
    """Traefik router names: alphanumeric and dashes only."""
    return "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in cname).strip("-")[:63]


def domain_points_here(domain: str) -> bool:
    """True when public DNS for domain already resolves to this server's IP.

    Let's Encrypt validates over HTTP against the public name, so asking for a
    certificate before DNS points here just fails and burns ACME rate limits.
    """
    server_ip = (load_features().get("public_ip") or "").strip()
    if not server_ip or not domain or "." not in domain:
        return False
    try:
        proc = subprocess.run(
            ["dig", "+short", "+time=2", "+tries=1", "A", domain],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    ips = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return server_ip in ips


def _build_labels(
    site_id: str,
    username: str,
    domain: str,
    stack_id: str,
    cname: str,
    port: int,
) -> dict[str, str]:
    router = _router_name(cname)
    http_router = f"{router}-http"
    # Exact Host only — deploy www.ben.com and ben.com as separate sites if needed.
    # Auto-adding www+apex on both causes duplicate routers and Traefik 404s.
    host_rule = f"Host(`{domain}`)"
    labels = {
        "traefik.enable": "true",
        "traefik.docker.network": get_settings().docker_network,
        f"traefik.http.routers.{http_router}.rule": host_rule,
        f"traefik.http.routers.{http_router}.entrypoints": "web",
        f"traefik.http.routers.{http_router}.service": router,
        f"traefik.http.routers.{router}.rule": host_rule,
        f"traefik.http.routers.{router}.entrypoints": "websecure",
        f"traefik.http.routers.{router}.tls": "true",
        f"traefik.http.routers.{router}.service": router,
        f"traefik.http.services.{router}.loadbalancer.server.port": str(port),
        "mrmpanel.site": site_id,
        "mrmpanel.user": username,
        "mrmpanel.domain": domain,
        "mrmpanel.stack": stack_id,
    }
    # Real certificate once the domain resolves here; Traefik's default cert
    # (browser warning) is the fallback for local or not-yet-pointed domains.
    if domain_points_here(domain):
        labels[f"traefik.http.routers.{router}.tls.certresolver"] = "letsencrypt"
    return labels


def _ensure_network(client: Any, container: Any, progress: Any = None) -> None:
    net_name = get_settings().docker_network
    try:
        net = client.networks.get(net_name)
    except docker.errors.NotFound:
        if progress:
            progress(f"Creating Docker network {net_name}", None)
        net = client.networks.create(net_name, driver="bridge", check_duplicate=True)
    # Reconnect even if already attached (idempotent enough)
    try:
        net.connect(container)
        if progress:
            progress(f"Attached container to network {net_name}", None)
    except Exception as e:
        msg = str(e).lower()
        if "already" not in msg:
            raise


def _run_container(
    *,
    username: str,
    domain: str,
    stack: dict[str, Any],
    stack_id: str,
    version: str | None,
    domain_dir: Path,
    db_info: dict[str, str] | None,
    site_id: str,
    progress: Any = None,
) -> Any:
    def p(msg: str, pct: int | None = None) -> None:
        if progress:
            progress(msg, pct)

    image = resolve_image(stack, version)
    cname = _container_name(username, domain)
    port = int(stack.get("container_port", 80))
    mounts = _build_mounts(username, domain_dir, stack)
    env = _build_env(stack, db_info, username=username)
    labels = _build_labels(site_id, username, domain, stack_id, cname, port)

    client = _client()
    try:
        old = client.containers.get(cname)
        p(f"Removing existing container {cname}", 20)
        old.remove(force=True)
    except docker.errors.NotFound:
        pass

    kwargs: dict[str, Any] = {
        "image": image,
        "name": cname,
        "detach": True,
        "environment": env,
        "mounts": mounts,
        "labels": labels,
        "network": get_settings().docker_network,
        "restart_policy": {"Name": "unless-stopped"},
    }
    if stack.get("command"):
        kwargs["command"] = stack["command"]
    if stack.get("workdir"):
        kwargs["working_dir"] = stack["workdir"]

    # Always pull WordPress tags so floating 7.0-* picks up current core, not a stale local image
    if image.startswith("wordpress:"):
        p(f"Pulling {image} (ensures current WordPress core)…", 30)
        client.images.pull(image)
        p(f"Image ready: {image}", 55)
    else:
        try:
            client.images.get(image)
            p(f"Image ready: {image}", 35)
        except docker.errors.ImageNotFound:
            p(f"Pulling image {image} (this can take a few minutes)…", 30)
            client.images.pull(image)
            p(f"Pulled {image}", 55)

    p(f"Starting container {cname}", 65)
    container = client.containers.run(**kwargs)
    _ensure_network(client, container, progress)

    # Brief settle so Traefik docker provider + app entrypoint can register
    p("Waiting for container to become ready…", 80)
    for _ in range(15):
        container.reload()
        if container.status == "running":
            break
        time.sleep(0.4)
    container.reload()
    if container.status != "running":
        logs = ""
        try:
            logs = container.logs(tail=40).decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"Container {cname} is {container.status}. Logs:\n{logs[-2000:]}"
        )

    p("Container is running — verifying Traefik Host routing…", 90)
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    code = None
    for attempt in range(30):
        req = urllib.request.Request(
            "http://127.0.0.1/",
            headers={"Host": domain},
            method="GET",
        )
        try:
            with opener.open(req, timeout=5) as resp:
                code = resp.getcode()
                break
        except urllib.error.HTTPError as e:
            code = e.code
            # 3xx means Traefik matched a router (do not follow — public DNS may 404)
            if e.code != 404:
                break
            time.sleep(1)
        except Exception as e:
            if attempt == 29:
                p(f"Local check skipped: {e}", 95)
            time.sleep(1)
    if code is not None:
        p(f"Local check Host={domain} → HTTP {code}", 95)
        if code == 404:
            p(
                "Traefik still 404 after wait — check: docker logs (traefik) for Docker API errors, "
                "and /etc/docker/daemon.json min-api-version=1.24",
                95,
            )

    return container, cname, image


def site_display_version(site: dict[str, Any]) -> str | None:
    """Version shown for a site; falls back to stack default when missing."""
    if site.get("version") is not None:
        return str(site["version"])
    stack = get_stack(site.get("stack") or "")
    if not stack:
        return None
    return default_version(stack)


def deploy_site(
    username: str,
    domain: str,
    stack_id: str,
    db_info: dict[str, str] | None = None,
    version: str | None = None,
    progress: Any = None,
    app_opts: dict[str, str] | None = None,
) -> dict[str, Any]:
    def p(msg: str, pct: int | None = None) -> None:
        if progress:
            progress(msg, pct)

    features = load_features()
    if not features.get("web"):
        raise RuntimeError("Web feature is not installed")

    p("Validating stack…", 5)
    stack = get_stack(stack_id)
    if not stack:
        raise ValueError(f"Unknown stack: {stack_id}")
    if not stack_available(stack):
        raise ValueError(
            f"Stack {stack_id} requires {[r for r in stack.get('requires', [])]} which is not installed"
        )

    versions = stack_versions(stack)
    chosen: str | None = None
    if versions:
        chosen = version or default_version(stack)
        resolve_image(stack, chosen)

    p(f"Preparing files for {domain}", 12)
    domain_dir = ensure_domain_dir(username, domain)

    # Official wordpress image only copies core when the bind-mount is empty.
    # Leftover 6.7.x files stay forever if we only recreate the container.
    if stack.get("auto_install") in {"wordpress", "laravel"}:
        if any(domain_dir.iterdir()):
            p(
                "Clearing domain directory for a clean application install",
                13,
            )
            _clear_dir_contents(domain_dir)
        resolved = resolve_image(stack, chosen) if versions else str(stack.get("image") or "")
        p(f"{stack.get('name') or stack_id} stack image: {resolved}", 14)

    if stack.get("auto_install") == "laravel":
        _laravel_scaffold(
            domain=domain,
            domain_dir=domain_dir,
            db_info=db_info or {},
            php_version=chosen or default_version(stack) or "8.3",
            laravel_version=str(stack.get("laravel_version") or "").strip(),
            app_name=((app_opts or {}).get("app_name") or domain).strip() or domain,
            progress=progress,
        )

    _write_starters(domain_dir, stack)

    site_id = f"{username}__{domain}"
    container, cname, image = _run_container(
        username=username,
        domain=domain,
        stack=stack,
        stack_id=stack_id,
        version=chosen,
        domain_dir=domain_dir,
        db_info=db_info,
        site_id=site_id,
        progress=progress,
    )

    meta = {
        "id": site_id,
        "username": username,
        "domain": domain,
        "stack": stack_id,
        "version": chosen,
        "container": cname,
        "container_id": container.id,
        "image": image,
        "db": db_info,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "path": str(domain_dir),
        "url": f"http://{domain}",
    }

    if stack.get("auto_install") == "wordpress":
        wp_opts = app_opts or {}
        wp_user = (wp_opts.get("wp_admin_user") or "admin").strip() or "admin"
        wp_pass = (wp_opts.get("wp_admin_password") or "").strip()
        generated = False
        if not wp_pass:
            wp_pass = secrets.token_urlsafe(12)
            generated = True
        wp_title = (wp_opts.get("wp_title") or domain).strip() or domain
        wp_email = (wp_opts.get("wp_admin_email") or f"admin@{domain}").strip()
        _wordpress_auto_install(
            domain=domain,
            domain_dir=domain_dir,
            db_info=db_info or {},
            admin_user=wp_user,
            admin_password=wp_pass,
            admin_email=wp_email,
            title=wp_title,
            progress=progress,
            wp_core_version=str(stack.get("wp_core_version") or "7.0"),
        )
        meta["wp_admin_user"] = wp_user
        meta["wp_admin_email"] = wp_email
        if generated:
            p(f"WordPress ready — admin '{wp_user}' password: {wp_pass}", 98)
        else:
            p(f"WordPress ready — admin user '{wp_user}'", 98)

    if stack.get("auto_install") == "laravel":
        _laravel_finalize(
            container_name=cname,
            domain=domain,
            domain_dir=domain_dir,
            username=username,
            progress=progress,
        )
        p("Laravel ready — open the site URL to see the welcome page", 98)

    # Containers may preserve restrictive source modes while unpacking an app.
    # Reapply the named ACL after deployment so the panel operator retains SFTP access.
    grant_operator_access(domain_dir)
    _save_site(meta)

    if features.get("web") and features.get("mail"):
        try:
            from . import webmail

            webmail.sync_routes()
            p(f"Webmail ready at https://{domain}/webmail/", 95)
        except Exception as exc:  # noqa: BLE001 — site deploy should still succeed
            p(f"Webmail route warning: {exc}", 95)

    if features.get("dns"):
        try:
            from . import dns as dns_svc, domains as domain_svc

            managed = domain_svc.owner_for_hostname(domain)
            zone = managed["domain"] if managed else domain
            if domain == zone:
                p(f"Creating DNS zone for {domain}…", 96)
                dns_svc.ensure_domain_zone(domain, features)
            else:
                p(f"Adding {domain} to DNS zone {zone}…", 96)
                dns_svc.ensure_host_record(zone, domain, features)
        except Exception as exc:  # noqa: BLE001 — deploy should still succeed
            p(f"DNS zone warning: {exc}", 97)

    pub = features.get("public_ip") or ""
    ns_hint = ""
    if features.get("dns") and features.get("ns1_hostname"):
        ns_hint = f"; set registrar NS to {features.get('ns1_hostname')} / {features.get('ns2_hostname')}"
    p(
        f"Done. Open http://{domain}"
        + (f" (DNS/hosts → {pub}{ns_hint})" if pub else ns_hint)
        + " — not the panel :8080 port and not a bare IP.",
        100,
    )
    return meta


def _php_sq(value: str) -> str:
    """Escape a value for a single-quoted PHP string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _materialize_wp_config_db(cfg: Path, db_info: dict[str, str], db_host: str) -> None:
    """Turn Docker getenv_docker() DB_* defines into literal defines.

    Official WordPress image writes:
      define( 'DB_HOST', getenv_docker('WORDPRESS_DB_HOST', 'mysql') );
    wordpress:cli has no WORDPRESS_DB_* env, so getenv falls back to 'mysql'.
    A second define() only warns — PHP keeps the first (mysql) value.
    """
    import re

    text = cfg.read_text(encoding="utf-8", errors="replace")
    pairs = {
        "DB_NAME": db_info.get("db_name", ""),
        "DB_USER": db_info.get("db_user", ""),
        "DB_PASSWORD": db_info.get("db_password", ""),
        "DB_HOST": db_host,
    }
    for key, val in pairs.items():
        if not val and key != "DB_PASSWORD":
            continue
        lit = f"define('{key}', '{_php_sq(val)}');"
        pat = rf"define\s*\(\s*['\"]{key}['\"]\s*,\s*[^;]+?\)\s*;"
        text, n = re.subn(pat, lit, text, count=1)
        if n == 0:
            marker = "/* That's all, stop editing!"
            if marker in text:
                text = text.replace(marker, lit + "\n" + marker, 1)
            else:
                text = text + "\n" + lit + "\n"
    # Drop duplicate DB_* defines left by older buggy rewrites
    for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        counter = [0]

        def _keep(m: re.Match[str], c: list[int] = counter) -> str:
            c[0] += 1
            return m.group(0) if c[0] == 1 else ""

        text = re.sub(
            rf"define\s*\(\s*['\"]{key}['\"]\s*,\s*[^;]+?\)\s*;",
            _keep,
            text,
        )
    cfg.write_text(text, encoding="utf-8")


def _env_escape(value: str) -> str:
    """Quote a .env value when it contains spaces or special characters."""
    text = value or ""
    if not text:
        return '""'
    if any(ch in text for ch in " \t\"#'\\"):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _write_laravel_env(
    domain_dir: Path,
    *,
    domain: str,
    app_name: str,
    db_info: dict[str, str],
) -> None:
    db_host = db_info.get("host") or "mrmpanel-mariadb"
    db_port = str(db_info.get("port") or "3306")
    lines = [
        f"APP_NAME={_env_escape(app_name)}",
        "APP_ENV=production",
        "APP_KEY=",
        "APP_DEBUG=false",
        f"APP_URL=http://{domain}",
        "",
        "APP_LOCALE=en",
        "APP_FALLBACK_LOCALE=en",
        "APP_FAKER_LOCALE=en_US",
        "",
        "LOG_CHANNEL=stack",
        "LOG_LEVEL=error",
        "",
        "DB_CONNECTION=mysql",
        f"DB_HOST={_env_escape(db_host)}",
        f"DB_PORT={_env_escape(db_port)}",
        f"DB_DATABASE={_env_escape(db_info.get('db_name') or '')}",
        f"DB_USERNAME={_env_escape(db_info.get('db_user') or '')}",
        f"DB_PASSWORD={_env_escape(db_info.get('db_password') or '')}",
        "",
        "SESSION_DRIVER=database",
        "SESSION_LIFETIME=120",
        "BROADCAST_CONNECTION=log",
        "FILESYSTEM_DISK=local",
        "QUEUE_CONNECTION=database",
        "CACHE_STORE=database",
        "",
        "MAIL_MAILER=log",
    ]
    (domain_dir / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _laravel_scaffold(
    *,
    domain: str,
    domain_dir: Path,
    db_info: dict[str, str],
    php_version: str,
    laravel_version: str = "",
    app_name: str = "",
    progress: Any = None,
) -> None:
    """Install a full Laravel application into the site bind-mount."""

    def p(msg: str, pct: int | None = None) -> None:
        if progress:
            progress(msg, pct)

    if not db_info.get("db_name"):
        raise RuntimeError("Laravel requires a MariaDB database")

    image = f"webdevops/php-nginx:{php_version}"
    client = _client()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        p(f"Pulling {image} for Laravel install…", 18)
        client.images.pull(image)

    package = "laravel/laravel"
    if laravel_version:
        package = f"{package}:{laravel_version}"

    p(f"Installing {package} with Composer (this can take a few minutes)…", 22)
    cmd = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "bash",
        "--network",
        get_settings().docker_network,
        "--user",
        "0:0",
        "--security-opt",
        "label=disable",
        "-v",
        f"{domain_dir}:/app",
        "-w",
        "/app",
        image,
        "-lc",
        (
            "set -euo pipefail; "
            "composer create-project "
            f"{package} . --prefer-dist --no-interaction --no-dev; "
            "mkdir -p storage/framework/{cache,sessions,views} storage/logs bootstrap/cache; "
            "chmod -R ug+rwx storage bootstrap/cache"
        ),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "composer create-project failed").strip()
        raise RuntimeError(f"Laravel install failed: {err[-2000:]}")

    if not (domain_dir / "artisan").is_file() or not (domain_dir / "public" / "index.php").is_file():
        raise RuntimeError("Laravel files were not created (artisan/public/index.php missing)")

    p("Writing Laravel .env with database settings…", 48)
    _write_laravel_env(
        domain_dir,
        domain=domain,
        app_name=app_name or domain,
        db_info=db_info,
    )


def _laravel_finalize(
    *,
    container_name: str,
    domain: str,
    domain_dir: Path,
    username: str,
    progress: Any = None,
) -> None:
    """Generate APP_KEY, run migrations, and fix ownership."""

    def p(msg: str, pct: int | None = None) -> None:
        if progress:
            progress(msg, pct)

    def _artisan(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "exec", container_name, "php", "artisan", *args],
            capture_output=True,
            text=True,
        )

    p("Generating Laravel application key…", 88)
    key = _artisan("key:generate", "--force")
    if key.returncode != 0:
        err = (key.stderr or key.stdout or "key:generate failed").strip()
        raise RuntimeError(f"Laravel key:generate failed: {err[-1200:]}")

    p("Running Laravel migrations…", 92)
    migrate = _artisan("migrate", "--force")
    if migrate.returncode != 0:
        err = (migrate.stderr or migrate.stdout or "migrate failed").strip()
        raise RuntimeError(f"Laravel migrate failed: {err[-1200:]}")

    link = _artisan("storage:link")
    if link.returncode != 0:
        p("storage:link warning — continuing", 94)

    try:
        pw = __import__("pwd").getpwnam(username)
        subprocess.run(
            ["chown", "-R", f"{pw.pw_uid}:{pw.pw_gid}", str(domain_dir)],
            check=False,
            capture_output=True,
            text=True,
        )
    except KeyError:
        pass

    p(f"Laravel installed for http://{domain}", 96)


def _wordpress_auto_install(
    *,
    domain: str,
    domain_dir: Path,
    db_info: dict[str, str],
    admin_user: str,
    admin_password: str,
    admin_email: str,
    title: str,
    progress: Any = None,
    wp_core_version: str = "7.0",
) -> None:
    """Finish WordPress using official wordpress:cli against the site bind-mount."""
    def p(msg: str, pct: int | None = None) -> None:
        if progress:
            progress(msg, pct)

    if not db_info.get("db_name"):
        raise RuntimeError("Missing database credentials for WordPress install")

    db_host = f"{db_info.get('host', 'mrmpanel-mariadb')}"
    if ":" not in db_host and db_info.get("port"):
        db_host = f"{db_host}:{db_info['port']}"

    p("Waiting for WordPress files…", 92)
    index = domain_dir / "index.php"
    for _ in range(90):
        if index.is_file() and (domain_dir / "wp-includes" / "version.php").is_file():
            break
        time.sleep(1)
    else:
        raise RuntimeError("WordPress files did not appear in time (index.php missing)")

    wp_ver = _read_wp_version(domain_dir)
    if wp_ver:
        p(f"WordPress core unpacked from image: {wp_ver}", 93)

    client = _client()
    cli_image = "wordpress:cli-php8.3"
    try:
        client.images.get(cli_image)
    except docker.errors.ImageNotFound:
        p(f"Pulling {cli_image} for auto-install…", 93)
        client.images.pull(cli_image)

    net = get_settings().docker_network
    url = f"http://{domain}"
    # Same env as the app container — without it getenv_docker defaults to mysql
    wp_env = [
        "-e",
        f"WORDPRESS_DB_HOST={db_host}",
        "-e",
        f"WORDPRESS_DB_NAME={db_info['db_name']}",
        "-e",
        f"WORDPRESS_DB_USER={db_info['db_user']}",
        "-e",
        f"WORDPRESS_DB_PASSWORD={db_info['db_password']}",
    ]

    def _wp(*args: str) -> subprocess.CompletedProcess[str]:
        cmd = [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--security-opt",
            "label=disable",
            "--network",
            net,
            *wp_env,
            "-v",
            f"{domain_dir}:/var/www/html",
            cli_image,
            "wp",
            "--allow-root",
            *args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    # Docker image tags can lag or a stale bind-mount can leave 6.7.x — force core from wordpress.org
    want = (wp_core_version or "").strip()
    p(f"Syncing WordPress core to {want or 'latest'} from wordpress.org…", 93)
    dl_args = ["core", "download", "--force", "--skip-content"]
    if want:
        dl_args.append(f"--version={want}")
    dl = _wp(*dl_args)
    if dl.returncode != 0:
        # Fallback: latest stable if exact minor tag missing on wordpress.org
        p("Exact version download failed — trying latest stable…", 93)
        dl = _wp("core", "download", "--force", "--skip-content")
        if dl.returncode != 0:
            err = (dl.stderr or dl.stdout or "wp core download failed").strip()
            raise RuntimeError(f"Could not download WordPress core: {err[-1200:]}")

    wp_ver = _read_wp_version(domain_dir)
    if wp_ver:
        p(f"WordPress core on disk: {wp_ver}", 94)
        if want and not wp_ver.startswith(want):
            p(f"Warning: expected {want}* but found {wp_ver}", 94)

    cfg = domain_dir / "wp-config.php"
    if not cfg.is_file():
        p("Waiting for wp-config.php from WordPress entrypoint…", 94)
        for _ in range(60):
            if cfg.is_file():
                break
            time.sleep(1)

    p(f"Writing wp-config.php with DB_HOST={db_host}", 94)
    try:
        if cfg.is_file():
            cfg.chmod(0o664)
            cfg.unlink()
    except OSError:
        pass

    create = _wp(
        "config",
        "create",
        f"--dbname={db_info['db_name']}",
        f"--dbuser={db_info['db_user']}",
        f"--dbpass={db_info['db_password']}",
        f"--dbhost={db_host}",
        "--skip-check",
        "--force",
    )
    if not cfg.is_file():
        err = (create.stderr or create.stdout or "wp config create failed").strip()
        raise RuntimeError(f"Could not create wp-config.php: {err[-1200:]}")
    if create.returncode != 0:
        p("wp config create warned; materializing literal DB_* defines", 94)
    _materialize_wp_config_db(cfg, db_info, db_host)

    p("Checking database connectivity…", 95)
    db_check = _wp("db", "check")
    if db_check.returncode != 0:
        _materialize_wp_config_db(cfg, db_info, db_host)
        time.sleep(2)
        db_check = _wp("db", "check")
        if db_check.returncode != 0:
            err = (db_check.stderr or db_check.stdout or "wp db check failed").strip()
            raise RuntimeError(
                f"WordPress cannot reach MariaDB at {db_host}: {err[-1200:]}"
            )

    p("Checking if WordPress is already installed…", 95)
    check = _wp("core", "is-installed")
    if check.returncode == 0:
        p("WordPress already installed — skipping setup", 97)
        return

    p("Running WordPress core install…", 96)
    result = _wp(
        "core",
        "install",
        f"--url={url}",
        f"--title={title}",
        f"--admin_user={admin_user}",
        f"--admin_password={admin_password}",
        f"--admin_email={admin_email}",
        "--skip-email",
    )
    if result.returncode != 0:
        time.sleep(3)
        _materialize_wp_config_db(cfg, db_info, db_host)
        result = _wp(
            "core",
            "install",
            f"--url={url}",
            f"--title={title}",
            f"--admin_user={admin_user}",
            f"--admin_password={admin_password}",
            f"--admin_email={admin_email}",
            "--skip-email",
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "wp core install failed").strip()
            raise RuntimeError(f"WordPress auto-install failed: {err[-1500:]}")

    p("WordPress installed (admin account created)", 97)


def change_site_version(site_id: str, version: str) -> dict[str, Any]:
    """Recreate the site container on a new runtime version; keep files and DB meta."""
    site = get_site(site_id)
    if not site:
        raise ValueError("Site not found")

    stack_id = site.get("stack") or ""
    stack = get_stack(stack_id)
    if not stack:
        raise ValueError(f"Unknown stack: {stack_id}")
    if not stack_available(stack):
        raise ValueError(f"Stack {stack_id} is not available")

    versions = stack_versions(stack)
    if not versions:
        raise ValueError("This stack has no selectable runtime version")

    chosen = str(version)
    resolve_image(stack, chosen)

    domain_dir = Path(site["path"])
    if not domain_dir.is_dir():
        domain_dir = ensure_domain_dir(site["username"], site["domain"])

    container, cname, _image = _run_container(
        username=site["username"],
        domain=site["domain"],
        stack=stack,
        stack_id=stack_id,
        version=chosen,
        domain_dir=domain_dir,
        db_info=site.get("db"),
        site_id=site_id,
    )

    site["version"] = chosen
    site["container"] = cname
    site["container_id"] = container.id
    site["path"] = str(domain_dir)
    site["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_site(site)
    return site


def refresh_site_tls(site_id: str) -> dict[str, Any]:
    """Recreate the site container so Traefik re-reads its labels.

    Used after a domain starts pointing at this server: the rebuilt container
    carries the letsencrypt certresolver label and Traefik requests a real cert.
    """
    site = get_site(site_id)
    if not site:
        raise ValueError("Site not found")
    domain = site.get("domain") or ""
    if not domain_points_here(domain):
        raise ValueError(
            f"{domain} does not resolve to this server yet. Point its A record here first, "
            "then try again (DNS changes can take a few minutes)."
        )

    stack_id = site.get("stack") or ""
    stack = get_stack(stack_id)
    if not stack:
        raise ValueError(f"Unknown stack: {stack_id}")

    domain_dir = Path(site["path"])
    if not domain_dir.is_dir():
        domain_dir = ensure_domain_dir(site["username"], domain)

    container, cname, _image = _run_container(
        username=site["username"],
        domain=domain,
        stack=stack,
        stack_id=stack_id,
        version=site.get("version"),
        domain_dir=domain_dir,
        db_info=site.get("db"),
        site_id=site_id,
    )
    site["container"] = cname
    site["container_id"] = container.id
    site["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_site(site)
    return site


def _site_files_path(site: dict[str, Any]) -> Path | None:
    """Return the site file tree if it sits under the owner's domains directory."""
    username = str(site.get("username") or "")
    if not username:
        return None
    domains_root = (user_home(username) / "domains").resolve()
    candidates: list[Path] = []
    raw = str(site.get("path") or "").strip()
    if raw:
        candidates.append(Path(raw))
    domain = str(site.get("domain") or "").strip()
    if domain:
        candidates.append(user_home(username) / "domains" / domain)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(domains_root)
        except (OSError, ValueError):
            continue
        if resolved.exists() and resolved.is_dir():
            return resolved
    return None


def delete_site(site_id: str, *, delete_db: bool = False) -> dict[str, Any]:
    """Remove a site container, its files, and optionally its linked database."""
    site = get_site(site_id)
    if not site:
        raise ValueError("Site not found")

    client = _client()
    try:
        c = client.containers.get(site["container"])
        c.remove(force=True)
    except docker.errors.NotFound:
        pass

    files_path = _site_files_path(site)
    if files_path is not None:
        shutil.rmtree(files_path)

    db_deleted = False
    db_info = site.get("db") if isinstance(site.get("db"), dict) else None
    if delete_db and db_info and db_info.get("db_name"):
        from . import databases

        databases.delete_database(db_info)
        db_deleted = True

    path = _sites_dir() / f"{site_id}.json"
    if path.exists():
        path.unlink()

    features = load_features()
    if features.get("web") and features.get("mail"):
        try:
            from . import webmail

            webmail.sync_routes()
        except Exception:
            pass

    return {
        "domain": site.get("domain"),
        "files_deleted": files_path is not None,
        "db_deleted": db_deleted,
        "db_name": (db_info or {}).get("db_name") if db_deleted else None,
    }


def stop_site(site_id: str, *, delete_db: bool = False) -> dict[str, Any]:
    """Backward-compatible alias for delete_site."""
    return delete_site(site_id, delete_db=delete_db)


def new_db_credentials(prefix: str = "site") -> dict[str, str]:
    name = f"{prefix}_" + secrets.token_hex(4)
    user = name[:16]
    password = secrets.token_urlsafe(16)
    return {"db_name": name, "db_user": user, "db_password": password}
