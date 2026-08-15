from __future__ import annotations

from pathlib import Path
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


def resolve_image(stack: dict[str, Any], version: str | None = None) -> str:
    """Resolve stack image template with a runtime version.

    Fixed-image stacks (no ``versions``) ignore ``version`` and return ``image`` as-is.
    Versioned stacks require a version from ``versions`` (or the stack default).
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
    if "{version}" not in image:
        return image
    return image.replace("{version}", chosen)
