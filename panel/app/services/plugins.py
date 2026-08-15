from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ..config import get_settings
from .users import user_home

SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


def _jail_path(username: str, rel: str) -> Path:
    """Resolve path strictly under the user's home. Reject escapes."""
    home = user_home(username).resolve()
    # Disallow absolute and .. components before resolve
    if rel.startswith("/") or ".." in Path(rel).parts:
        raise PermissionError("Path escapes home directory")
    target = (home / rel).resolve()
    if not str(target).startswith(str(home) + os.sep) and target != home:
        raise PermissionError("Path escapes home directory")
    return target


def plugins_root(username: str) -> Path:
    root = user_home(username) / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    return root


def list_plugins(username: str) -> list[dict]:
    root = plugins_root(username)
    items = []
    for p in sorted(root.iterdir()):
        if p.is_dir():
            items.append({"name": p.name, "path": str(p.relative_to(user_home(username)))})
    return items


def install_plugin_stub(username: str, name: str) -> Path:
    if not SAFE_NAME.match(name):
        raise ValueError("Invalid plugin name")
    dest = _jail_path(username, f"plugins/{name}")
    dest.mkdir(parents=True, exist_ok=True)
    readme = dest / "README.md"
    if not readme.exists():
        readme.write_text(
            f"# Plugin {name}\n\nRuns only inside this user's home jail.\n"
        )
    entry = dest / "run.sh"
    if not entry.exists():
        entry.write_text("#!/bin/sh\necho \"plugin ok\"\n")
        entry.chmod(0o750)
    return dest


def run_plugin(username: str, name: str) -> str:
    if not SAFE_NAME.match(name):
        raise ValueError("Invalid plugin name")
    script = _jail_path(username, f"plugins/{name}/run.sh")
    if not script.exists():
        raise FileNotFoundError("Plugin run.sh not found")
    home = user_home(username)
    # Prefer bubblewrap if available; else run as user with cwd jailed
    if subprocess.run(["which", "bwrap"], capture_output=True).returncode == 0:
        cmd = [
            "bwrap",
            "--die-with-parent",
            "--unshare-pid",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--bind",
            str(home),
            str(home),
            "--chdir",
            str(script.parent),
            "--",
            "/bin/sh",
            str(script),
        ]
    else:
        cmd = ["su", "-", username, "-c", f"cd {script.parent} && /bin/sh ./run.sh"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(out or f"exit {r.returncode}")
    return out
