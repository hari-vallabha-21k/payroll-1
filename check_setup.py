#!/usr/bin/env python3
"""Diagnose a local setup: python version, files, dependencies, running server.

    python check_setup.py

Prints one report covering everything that commonly goes wrong, so a problem
can be identified without a round of back-and-forth.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND_FILES = [
    "admin.html",
    "attendance.html",
    "enroll.html",
    "admin.js",
    "webauthn-client.js",
    "styles.css",
]
REQUIRED_MODULES = ["fastapi", "uvicorn", "sqlalchemy", "pydantic", "jwt", "bcrypt", "webauthn"]
OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "
problems: list[str] = []


def line(mark: str, text: str) -> None:
    print(f"[{mark}] {text}")


def check_python() -> None:
    version = sys.version_info
    text = f"Python {version.major}.{version.minor}.{version.micro} at {sys.executable}"
    if version < (3, 10):
        line(BAD, text + " - 3.10 or newer is required")
        problems.append("Install Python 3.10+ and recreate the virtual environment.")
    else:
        line(OK, text)

    if sys.prefix == sys.base_prefix:
        line(WARN, "Not running inside a virtual environment")


def check_git() -> None:
    try:
        commit = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        line(WARN, "git not available - cannot report the checked-out commit")
        return

    if commit.returncode != 0:
        line(WARN, "Not a git checkout")
        return

    line(OK, f"Checked out: {commit.stdout.strip()}")
    changed = [row for row in dirty.stdout.splitlines() if row.strip()]
    if changed:
        line(WARN, f"{len(changed)} uncommitted change(s) - `git pull` may refuse to run")
        for row in changed[:5]:
            print(f"         {row}")


def check_frontend() -> None:
    folder = ROOT / "frontend"
    if not folder.is_dir():
        line(BAD, f"frontend/ folder is missing (expected at {folder})")
        problems.append(
            "Restore the frontend: git fetch origin && "
            "git reset --hard origin/claude/determined-darwin-n9r142"
        )
        return

    missing = [name for name in FRONTEND_FILES if not (folder / name).is_file()]
    if missing:
        line(BAD, f"frontend/ is missing: {', '.join(missing)}")
        problems.append("Restore the missing frontend files with a fresh checkout.")
    else:
        line(OK, f"frontend/ complete ({len(FRONTEND_FILES)} files) at {folder}")


def check_dependencies() -> None:
    import importlib.util

    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        line(BAD, f"Missing packages: {', '.join(missing)}")
        problems.append("Install dependencies: pip install -r requirements.txt")
    else:
        line(OK, "All required packages importable")


def check_app_imports() -> None:
    sys.path.insert(0, str(ROOT))
    try:
        from backend.app.main import app  # noqa: F401
    except Exception as exc:  # any import-time failure is worth reporting verbatim
        line(BAD, f"backend.app.main failed to import: {type(exc).__name__}: {exc}")
        problems.append("Fix the import error above; the server cannot start until it is resolved.")
        return
    line(OK, "backend.app.main imports cleanly")


def check_server(port: int = 8000) -> None:
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read())
    except urllib.error.URLError:
        line(WARN, f"No server answering on port {port} (start it, then re-run this check)")
        return
    except Exception as exc:
        line(WARN, f"Could not read {url}: {exc}")
        return

    if "frontend_ready" not in payload:
        line(BAD, f"Server on port {port} is running OLD code (health: {payload})")
        problems.append(
            "The running server predates the frontend fix. Stop it (Ctrl+C), pull the "
            "latest commit, then start uvicorn again."
        )
        return

    if payload.get("frontend_ready"):
        line(OK, f"Server healthy and serving frontend from {payload['frontend_dir']}")
    else:
        line(BAD, f"Server cannot find the frontend in {payload['frontend_dir']}")
        line("      ", f"missing: {', '.join(payload.get('missing_files', []))}")
        problems.append(
            "Restore frontend/ next to backend/, or set FRONTEND_DIR in .env to its location."
        )


def main() -> int:
    print("Payroll & Attendance - setup check")
    print(f"Project root: {ROOT}\n")
    check_python()
    check_git()
    check_frontend()
    check_dependencies()
    check_app_imports()
    check_server()

    print()
    if problems:
        print("Problems found:")
        for number, problem in enumerate(problems, 1):
            print(f"  {number}. {problem}")
        return 1
    print("Everything checks out. Start the server and open http://127.0.0.1:8000/admin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
