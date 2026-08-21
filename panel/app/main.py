from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import authenticate, is_admin, is_hosting_user
from .config import ensure_data_dirs, get_settings, load_features, save_features
from .services import (
    certs,
    databases,
    deploy_jobs,
    dns,
    domains,
    mail,
    plans,
    plugins,
    runtime,
    server_health,
    sites,
    stacks,
    users,
    webmail_sso,
)

ensure_data_dirs()
settings = get_settings()

app = FastAPI(title="mrmpanel", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie,
    same_site="lax",
    https_only=False,
)

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["site_version"] = sites.site_display_version
templates.env.globals["format_limit"] = plans.format_limit
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

assets = settings.assets_dir
if assets.exists():
    app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")


def current_user(request: Request) -> dict[str, Any] | None:
    return request.session.get("user")


def ctx(request: Request, **extra: Any) -> dict[str, Any]:
    user = current_user(request)
    admin = is_admin(user)
    hosting_users = users.list_hosting_users() if admin else []
    allowed_users = {item["username"] for item in hosting_users}
    selected_user = str(request.session.get("admin_selected_user") or "")
    if selected_user not in allowed_users:
        selected_user = hosting_users[0]["username"] if hosting_users else ""
        if admin and selected_user:
            request.session["admin_selected_user"] = selected_user
        elif admin:
            request.session.pop("admin_selected_user", None)
    return {
        "request": request,
        "user": user,
        "is_admin": admin,
        "is_user": is_hosting_user(user),
        "features": load_features(),
        "title": settings.panel_title,
        "admin_users": hosting_users,
        "selected_user": selected_user,
        **extra,
    }


