"""Environment / platform helpers shared by every build subcommand."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Mapping


# Recognized AppPlatform values, mirroring the PowerShell scripts.
APP_PLATFORMS = ("win32", "android", "linux", "mac", "ios")
TARGET_CPUS = ("x64", "x86", "arm64")
CONFIGURATIONS = ("Debug", "Release")


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def host_app_platform() -> str:
    """The app-platform string matching the *host* machine, used as a default."""
    if is_windows():
        return "win32"
    if is_macos():
        return "mac"
    if is_linux():
        return "linux"
    raise RuntimeError(f"unsupported host platform: {sys.platform}")


def cpu_count_for_build() -> int:
    """How many ninja jobs to run. Mirrors the 2x logical-cores heuristic."""
    n = os.cpu_count() or 4
    return n * 2


def project_root() -> Path:
    """Repo root, derived from this file's location: scripts/build_lib/env.py."""
    return Path(__file__).resolve().parent.parent.parent


def build_work_dir(sources_path: Path) -> Path:
    """The PowerShell scripts use ``<repo>/build`` as the workspace."""
    return sources_path / "build"


def depot_tools_dir(sources_path: Path) -> Path:
    return build_work_dir(sources_path) / "depot_tools"


def v8_dir(sources_path: Path) -> Path:
    return build_work_dir(sources_path) / "v8"


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def run(
    cmd: list[str | Path],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and stream its output. Returns the CompletedProcess.

    ``cmd`` is always a list — never a shell string — to keep semantics
    identical across platforms and avoid quoting traps.
    """
    str_cmd = [str(c) for c in cmd]
    pretty = " ".join(str_cmd)
    print(f"+ {pretty}", flush=True)

    result = subprocess.run(
        str_cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )
    if check and result.returncode != 0:
        if capture and result.stdout:
            print(result.stdout, flush=True)
        raise SystemExit(
            f"command failed (exit {result.returncode}): {pretty}"
        )
    return result


def which(name: str, extra_path: Iterable[Path] | None = None) -> Path | None:
    """Find an executable, optionally with extra PATH entries prepended."""
    if extra_path:
        path_parts = [str(p) for p in extra_path] + [os.environ.get("PATH", "")]
        searched = os.pathsep.join(path_parts)
    else:
        searched = os.environ.get("PATH", "")
    found = shutil.which(name, path=searched)
    return Path(found) if found else None


# ---------------------------------------------------------------------------
# Path manipulation
# ---------------------------------------------------------------------------


def prepend_path(env: dict[str, str], entry: Path) -> None:
    """Prepend ``entry`` to ``PATH`` in ``env``, dedupe-aware."""
    current = env.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    entry_str = str(entry)
    parts = [p for p in parts if p and p != entry_str]
    env["PATH"] = os.pathsep.join([entry_str] + parts)
