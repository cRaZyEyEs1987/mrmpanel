from __future__ import annotations

from typing import Any

import yaml

from ..config import get_settings, load_features


def list_stacks(available_only: bool = True) -> list[dict[str, Any]]:
    features = load_features()
    stacks_dir = get_settings().stacks_dir
    results: list[dict[str, Any]] = []
    if not stacks_dir.exists():
        return results
    for path in sorted(stacks_dir.glob("*.yml")):
        data = yaml.safe_load(path.read_text()) or {}
        data["_path"] = str(path)
        requires = data.get("requires") or []
        if available_only:
            ok = True
            for req in requires:
                if req == "mariadb" and not features.get("mariadb"):
                    ok = False
                if req == "postgres" and not features.get("postgres"):
                    ok = False
            if not ok:
                continue
        results.append(data)
    return results


def get_stack(stack_id: str) -> dict[str, Any] | None:
    for s in list_stacks(available_only=False):
        if s.get("id") == stack_id:
            return s
    return None


def stack_available(stack: dict[str, Any]) -> bool:
    features = load_features()
    for req in stack.get("requires") or []:
        if not features.get(req):
            return False
    return True


def stack_versions(stack: dict[str, Any]) -> list[str]:
    versions = stack.get("versions") or []
    return [str(v) for v in versions]


def default_version(stack: dict[str, Any]) -> str | None:
    if stack.get("default_version") is not None:
        return str(stack["default_version"])
    versions = stack_versions(stack)
    return versions[-1] if versions else None


def stack_app_versions(stack: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize app_versions entries to ``{id, php: [...]}``."""
    raw = stack.get("app_versions") or []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        app_id = str(entry.get("id") or "").strip()
        if not app_id:
            continue
        php = [str(p) for p in (entry.get("php") or [])]
        out.append({"id": app_id, "php": php})
    return out


def default_app_version(stack: dict[str, Any]) -> str | None:
    if stack.get("default_app_version") is not None:
        return str(stack["default_app_version"])
    apps = stack_app_versions(stack)
    return apps[0]["id"] if apps else None


def app_versions_for_php(stack: dict[str, Any], php: str) -> list[str]:
    """Return app version ids that list ``php`` as supported."""
    php = str(php)
    return [a["id"] for a in stack_app_versions(stack) if php in a["php"]]


def app_versions_matrix(stack: dict[str, Any]) -> dict[str, list[str]]:
    """Map app id → allowed PHP versions (for UI data attributes)."""
    return {a["id"]: list(a["php"]) for a in stack_app_versions(stack)}


def validate_app_version(
    stack: dict[str, Any], php: str | None, app: str | None
) -> str | None:
    """Validate or default app version for stacks that define ``app_versions``.

    Returns the chosen app id, or None when the stack has no app versions.
    """
    apps = stack_app_versions(stack)
    if not apps:
        return None
    chosen_php = str(php or default_version(stack) or "")
    if not chosen_php:
        raise ValueError(f"Stack {stack.get('id')} requires a runtime version")
    allowed = app_versions_for_php(stack, chosen_php)
    if not allowed:
        raise ValueError(
            f"No app versions support PHP {chosen_php} for stack {stack.get('id')}"
        )
    chosen = (app or default_app_version(stack) or "").strip()
    if not chosen:
        chosen = allowed[0]
    if chosen not in {a["id"] for a in apps}:
        raise ValueError(
            f"App version {chosen!r} is not allowed for stack {stack.get('id')} "
            f"(allowed: {', '.join(a['id'] for a in apps)})"
        )
    if chosen not in allowed:
        raise ValueError(
            f"App version {chosen!r} does not support PHP {chosen_php} "
            f"(supported: {', '.join(allowed)})"
        )
    return chosen


def resolve_image(
    stack: dict[str, Any],
    version: str | None = None,
    app_version: str | None = None,
) -> str:
    """Resolve stack image template with runtime and optional app version.

    Fixed-image stacks (no ``versions``) ignore ``version`` and return ``image`` as-is.
    Versioned stacks require a version from ``versions`` (or the stack default).
    Templates with ``{app}`` also require a validated app version.
    """
    image = str(stack.get("image") or "")
    versions = stack_versions(stack)
    if not versions:
        return image

    chosen = version or default_version(stack)
    if not chosen:
        raise ValueError(f"Stack {stack.get('id')} requires a runtime version")
    chosen = str(chosen)
    if chosen not in versions:
        raise ValueError(
            f"Version {chosen!r} is not allowed for stack {stack.get('id')} "
            f"(allowed: {', '.join(versions)})"
        )

    needs_app = "{app}" in image or bool(stack_app_versions(stack))
    if needs_app and "{app}" in image:
        app = validate_app_version(stack, chosen, app_version)
        if not app:
            raise ValueError(f"Stack {stack.get('id')} requires an app version")
        image = image.replace("{app}", app)

    if "{version}" not in image:
        return image
    return image.replace("{version}", chosen)