def connection_status(request: Request) -> dict[str, Any]:
    """Describe the URL and transport used by this browser request."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    scheme = (forwarded_proto.split(",", 1)[0].strip() or request.url.scheme).lower()
    forwarded_host = request.headers.get("x-forwarded-host", "")
    authority = (
        forwarded_host.split(",", 1)[0].strip()
        or request.headers.get("host", "")
        or request.url.netloc
    )
    if authority.startswith("["):
        hostname = authority.split("]", 1)[0].lstrip("[")
    elif authority.count(":") == 1:
        hostname = authority.rsplit(":", 1)[0]
    else:
        hostname = authority
    return {
        "scheme": scheme,
        "secure": scheme == "https",
        "authority": authority,
        "hostname": hostname.lower().rstrip("."),
        "url": f"{scheme}://{authority}/",
    }


def require_admin(request: Request) -> dict[str, Any] | RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse("/u/", status_code=303)
    return user


def require_hosting_user(request: Request) -> dict[str, Any] | RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_hosting_user(user):
        return RedirectResponse("/", status_code=303)
    return user


def selected_admin_user(request: Request) -> str:
    """Selected account scope; default to the first hosting user."""
    selected = str(request.session.get("admin_selected_user") or "")
    if selected and users.get_hosting_user(selected):
        return selected
    hosting_users = users.list_hosting_users()
    if hosting_users:
        selected = hosting_users[0]["username"]
        request.session["admin_selected_user"] = selected
        return selected
    request.session.pop("admin_selected_user", None)
    return ""


# ── Auth ──────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = current_user(request)
    if user:
        if is_admin(user):
            return RedirectResponse("/", status_code=303)
        return RedirectResponse("/u/", status_code=303)
    return templates.TemplateResponse("login.html", ctx(request, error=None))


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    user = authenticate(username.strip(), password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            ctx(request, error="Invalid username or password"),
            status_code=401,
        )
    request.session["user"] = user
    if is_admin(user):
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/u/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/admin/select-user")
async def admin_select_user(
    request: Request,
    username: str = Form(""),
    next_url: str = Form("/"),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = username.strip()
    if username and not users.get_hosting_user(username):
        hosting_users = users.list_hosting_users()
        username = hosting_users[0]["username"] if hosting_users else ""
    if username:
        request.session["admin_selected_user"] = username
    else:
        request.session.pop("admin_selected_user", None)
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    return RedirectResponse(next_url, status_code=303)


# ── Admin routes ───────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    features = load_features()
    hostname_ssl = certs.hostname_ssl_status(features.get("hostname") or "")
    connection = connection_status(request)
    flash_ok = request.session.pop("dash_ok", None)
    flash_error = request.session.pop("dash_error", None)
    mail_svc = runtime.mail_service_status() if features.get("mail") else None
    gaps = runtime.mail_security_gaps() if features.get("mail") else []
    return templates.TemplateResponse(
        "dashboard.html",
        ctx(
            request,
            hostname_ssl=hostname_ssl,
            connection=connection,
            network_health=server_health.network_health(),
            mail_service=mail_svc,
            site_runtime=runtime.site_runtime_rows() if features.get("web") else [],
            mail_security_gaps=gaps,
            flash_ok=flash_ok,
            flash_error=flash_error,
        ),
    )


@app.post("/services/mail/{action}")
async def mail_service_control(request: Request, action: str):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    if not load_features().get("mail"):
        request.session["dash_error"] = "Mail is not installed on this server"
        return RedirectResponse("/", status_code=303)
    try:
        result = runtime.control_mail(action)
        if result.get("ok"):
            request.session["dash_ok"] = f"Mail container: {action} succeeded"
        else:
            request.session["dash_error"] = result.get("error") or f"Mail {action} failed"
    except Exception as exc:
        request.session["dash_error"] = str(exc)
    return RedirectResponse("/", status_code=303)


@app.post("/services/sites/{site_id}/{action}")
async def site_service_control(request: Request, site_id: str, action: str):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    try:
        result = runtime.control_site(site_id, action)
        domain = result.get("domain") or site_id
        if result.get("ok"):
            request.session["dash_ok"] = f"{domain}: {action} succeeded"
        else:
            request.session["dash_error"] = (
                f"{domain}: {result.get('error') or action + ' failed'}"
            )
    except Exception as exc:
        request.session["dash_error"] = str(exc)
    return RedirectResponse("/", status_code=303)


@app.post("/api/ssl/activate")
async def api_ssl_activate(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return {"error": "unauthorized"}

    features = load_features()
    hostname = str(features.get("hostname") or "").strip().lower()
    job = deploy_jobs.create_job("activate_hostname_ssl", {"hostname": hostname})

    def work(progress: deploy_jobs.ProgressFn) -> dict[str, Any]:
        return certs.activate_hostname_ssl(progress)

    deploy_jobs.run_in_background(job["id"], work)
    return {"job_id": job["id"]}


@app.get("/api/ssl/jobs/{job_id}")
async def api_ssl_job(request: Request, job_id: str):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return {"error": "unauthorized"}
    job = deploy_jobs.get_job(job_id)
    if not job or job.get("kind") != "activate_hostname_ssl":
        return {"error": "not found"}
    return job


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    return templates.TemplateResponse(
        "users.html",
        ctx(
            request,
            hosting_users=_users_with_quotas(),
            plan_list=plans.list_plans(),
            error=None,
            ok=None,
        ),
    )


def _users_with_quotas() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for u in users.list_hosting_users():
        row = dict(u)
        try:
            row["quota"] = plans.user_quota_view(u["username"])
        except Exception:
            row["quota"] = None
        out.append(row)
    return out


@app.post("/users")
async def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    plan_id: str = Form("unlimited"),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    try:
        users.create_hosting_user(
            username.strip(),
            password,
            display_name.strip(),
            plan_id=plan_id.strip() or plans.DEFAULT_PLAN_ID,
        )
        ok = f"User {username} created — they can sign in at /login"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "users.html",
        ctx(
            request,
            hosting_users=_users_with_quotas(),
            plan_list=plans.list_plans(),
            error=error,
            ok=ok,
        ),
    )


@app.post("/users/{username}/plan")
async def users_set_plan(request: Request, username: str, plan_id: str = Form(...)):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    try:
        plan = plans.set_user_plan(username, plan_id.strip())
        ok = f"Assigned {username} to plan {plan['name']}"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "users.html",
        ctx(
            request,
            hosting_users=_users_with_quotas(),
            plan_list=plans.list_plans(),
            error=error,
            ok=ok,
        ),
    )


@app.post("/plans")
async def plans_create(
    request: Request,
    name: str = Form(...),
    disk_gb: str = Form(""),
    domains_limit: str = Form(""),
    sites_limit: str = Form(""),
    mailboxes_limit: str = Form(""),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    try:
        plan = plans.create_plan(
            name.strip(),
            disk_gb=disk_gb.strip() or None,
            domains=domains_limit.strip() or None,
            sites=sites_limit.strip() or None,
            mailboxes=mailboxes_limit.strip() or None,
        )
        ok = f"Plan {plan['name']} created"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "users.html",
        ctx(
            request,
            hosting_users=_users_with_quotas(),
            plan_list=plans.list_plans(),
            error=error,
            ok=ok,
        ),
    )


@app.post("/plans/{plan_id}/delete")
async def plans_delete(request: Request, plan_id: str):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    try:
        plans.delete_plan(plan_id)
        ok = f"Plan {plan_id} deleted"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "users.html",
        ctx(
            request,
            hosting_users=_users_with_quotas(),
            plan_list=plans.list_plans(),
            error=error,
            ok=ok,
        ),
    )


@app.post("/plans/{plan_id}/update")
async def plans_update(
    request: Request,
    plan_id: str,
    name: str = Form(...),
    disk_gb: str = Form(""),
    domains_limit: str = Form(""),
    sites_limit: str = Form(""),
    mailboxes_limit: str = Form(""),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    try:
        plan = plans.update_plan(
            plan_id,
            name=name.strip(),
            disk_gb=disk_gb.strip() or None,
            domains=domains_limit.strip() or None,
            sites=sites_limit.strip() or None,
            mailboxes=mailboxes_limit.strip() or None,
        )
        ok = f"Plan {plan['name']} updated"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "users.html",
        ctx(
            request,
            hosting_users=_users_with_quotas(),
            plan_list=plans.list_plans(),
            error=error,
            ok=ok,
        ),
    )


def _domain_records_ctx(
    domain_list: list[dict[str, Any]],
    selected_domain: str = "",
) -> dict[str, Any]:
    """Build a DNS record view restricted to the domains in the current scope."""
    allowed = [str(item.get("domain") or "").lower() for item in domain_list]
    selected = selected_domain.strip().rstrip(".").lower()
    if selected not in allowed:
        selected = allowed[0] if allowed else ""

    rows: list[dict[str, Any]] = []
    dns_records_error = ""
    dns_records_source = ""
    if selected:
        features = load_features()
        if not features.get("dns"):
            dns_records_source = "external"
        else:
            try:
                if dns.zone_exists(selected):
                    rows = dns.zone_record_rows(selected)
                    dns_records_source = "this-server"
                else:
                    dns_records_source = "external"
            except Exception as exc:
                dns_records_error = str(exc)

    return {
        "record_domain": selected,
        "dns_record_rows": rows,
        "dns_records_source": dns_records_source,
        "dns_records_error": dns_records_error,
    }


@app.get("/domains", response_class=HTMLResponse)
async def domains_page(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    selected = selected_admin_user(request)
    domain_list = (
        domains.list_domains_for_user(selected) if selected else domains.list_domains()
    )
    return templates.TemplateResponse(
        "domains.html",
        ctx(
            request,
            domain_list=domain_list,
            hosting_users=users.list_hosting_users(),
            error=None,
            ok=None,
            **_domain_records_ctx(
                domain_list,
                request.query_params.get("domain") or "",
            ),
        ),
    )


@app.post("/domains", response_class=HTMLResponse)
async def domains_create(
    request: Request,
    domain: str = Form(...),
    username: str = Form(""),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    selected = selected_admin_user(request)
    owner = selected or username.strip()
    error = ok = None
    try:
        meta = domains.add_domain(domain, owner, allow_delegation=True)
        ok = f"Domain {meta['domain']} assigned to {owner}"
        if meta.get("dns_error"):
            ok += f" (DNS warning: {meta['dns_error']})"
    except Exception as exc:
        error = str(exc)
    domain_list = (
        domains.list_domains_for_user(selected) if selected else domains.list_domains()
    )
    return templates.TemplateResponse(
        "domains.html",
        ctx(
            request,
            domain_list=domain_list,
            hosting_users=users.list_hosting_users(),
            error=error,
            ok=ok,
            **_domain_records_ctx(domain_list, meta["domain"] if ok else domain),
        ),
    )


@app.post("/domains/{domain_name}/delete")
async def domains_delete(request: Request, domain_name: str):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    selected = selected_admin_user(request)
    meta = domains.get_domain(domain_name)
    if meta and (not selected or meta.get("username") == selected):
        try:
            domains.delete_domain(domain_name)
        except ValueError:
            return RedirectResponse("/domains?delete=blocked", status_code=303)
    return RedirectResponse("/domains", status_code=303)


def _admin_sites_ctx(
    request: Request,
    error: str | None = None,
    ok: str | None = None,
) -> dict[str, Any]:
    selected = selected_admin_user(request)
    return ctx(
        request,
        site_list=(
            sites.list_sites_for_user(selected) if selected else sites.list_sites()
        ),
        hosting_users=users.list_hosting_users(),
        managed_domains=(
            domains.list_domains_for_user(selected)
            if selected
            else domains.list_domains()
        ),
        stack_list=stacks.list_stacks(),
        error=error,
        ok=ok,
    )


def site_hostname(managed_domain: str, site_kind: str, subdomain: str = "") -> str:
    base = domains.normalize_domain(managed_domain)
    if site_kind == "domain":
        return base
    if site_kind != "subdomain":
        raise ValueError("Choose main domain or subdomain")
    label = subdomain.strip().strip(".").lower()
    if not label:
        raise ValueError("Enter a subdomain name")
    return domains.normalize_domain(f"{label}.{base}")


@app.get("/sites", response_class=HTMLResponse)
async def sites_page(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    return templates.TemplateResponse(
        "sites.html",
        _admin_sites_ctx(request),
    )


@app.post("/sites")
async def sites_create(
    request: Request,
    username: str = Form(""),
    managed_domain: str = Form(...),
    site_kind: str = Form("domain"),
    subdomain: str = Form(""),
    stack_id: str = Form(...),
    create_db: str = Form("auto"),
    version: str = Form(""),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    try:
        owner = selected_admin_user(request) or username.strip()
        domain = site_hostname(managed_domain, site_kind, subdomain)
        _deploy(owner, domain, stack_id, create_db, version)
        ok = f"Deployed {domain} ({stack_id}) — open http://{domain}"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "sites.html",
        _admin_sites_ctx(request, error=error, ok=ok),
    )


@app.post("/api/sites/deploy")
async def api_sites_deploy(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return {"error": "unauthorized"}
    form = await request.form()
    username = selected_admin_user(request) or str(form.get("username", "")).strip()
    try:
        domain = site_hostname(
            str(form.get("managed_domain", "")),
            str(form.get("site_kind", "domain")),
            str(form.get("subdomain", "")),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    stack_id = str(form.get("stack_id", "")).strip()
    create_db = str(form.get("create_db", "auto"))
    version = str(form.get("version", ""))
    app_opts = {
        "wp_admin_user": str(form.get("wp_admin_user", "")).strip(),
        "wp_admin_password": str(form.get("wp_admin_password", "")).strip(),
        "wp_title": str(form.get("wp_title", "")).strip(),
        "wp_admin_email": str(form.get("wp_admin_email", "")).strip(),
        "app_name": str(form.get("app_name", "")).strip(),
    }
    if not username or not domain or not stack_id:
        return {"error": "username, domain, and stack_id are required"}

    job = deploy_jobs.create_job(
        "deploy_site",
        {"username": username, "domain": domain, "stack_id": stack_id},
    )

    def work(progress: deploy_jobs.ProgressFn) -> dict[str, Any]:
        return _deploy(
            username,
            domain,
            stack_id,
            create_db,
            version,
            progress=progress,
            app_opts=app_opts,
        )

    deploy_jobs.run_in_background(job["id"], work)
    return {"job_id": job["id"]}


@app.get("/api/sites/deploy/{job_id}")
async def api_sites_deploy_status(request: Request, job_id: str):
    user = current_user(request)
    if not user:
        return {"error": "unauthorized"}
    job = deploy_jobs.get_job(job_id)
    if not job:
        return {"error": "not found"}
    # Hosting users may only see jobs for themselves
    if is_hosting_user(user) and not is_admin(user):
        if job.get("meta", {}).get("username") != user.get("username"):
            return {"error": "forbidden"}
    return job


@app.post("/sites/{site_id}/ssl", response_class=HTMLResponse)
async def sites_enable_ssl(request: Request, site_id: str):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    try:
        existing = sites.get_site(site_id)
        selected = selected_admin_user(request)
        if selected and (not existing or existing.get("username") != selected):
            raise ValueError("Site is outside the selected account scope")
        site = sites.refresh_site_tls(site_id)
        ok = (
            f"Requested a free SSL certificate for {site['domain']}. "
            f"Give it about a minute, then open https://{site['domain']}"
        )
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "sites.html",
        _admin_sites_ctx(request, error=error, ok=ok),
    )


@app.post("/sites/{site_id}/delete")
async def sites_delete(
    request: Request,
    site_id: str,
    delete_db: str = Form(""),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    existing = sites.get_site(site_id)
    selected = selected_admin_user(request)
    error = None
    ok = None
    if existing and (not selected or existing.get("username") == selected):
        try:
            result = sites.delete_site(site_id, delete_db=delete_db in {"1", "true", "on", "yes"})
            domain = result.get("domain") or site_id
            if result.get("db_deleted"):
                ok = f"Deleted {domain}, its files, and database {result.get('db_name')}."
            else:
                ok = f"Deleted {domain} and its files."
                if existing.get("db"):
                    ok += " Linked database was kept."
        except Exception as e:
            error = str(e)
    return templates.TemplateResponse(
        "sites.html",
        _admin_sites_ctx(request, error=error, ok=ok),
    )


@app.get("/databases", response_class=HTMLResponse)
async def databases_page(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    features = load_features()
    if not features.get("mariadb") and not features.get("postgres"):
        return templates.TemplateResponse(
            "module_disabled.html",
            ctx(request, module="Databases", reason="No SQL engine was selected during install."),
        )
    selected = selected_admin_user(request)
    return templates.TemplateResponse(
        "databases.html",
        ctx(
            request,
            error=None,
            ok=None,
            created=None,
            database_list=databases.list_databases(selected),
            hosting_users=users.list_hosting_users(),
        ),
    )


@app.post("/databases")
async def databases_create(
    request: Request,
    engine: str = Form(...),
    prefix: str = Form("db"),
    username: str = Form(""),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    created = None
    try:
        owner = selected_admin_user(request) or username.strip()
        if not users.get_hosting_user(owner):
            raise ValueError("Choose a hosting user for this database")
        created = databases.create_database(engine, prefix=prefix)
        databases.register_database(created, owner)
        ok = "Database created"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "databases.html",
        ctx(
            request,
            error=error,
            ok=ok,
            created=created,
            database_list=databases.list_databases(selected_admin_user(request)),
            hosting_users=users.list_hosting_users(),
        ),
    )


def _admin_mail_ctx(
    request: Request,
    selected_domain: str = "",
    error: str | None = None,
    ok: str | None = None,
    dkim_result: str | None = None,
) -> dict[str, Any]:
    selected_user = selected_admin_user(request)
    managed = (
        domains.list_domains_for_user(selected_user)
        if selected_user
        else domains.list_domains()
    )
    domain_names = [item["domain"] for item in managed]
    selected_domain = selected_domain.lower().strip()
    if selected_domain not in domain_names:
        selected_domain = domain_names[0] if domain_names else ""
    status = (
        mail.user_domain_warnings(selected_user, domain_names)
        if selected_user
        else mail.mail_status()
    )
    return ctx(
        request,
        managed_domains=managed,
        domains=domain_names,
        selected_domain=selected_domain,
        mail_status=status,
        guidance=mail.dns_guidance(selected_domain) if selected_domain else None,
        mailboxes=mail.list_mailboxes_for_domains(domain_names),
        error=error,
        ok=ok,
        dkim_result=dkim_result,
    )


@app.get("/mail", response_class=HTMLResponse)
async def mail_page(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    features = load_features()
    if not features.get("mail"):
        return templates.TemplateResponse(
            "module_disabled.html",
            ctx(
                request,
                module="Mail",
                reason="Mail was not selected during install.",
            ),
        )
    return templates.TemplateResponse(
        "mail.html",
        _admin_mail_ctx(request, request.query_params.get("domain") or ""),
    )


@app.post("/mail/mailbox")
async def mail_add_mailbox(
    request: Request,
    local_part: str = Form(...),
    domain: str = Form(...),
    password: str = Form(...),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    email = ""
    try:
        email = mail.mailbox_address(local_part, domain)
        selected = selected_admin_user(request)
        allowed = domains.domain_names_for_user(selected) if selected else [
            item["domain"] for item in domains.list_domains()
        ]
        if domain.strip().lower() not in allowed:
            raise ValueError("Mailbox domain is outside the selected account scope")
        owner_meta = domains.get_domain(domain)
        if not owner_meta:
            raise ValueError("Mailbox domain is not managed by an account")
        mail.add_mailbox(email, password, username=owner_meta["username"])
        path = mail.mailbox_storage_path(email.strip())
        ok = f"Mailbox {email} added"
        if path:
            ok += f" → {path}"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "mail.html",
        _admin_mail_ctx(request, domain, error=error, ok=ok),
    )


@app.post("/mail/dkim")
async def mail_dkim(request: Request, domain: str = Form(...)):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    dkim_result = None
    try:
        selected = selected_admin_user(request)
        allowed = domains.domain_names_for_user(selected) if selected else [
            item["domain"] for item in domains.list_domains()
        ]
        domain = domain.strip().lower()
        if domain not in allowed:
            raise ValueError("DKIM domain is outside the selected account scope")
        dkim_result = mail.enable_dkim(domain)
        ok = "DKIM enabled — publish the TXT record below"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "mail.html",
        _admin_mail_ctx(
            request,
            domain.strip(),
            error=error,
            ok=ok,
            dkim_result=dkim_result,
        ),
    )


@app.post("/mail/dismiss/{key}")
async def mail_dismiss(request: Request, key: str):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    mail.dismiss_warning(key)
    return RedirectResponse("/mail", status_code=303)


@app.post("/mail/webmail-sso")
async def mail_webmail_sso(request: Request, email: str = Form(...)):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    email = email.strip().lower()
    selected = selected_admin_user(request)
    allowed = domains.domain_names_for_user(selected) if selected else [
        item["domain"] for item in domains.list_domains()
    ]
    domain = email.split("@", 1)[1] if "@" in email else ""
    try:
        if "@" not in email:
            raise ValueError("Invalid mailbox address")
        if domain not in {d.lower() for d in allowed}:
            raise ValueError("Mailbox is outside the selected account scope")
        known = {m.lower() for m in mail.list_mailboxes_for_domains(allowed)}
        if email not in known:
            raise ValueError("Unknown mailbox")
        return RedirectResponse(webmail_sso.webmail_sso_url(email), status_code=303)
    except Exception as e:
        return templates.TemplateResponse(
            "mail.html",
            _admin_mail_ctx(request, domain, error=str(e)),
        )


@app.post("/warnings/dismiss/{key}")
async def warn_dismiss(request: Request, key: str):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    mail.dismiss_warning(key)
    return RedirectResponse("/", status_code=303)


@app.get("/plugins", response_class=HTMLResponse)
async def plugins_page(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    hosting = users.list_hosting_users()
    plugin_user = (
        selected_admin_user(request)
        or request.query_params.get("user")
        or (hosting[0]["username"] if hosting else "")
    )
    plugin_list = plugins.list_plugins(plugin_user) if plugin_user else []
    return templates.TemplateResponse(
        "plugins.html",
        ctx(
            request,
            hosting_users=hosting,
            plugin_user=plugin_user,
            plugin_list=plugin_list,
            error=None,
            ok=None,
            output=None,
        ),
    )


@app.post("/plugins/install")
async def plugins_install(
    request: Request,
    username: str = Form(...),
    name: str = Form(...),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    try:
        plugins.install_plugin_stub(username, name.strip())
        ok = f"Plugin {name} installed under home jail"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "plugins.html",
        ctx(
            request,
            hosting_users=users.list_hosting_users(),
            plugin_user=username,
            plugin_list=plugins.list_plugins(username),
            error=error,
            ok=ok,
            output=None,
        ),
    )


@app.post("/plugins/run")
async def plugins_run(
    request: Request,
    username: str = Form(...),
    name: str = Form(...),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    error = ok = None
    output = None
    try:
        output = plugins.run_plugin(username, name.strip())
        ok = "Plugin finished"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "plugins.html",
        ctx(
            request,
            hosting_users=users.list_hosting_users(),
            plugin_user=username,
            plugin_list=plugins.list_plugins(username),
            error=error,
            ok=ok,
            output=output,
        ),
    )


def _settings_ctx(
    request: Request,
    *,
    error: str | None = None,
    ok: str | None = None,
    dns_report: dict[str, Any] | None = None,
    dns_failed: bool = False,
    security_actions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    features = load_features()
    pdns_ok = True
    if features.get("dns"):
        pdns_ok = dns.pdns_reachable()
    domain_names = [item["domain"] for item in domains.list_domains()]
    security_audit = (
        mail.mail_security_audit(domain_names)
        if features.get("mail") and domain_names
        else []
    )
    return ctx(
        request,
        error=error,
        ok=ok,
        pdns_ok=pdns_ok,
        dns_report=dns_report,
        dns_failed=dns_failed,
        security_audit=security_audit,
        security_actions=security_actions or [],
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    return templates.TemplateResponse(
        "settings.html",
        _settings_ctx(request),
    )


@app.post("/settings/mail-security", response_class=HTMLResponse)
async def settings_mail_security(
    request: Request,
    enable_spf: str = Form(""),
    enable_dkim: str = Form(""),
    enable_dmarc: str = Form(""),
    dmarc_policy: str = Form("quarantine"),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    features = load_features()
    spf = enable_spf == "on"
    dkim_enabled = enable_dkim == "on"
    dmarc = enable_dmarc == "on"
    dmarc_policy = dmarc_policy.strip().lower()
    if dmarc_policy not in {"none", "quarantine", "reject"}:
        dmarc_policy = "quarantine"
    features["mail_security_spf"] = spf
    features["mail_security_dkim"] = dkim_enabled
    features["mail_security_dmarc"] = dmarc
    features["mail_dmarc_policy"] = dmarc_policy
    save_features(features)

    error = None
    actions: list[dict[str, str]] = []
    try:
        domain_names = [item["domain"] for item in domains.list_domains()]
        actions = mail.configure_mail_security(
            domain_names,
            spf=spf,
            dkim=dkim_enabled,
            dmarc=dmarc,
            dmarc_policy=dmarc_policy,
        )
        ok = f"Mail authentication checked for {len(domain_names)} domain(s)."
    except Exception as exc:
        error = str(exc)
        ok = None
    return templates.TemplateResponse(
        "settings.html",
        _settings_ctx(
            request,
            error=error,
            ok=ok,
            security_actions=actions,
        ),
    )


@app.post("/settings/dns", response_class=HTMLResponse)
async def settings_dns_save(
    request: Request,
    ns1_hostname: str = Form(...),
    ns2_hostname: str = Form(...),
    ns1_ip: str = Form(...),
    ns2_ip: str = Form(...),
):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    from .services import dns as dns_svc

    error = None
    ok = None
    try:
        dns_svc.update_ns_settings(
            ns1_hostname=ns1_hostname,
            ns2_hostname=ns2_hostname,
            ns1_ip=ns1_ip,
            ns2_ip=ns2_ip,
        )
        ok = "Nameserver settings saved and base zone updated."
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    return templates.TemplateResponse(
        "settings.html",
        _settings_ctx(request, error=error, ok=ok),
    )


@app.post("/settings/dns-debug", response_class=HTMLResponse)
async def settings_dns_debug(request: Request, domain: str = Form("")):
    gate = require_admin(request)
    if isinstance(gate, RedirectResponse):
        return gate
    from .services import dns as dns_svc

    report = None
    error = None
    dns_failed = False
    try:
        report = dns_svc.diagnose_ns_acceptance(domain.strip() or None)
        dns_failed = any(c["critical"] and not c["pass"] for c in report.get("checks", []))
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    return templates.TemplateResponse(
        "settings.html",
        _settings_ctx(
            request,
            error=error,
            dns_report=report,
            dns_failed=dns_failed,
        ),
    )


# ── Hosting user routes (/u) ───────────────────────────────────────────


@app.get("/u/", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    my_sites = sites.list_sites_for_user(username)
    domains_list = sites.user_domains(username)
    warnings = mail.user_domain_warnings(username, domains_list)
    quota = plans.user_quota_view(username)
    security_audit = []
    if load_features().get("mail") and domains_list:
        security_audit = mail.mail_security_audit(domains_list)
    flash_ok = request.session.pop("u_dash_ok", None)
    flash_error = request.session.pop("u_dash_error", None)
    flash_actions = request.session.pop("u_dash_actions", None)
    return templates.TemplateResponse(
        "user/dashboard.html",
        ctx(
            request,
            site_list=my_sites,
            domains=domains_list,
            domain_warnings=warnings,
            quota=quota,
            security_audit=security_audit,
            flash_ok=flash_ok,
            flash_error=flash_error,
            flash_actions=flash_actions,
        ),
    )


@app.post("/u/mail/security")
async def user_mail_security_enable(
    request: Request,
    domain: str = Form(...),
    enable_spf: str = Form("on"),
    enable_dkim: str = Form("on"),
    enable_dmarc: str = Form("on"),
):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    if not load_features().get("mail"):
        request.session["u_dash_error"] = "Mail is not installed"
        return RedirectResponse("/u/", status_code=303)
    try:
        name = domains.normalize_domain(domain)
    except ValueError as exc:
        request.session["u_dash_error"] = str(exc)
        return RedirectResponse("/u/", status_code=303)
    if name not in sites.user_domains(username):
        request.session["u_dash_error"] = "That domain is not assigned to your account"
        return RedirectResponse("/u/", status_code=303)
    features = load_features()
    dmarc_policy = str(features.get("mail_dmarc_policy") or "quarantine")
    try:
        actions = mail.configure_mail_security(
            [name],
            spf=enable_spf == "on",
            dkim=enable_dkim == "on",
            dmarc=enable_dmarc == "on",
            dmarc_policy=dmarc_policy,
        )
        request.session["u_dash_ok"] = f"Mail security updated for {name}"
        request.session["u_dash_actions"] = actions
    except Exception as exc:
        request.session["u_dash_error"] = str(exc)
    return RedirectResponse("/u/", status_code=303)


@app.post("/u/warnings/dismiss/{key:path}")
async def user_warn_dismiss(request: Request, key: str):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    # Only allow dismissing keys that belong to this user's domains
    domains = sites.user_domains(username)
    allowed = False
    for d in domains:
        if key.startswith(f"{d}:"):
            allowed = True
            break
    if allowed:
        mail.dismiss_warning(key, username=username)
    return RedirectResponse("/u/", status_code=303)


@app.get("/u/domains", response_class=HTMLResponse)
async def user_domains_page(request: Request):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    domain_list = domains.list_domains_for_user(gate["username"])
    return templates.TemplateResponse(
        "user/domains.html",
        ctx(
            request,
            domain_list=domain_list,
            error=None,
            ok=None,
            **_domain_records_ctx(
                domain_list,
                request.query_params.get("domain") or "",
            ),
        ),
    )


@app.post("/u/domains", response_class=HTMLResponse)
async def user_domains_create(request: Request, domain: str = Form(...)):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    error = ok = None
    try:
        meta = domains.add_domain(domain, username)
        ok = f"Domain {meta['domain']} added"
        if meta.get("dns_error"):
            ok += f" (DNS warning: {meta['dns_error']})"
    except Exception as exc:
        error = str(exc)
    domain_list = domains.list_domains_for_user(username)
    return templates.TemplateResponse(
        "user/domains.html",
        ctx(
            request,
            domain_list=domain_list,
            error=error,
            ok=ok,
            **_domain_records_ctx(domain_list, meta["domain"] if ok else domain),
        ),
    )


@app.post("/u/domains/{domain_name}/delete")
async def user_domains_delete(request: Request, domain_name: str):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    meta = domains.get_domain(domain_name)
    if meta and meta.get("username") == gate["username"]:
        try:
            domains.delete_domain(domain_name)
        except ValueError:
            return RedirectResponse("/u/domains?delete=blocked", status_code=303)
    return RedirectResponse("/u/domains", status_code=303)


@app.get("/u/sites", response_class=HTMLResponse)
async def user_sites(request: Request):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    return templates.TemplateResponse(
        "user/sites.html",
        ctx(
            request,
            site_list=sites.list_sites_for_user(username),
            managed_domains=domains.list_domains_for_user(username),
            stack_list=stacks.list_stacks(),
            error=None,
            ok=None,
        ),
    )


@app.post("/u/sites")
async def user_sites_create(
    request: Request,
    managed_domain: str = Form(...),
    site_kind: str = Form("domain"),
    subdomain: str = Form(""),
    stack_id: str = Form(...),
    create_db: str = Form("auto"),
    version: str = Form(""),
):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    error = ok = None
    try:
        domain = site_hostname(managed_domain, site_kind, subdomain)
        _deploy(username, domain, stack_id, create_db, version)
        ok = f"Deployed {domain} — open http://{domain.strip().lower()} (not :8080 / not bare IP)"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "user/sites.html",
        ctx(
            request,
            site_list=sites.list_sites_for_user(username),
            managed_domains=domains.list_domains_for_user(username),
            stack_list=stacks.list_stacks(),
            error=error,
            ok=ok,
        ),
    )


@app.post("/api/u/sites/deploy")
async def api_user_sites_deploy(request: Request):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return {"error": "unauthorized"}
    username = gate["username"]
    form = await request.form()
    try:
        domain = site_hostname(
            str(form.get("managed_domain", "")),
            str(form.get("site_kind", "domain")),
            str(form.get("subdomain", "")),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    stack_id = str(form.get("stack_id", "")).strip()
    create_db = str(form.get("create_db", "auto"))
    version = str(form.get("version", ""))
    app_opts = {
        "wp_admin_user": str(form.get("wp_admin_user", "")).strip(),
        "wp_admin_password": str(form.get("wp_admin_password", "")).strip(),
        "wp_title": str(form.get("wp_title", "")).strip(),
        "wp_admin_email": str(form.get("wp_admin_email", "")).strip(),
        "app_name": str(form.get("app_name", "")).strip(),
    }
    if not domain or not stack_id:
        return {"error": "domain and stack_id are required"}

    job = deploy_jobs.create_job(
        "deploy_site",
        {"username": username, "domain": domain, "stack_id": stack_id},
    )

    def work(progress: deploy_jobs.ProgressFn) -> dict[str, Any]:
        return _deploy(
            username,
            domain,
            stack_id,
            create_db,
            version,
            progress=progress,
            app_opts=app_opts,
        )

    deploy_jobs.run_in_background(job["id"], work)
    return {"job_id": job["id"]}


def _site_settings_ctx(
    request: Request,
    site: dict,
    php_ini: str,
    error: str | None = None,
    ok: str | None = None,
) -> dict:
    stack = stacks.get_stack(site.get("stack") or "") or {}
    managed = domains.owner_for_hostname(site["domain"])
    mail_domain = managed["domain"] if managed else site["domain"]
    return ctx(
        request,
        site=site,
        guidance=mail.dns_guidance(site["domain"]),
        mail_guidance=mail.dns_guidance(mail_domain),
        mail_domain=mail_domain,
        php_ini=php_ini,
        stack_versions=stacks.stack_versions(stack),
        current_version=sites.site_display_version(site),
        error=error,
        ok=ok,
    )


@app.get("/u/sites/{site_id}/settings", response_class=HTMLResponse)
async def user_site_settings(request: Request, site_id: str):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    site = sites.site_owned_by(site_id, username)
    if not site:
        return RedirectResponse("/u/sites", status_code=303)
    php_ini = ""
    php_path = users.user_home(username) / "config" / "php.ini"
    if php_path.exists():
        php_ini = php_path.read_text()
    return templates.TemplateResponse(
        "user/site_settings.html",
        _site_settings_ctx(request, site, php_ini),
    )


@app.post("/u/sites/{site_id}/settings")
async def user_site_settings_save(
    request: Request,
    site_id: str,
    php_ini: str = Form(""),
):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    site = sites.site_owned_by(site_id, username)
    if not site:
        return RedirectResponse("/u/sites", status_code=303)
    error = ok = None
    try:
        php_path = users.user_home(username) / "config" / "php.ini"
        php_path.parent.mkdir(parents=True, exist_ok=True)
        # Basic sanity: no path escapes in content matter; it's a config file
        php_path.write_text(php_ini)
        ok = "Settings saved"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "user/site_settings.html",
        _site_settings_ctx(request, site, php_ini, error=error, ok=ok),
    )


@app.post("/u/sites/{site_id}/version")
async def user_site_version_change(
    request: Request,
    site_id: str,
    version: str = Form(...),
):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    site = sites.site_owned_by(site_id, username)
    if not site:
        return RedirectResponse("/u/sites", status_code=303)
    php_ini = ""
    php_path = users.user_home(username) / "config" / "php.ini"
    if php_path.exists():
        php_ini = php_path.read_text()
    error = ok = None
    try:
        site = sites.change_site_version(site_id, version.strip())
        ok = f"Runtime switched to {site.get('version')} (files and database kept)"
    except Exception as e:
        error = str(e)
        site = sites.site_owned_by(site_id, username) or site
    return templates.TemplateResponse(
        "user/site_settings.html",
        _site_settings_ctx(request, site, php_ini, error=error, ok=ok),
    )


@app.post("/u/sites/{site_id}/delete")
async def user_sites_delete(
    request: Request,
    site_id: str,
    delete_db: str = Form(""),
):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    existing = sites.site_owned_by(site_id, username)
    error = None
    ok = None
    if existing:
        try:
            result = sites.delete_site(site_id, delete_db=delete_db in {"1", "true", "on", "yes"})
            domain = result.get("domain") or site_id
            if result.get("db_deleted"):
                ok = f"Deleted {domain}, its files, and database {result.get('db_name')}."
            else:
                ok = f"Deleted {domain} and its files."
                if existing.get("db"):
                    ok += " Linked database was kept."
        except Exception as e:
            error = str(e)
    features = load_features()
    return templates.TemplateResponse(
        "user/sites.html",
        ctx(
            request,
            site_list=sites.list_sites_for_user(username),
            managed_domains=domains.list_domains_for_user(username),
            stack_list=stacks.list_stacks(),
            error=error,
            ok=ok,
            features=features,
        ),
    )


@app.get("/u/mail", response_class=HTMLResponse)
async def user_mail(request: Request):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    features = load_features()
    if not features.get("mail"):
        return templates.TemplateResponse(
            "module_disabled.html",
            ctx(
                request,
                module="Mail",
                reason="Mail is not enabled on this server.",
            ),
        )
    domains = sites.user_domains(username)
    selected = request.query_params.get("domain") or (domains[0] if domains else "")
    if selected and selected not in domains:
        selected = domains[0] if domains else ""
    return templates.TemplateResponse(
        "user/mail.html",
        ctx(
            request,
            domains=domains,
            selected_domain=selected,
            guidance=mail.dns_guidance(selected) if selected else None,
            mailboxes=mail.list_mailboxes_for_domains(domains),
            domain_warnings=mail.user_domain_warnings(username, domains),
            error=None,
            ok=None,
            dkim_result=None,
        ),
    )


@app.post("/u/mail/mailbox")
async def user_mail_mailbox(
    request: Request,
    local_part: str = Form(...),
    domain: str = Form(...),
    password: str = Form(...),
):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    domains = sites.user_domains(username)
    email = ""
    error = ok = None
    try:
        email = mail.mailbox_address(local_part, domain)
        if not mail.email_domain_allowed(email.strip(), domains):
            raise ValueError(
                "Mailbox domain must match one of your domains: "
                + (", ".join(domains) if domains else "(none yet — create a site first)")
            )
        mail.add_mailbox(email.strip(), password, username=username)
        path = mail.mailbox_storage_path(email.strip())
        ok = f"Mailbox {email} added"
        if path:
            ok += f" → {path}"
    except Exception as e:
        error = str(e)
    selected = domain.strip().lower() if domain else (domains[0] if domains else "")
    return templates.TemplateResponse(
        "user/mail.html",
        ctx(
            request,
            domains=domains,
            selected_domain=selected,
            guidance=mail.dns_guidance(selected) if selected else None,
            mailboxes=mail.list_mailboxes_for_domains(domains),
            domain_warnings=mail.user_domain_warnings(username, domains),
            error=error,
            ok=ok,
            dkim_result=None,
        ),
    )


@app.post("/u/mail/dkim")
async def user_mail_dkim(request: Request, domain: str = Form(...)):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    domains = sites.user_domains(username)
    domain = domain.strip().lower()
    error = ok = None
    dkim_result = None
    try:
        if domain not in {d.lower() for d in domains}:
            raise ValueError("You can only enable DKIM for your own domains")
        dkim_result = mail.enable_dkim(domain)
        ok = "DKIM enabled — publish the TXT record below"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "user/mail.html",
        ctx(
            request,
            domains=domains,
            selected_domain=domain,
            guidance=mail.dns_guidance(domain),
            mailboxes=mail.list_mailboxes_for_domains(domains),
            domain_warnings=mail.user_domain_warnings(username, domains),
            error=error,
            ok=ok,
            dkim_result=dkim_result,
        ),
    )


@app.post("/u/mail/dismiss/{key:path}")
async def user_mail_dismiss(request: Request, key: str):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    domains = sites.user_domains(username)
    if any(key.startswith(f"{d}:") for d in domains):
        mail.dismiss_warning(key, username=username)
    return RedirectResponse("/u/mail", status_code=303)


@app.post("/u/mail/webmail-sso")
async def user_mail_webmail_sso(request: Request, email: str = Form(...)):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    user_domains = sites.user_domains(username)
    email = email.strip().lower()
    error = None
    selected = user_domains[0] if user_domains else ""
    try:
        if "@" not in email:
            raise ValueError("Invalid mailbox address")
        selected = email.split("@", 1)[1]
        if selected not in {d.lower() for d in user_domains}:
            raise ValueError("You can only open webmail for your own mailboxes")
        known = {m.lower() for m in mail.list_mailboxes_for_domains(user_domains)}
        if email not in known:
            raise ValueError("Unknown mailbox")
        return RedirectResponse(webmail_sso.webmail_sso_url(email), status_code=303)
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "user/mail.html",
        ctx(
            request,
            domains=user_domains,
            selected_domain=selected,
            guidance=mail.dns_guidance(selected) if selected else None,
            mailboxes=mail.list_mailboxes_for_domains(user_domains),
            domain_warnings=mail.user_domain_warnings(username, user_domains),
            error=error,
            ok=None,
            dkim_result=None,
        ),
    )


@app.get("/u/settings", response_class=HTMLResponse)
async def user_settings(request: Request):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    meta = users.get_hosting_user(gate["username"])
    return templates.TemplateResponse(
        "user/settings.html",
        ctx(request, meta=meta, error=None, ok=None),
    )


@app.post("/u/settings")
async def user_settings_save(
    request: Request,
    display_name: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    error = ok = None
    try:
        meta = users.get_hosting_user(username)
        if not meta:
            raise ValueError("User not found")
        if display_name.strip():
            meta["display_name"] = display_name.strip()
            users.save_hosting_user(meta)
            request.session["user"]["display_name"] = meta["display_name"]
        if password:
            if password != password2:
                raise ValueError("Passwords do not match")
            if len(password) < 8:
                raise ValueError("Password must be at least 8 characters")
            users.set_panel_password(username, password)
        ok = "Settings updated"
    except Exception as e:
        error = str(e)
    meta = users.get_hosting_user(username)
    return templates.TemplateResponse(
        "user/settings.html",
        ctx(request, meta=meta, error=error, ok=ok),
    )


@app.get("/u/plugins", response_class=HTMLResponse)
async def user_plugins(request: Request):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    return templates.TemplateResponse(
        "user/plugins.html",
        ctx(
            request,
            plugin_list=plugins.list_plugins(username),
            error=None,
            ok=None,
            output=None,
        ),
    )


@app.post("/u/plugins/install")
async def user_plugins_install(request: Request, name: str = Form(...)):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    error = ok = None
    try:
        plugins.install_plugin_stub(username, name.strip())
        ok = f"Plugin {name} installed"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "user/plugins.html",
        ctx(
            request,
            plugin_list=plugins.list_plugins(username),
            error=error,
            ok=ok,
            output=None,
        ),
    )


@app.post("/u/plugins/run")
async def user_plugins_run(request: Request, name: str = Form(...)):
    gate = require_hosting_user(request)
    if isinstance(gate, RedirectResponse):
        return gate
    username = gate["username"]
    error = ok = None
    output = None
    try:
        output = plugins.run_plugin(username, name.strip())
        ok = "Plugin finished"
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse(
        "user/plugins.html",
        ctx(
            request,
            plugin_list=plugins.list_plugins(username),
            error=error,
            ok=ok,
            output=output,
        ),
    )


@app.get("/health")
async def health():
    return {"status": "ok", "features": load_features()}


def _deploy(
    username: str,
    domain: str,
    stack_id: str,
    create_db: str,
    version: str | None = None,
    progress: Any = None,
    app_opts: dict[str, str] | None = None,
) -> dict[str, Any]:
    def p(msg: str, pct: int | None = None) -> None:
        if progress:
            progress(msg, pct)

    if not users.get_hosting_user(username):
        raise ValueError(f"Hosting user not found: {username}")
    domain = domains.validate_site_hostname(username, domain)
    stack = stacks.get_stack(stack_id)
    if not stack:
        raise ValueError("Unknown stack")
    db_info = None
    needs_db = bool(stack.get("requires"))
    if create_db != "none" and (needs_db or create_db in ("mariadb", "postgres")):
        p("Provisioning database…", 8)
        engine = create_db if create_db in ("mariadb", "postgres") else None
        engine = databases.pick_engine_for_stack(stack, engine)
        if not engine and needs_db:
            raise ValueError("This stack needs a database engine that is not installed")
        if engine:
            db_info = databases.create_database(engine, prefix=username[:8])
            p(f"Database ready ({engine})", 15)
    elif needs_db and create_db == "none":
        raise ValueError("This stack requires a database")
    ver = (version or "").strip() or None
    return sites.deploy_site(
        username,
        domain.strip().lower(),
        stack_id,
        db_info,
        version=ver,
        progress=progress,
        app_opts=app_opts,
    )
