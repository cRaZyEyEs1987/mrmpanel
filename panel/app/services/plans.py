from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import get_settings

PLAN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

SEED_PLANS: list[dict[str, Any]] = [
    {
        "id": "starter",
        "name": "Starter",
        "disk_gb": 10,
        "domains": 1,
        "sites": 1,
        "mailboxes": 5,
        "builtin": True,
    },
    {
        "id": "business",
        "name": "Business",
        "disk_gb": 50,
        "domains": 5,
        "sites": 5,
        "mailboxes": 25,
        "builtin": True,
    },
    {
        "id": "unlimited",
        "name": "Unlimited",
        "disk_gb": None,
        "domains": None,
        "sites": None,
        "mailboxes": None,
        "builtin": True,
    },
]

DEFAULT_PLAN_ID = "unlimited"


def _path() -> Path:
    return get_settings().data_dir / "plans.json"


def _normalize_limit(value: Any) -> int | None:
    if value is None or value == "" or str(value).strip().lower() in {"infinite", "unlimited", "null"}:
        return None
    n = int(value)
    if n < 0:
        raise ValueError("Limits cannot be negative")
    return n


def _normalize_plan(raw: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(raw.get("id") or "").strip().lower()
    if not PLAN_ID_RE.fullmatch(plan_id):
        raise ValueError("Plan id must be 2–32 chars: lowercase, digit, underscore, hyphen")
    name = str(raw.get("name") or plan_id).strip() or plan_id
    return {
        "id": plan_id,
        "name": name,
        "disk_gb": _normalize_limit(raw.get("disk_gb")),
        "domains": _normalize_limit(raw.get("domains")),
        "sites": _normalize_limit(raw.get("sites")),
        "mailboxes": _normalize_limit(raw.get("mailboxes")),
        "builtin": bool(raw.get("builtin")),
        "created_at": raw.get("created_at")
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def ensure_plans() -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        plans = [_normalize_plan(dict(p)) for p in SEED_PLANS]
        try:
            save_plans(plans)
        except OSError:
            return plans
        return plans
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        plans = [_normalize_plan(dict(p)) for p in SEED_PLANS]
        try:
            save_plans(plans)
        except OSError:
            pass
        return plans
    if isinstance(data, dict) and "plans" in data:
        raw_list = data["plans"]
    elif isinstance(data, list):
        raw_list = data
    else:
        raw_list = []
    plans = []
    seen: set[str] = set()
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        try:
            plan = _normalize_plan(item)
        except ValueError:
            continue
        if plan["id"] in seen:
            continue
        seen.add(plan["id"])
        plans.append(plan)
    # Ensure seed plans exist (by id) without wiping custom plans
    changed = False
    for seed in SEED_PLANS:
        if seed["id"] not in seen:
            plans.append(_normalize_plan(dict(seed)))
            seen.add(seed["id"])
            changed = True
    if changed:
        try:
            save_plans(plans)
        except OSError:
            pass
    return plans


def save_plans(plans: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "plans": plans}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)
    path.chmod(0o644)


def list_plans() -> list[dict[str, Any]]:
    return ensure_plans()


def get_plan(plan_id: str) -> dict[str, Any] | None:
    pid = (plan_id or "").strip().lower()
    for plan in list_plans():
        if plan["id"] == pid:
            return plan
    return None


def create_plan(
    name: str,
    *,
    disk_gb: Any = None,
    domains: Any = None,
    sites: Any = None,
    mailboxes: Any = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    plans = list_plans()
    if plan_id:
        pid = plan_id.strip().lower()
    else:
        base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:24] or "plan"
        pid = base
        n = 1
        existing = {p["id"] for p in plans}
        while pid in existing:
            n += 1
            pid = f"{base}-{n}"
    if get_plan(pid):
        raise ValueError(f"Plan {pid} already exists")
    plan = _normalize_plan(
        {
            "id": pid,
            "name": name,
            "disk_gb": disk_gb,
            "domains": domains,
            "sites": sites,
            "mailboxes": mailboxes,
            "builtin": False,
        }
    )
    plans.append(plan)
    save_plans(plans)
    return plan


def update_plan(plan_id: str, **fields: Any) -> dict[str, Any]:
    plans = list_plans()
    pid = plan_id.strip().lower()
    for i, plan in enumerate(plans):
        if plan["id"] != pid:
            continue
        updated = dict(plan)
        if "name" in fields and fields["name"] is not None:
            updated["name"] = str(fields["name"]).strip() or plan["name"]
        for key in ("disk_gb", "domains", "sites", "mailboxes"):
            if key in fields:
                updated[key] = _normalize_limit(fields[key])
        plans[i] = _normalize_plan(updated)
        save_plans(plans)
        return plans[i]
    raise ValueError("Plan not found")


def delete_plan(plan_id: str) -> None:
    from . import users

    pid = plan_id.strip().lower()
    if pid == DEFAULT_PLAN_ID:
        raise ValueError("Cannot delete the Unlimited plan")
    assigned = [
        u["username"]
        for u in users.list_hosting_users()
        if (u.get("plan_id") or DEFAULT_PLAN_ID) == pid
    ]
    if assigned:
        raise ValueError(
            f"Plan is assigned to {len(assigned)} user(s): {', '.join(assigned[:5])}"
            + ("…" if len(assigned) > 5 else "")
            + ". Reassign them first."
        )
    plans = list_plans()
    before = len(plans)
    plans = [p for p in plans if p["id"] != pid]
    if len(plans) == before:
        raise ValueError("Plan not found")
    save_plans(plans)


def format_limit(value: int | None) -> str:
    return "Infinite" if value is None else str(value)


def disk_usage_bytes(username: str) -> int:
    from .users import user_home

    home = user_home(username)
    if not home.exists():
        return 0
    try:
        proc = subprocess.run(
            ["du", "-sb", str(home)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.split()[0])
    except (IndexError, ValueError):
        return 0


def disk_usage_gb(username: str) -> float:
    return round(disk_usage_bytes(username) / (1024**3), 2)


def user_usage(username: str) -> dict[str, Any]:
    from . import domains as domains_svc
    from . import mail as mail_svc
    from . import sites as sites_svc

    domain_names = domains_svc.domain_names_for_user(username)
    site_count = len(sites_svc.list_sites_for_user(username))
    mailbox_count = 0
    if domain_names:
        try:
            mailbox_count = len(mail_svc.list_mailboxes_for_domains(domain_names))
        except Exception:
            mailbox_count = 0
    return {
        "disk_gb": disk_usage_gb(username),
        "domains": len(domain_names),
        "sites": site_count,
        "mailboxes": mailbox_count,
    }


def user_quota_view(username: str, plan_id: str | None = None) -> dict[str, Any]:
    from . import users

    meta = users.get_hosting_user(username) or {}
    pid = (plan_id or meta.get("plan_id") or DEFAULT_PLAN_ID).strip().lower()
    plan = get_plan(pid) or get_plan(DEFAULT_PLAN_ID) or SEED_PLANS[-1]
    usage = user_usage(username)
    return {
        "plan": plan,
        "usage": usage,
        "limits": {
            "disk_gb": plan.get("disk_gb"),
            "domains": plan.get("domains"),
            "sites": plan.get("sites"),
            "mailboxes": plan.get("mailboxes"),
        },
    }


def _over(limit: int | None, used: float | int) -> bool:
    if limit is None:
        return False
    return float(used) >= float(limit)


def assert_can_add_domain(username: str) -> None:
    view = user_quota_view(username)
    limit = view["limits"]["domains"]
    used = view["usage"]["domains"]
    if _over(limit, used):
        raise ValueError(
            f"Domain quota reached ({used}/{format_limit(limit)} on plan {view['plan']['name']})"
        )


def assert_can_add_site(username: str) -> None:
    view = user_quota_view(username)
    limit = view["limits"]["sites"]
    used = view["usage"]["sites"]
    if _over(limit, used):
        raise ValueError(
            f"Site quota reached ({used}/{format_limit(limit)} on plan {view['plan']['name']})"
        )
    disk_limit = view["limits"]["disk_gb"]
    disk_used = view["usage"]["disk_gb"]
    if disk_limit is not None and disk_used >= float(disk_limit):
        raise ValueError(
            f"Disk quota reached ({disk_used}/{disk_limit} GB on plan {view['plan']['name']})"
        )


def assert_can_add_mailbox(username: str) -> None:
    view = user_quota_view(username)
    limit = view["limits"]["mailboxes"]
    used = view["usage"]["mailboxes"]
    if _over(limit, used):
        raise ValueError(
            f"Mailbox quota reached ({used}/{format_limit(limit)} on plan {view['plan']['name']})"
        )


def set_user_plan(username: str, plan_id: str) -> dict[str, Any]:
    from . import users

    plan = get_plan(plan_id)
    if not plan:
        raise ValueError("Plan not found")
    meta = users.get_hosting_user(username)
    if not meta:
        raise ValueError("User not found")
    meta["plan_id"] = plan["id"]
    users.save_hosting_user(meta)
    return plan
