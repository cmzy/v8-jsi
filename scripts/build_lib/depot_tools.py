"""Acquire Chromium depot_tools and wire it into the process environment.

Mirrors ``scripts/download_depottools.ps1``:

* Clone depot_tools into ``<repo>/build/depot_tools`` on first use.
* Prepend it to PATH so ``fetch``, ``gclient``, ``gn`` and ``ninja`` resolve.
* Set the environment variables depot_tools / gclient expect.
* Optionally emit Azure DevOps ``##vso`` directives so the same script can
  drive interactive runs and CI runs.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import env


DEPOT_TOOLS_REPO = (
    "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
)


def ensure_installed(sources_path: Path, *, skip_clone: bool = False) -> Path:
    """Clone depot_tools if missing and return its path.

    ``skip_clone=True`` matches the PowerShell ``-NoSetup`` flag: assume the
    tree already exists (useful when callers want to merely set env vars).
    """
    work = env.build_work_dir(sources_path)
    work.mkdir(parents=True, exist_ok=True)
    target = env.depot_tools_dir(sources_path)

    if target.exists():
        return target

    if skip_clone:
        raise SystemExit(
            f"depot_tools not found at {target} and --no-setup was specified"
        )

    print("Cloning depot_tools...", flush=True)
    env.run(["git", "clone", DEPOT_TOOLS_REPO, str(target)], cwd=work)
    return target


def configure_env(
    sources_path: Path,
    *,
    process_env: dict[str, str] | None = None,
    emit_ado: bool = False,
) -> dict[str, str]:
    """Apply the depot_tools environment to ``process_env`` (in-place) and
    return it. When ``process_env`` is ``None`` the current process env is
    mutated, which lets later subprocess calls in the same Python process
    inherit the settings without each call having to pass ``env=``.
    """
    if process_env is None:
        process_env = os.environ  # type: ignore[assignment]

    depot_tools = env.depot_tools_dir(sources_path)

    # On Windows we additionally strip any Chocolatey entries because their
    # bundled git collides with the depot_tools-provided one.
    if env.is_windows():
        path = process_env.get("PATH", "")
        cleaned = os.pathsep.join(
            p for p in path.split(os.pathsep)
            if p and "Chocolatey" not in p
        )
        process_env["PATH"] = cleaned

    env.prepend_path(process_env, depot_tools)

    # Tell depot_tools to skip its Windows-bundled VS toolchain probe; we use
    # the system toolchain. Tell gclient to use python3.
    process_env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    process_env["GCLIENT_PY3"] = "1"
    # Build emits TraceLogging output paths via ASIO_ROOT.
    process_env["ASIO_ROOT"] = str(
        sources_path / "deps" / "asio" / "include"
    )

    if emit_ado:
        # ADO picks up these on stdout to forward env vars to later steps.
        print(f"##vso[task.setvariable variable=PATH;]{process_env['PATH']}")
        print("##vso[task.setvariable variable=DEPOT_TOOLS_WIN_TOOLCHAIN;]0")
        print("##vso[task.setvariable variable=GCLIENT_PY3;]1")
        print(
            f"##vso[task.setvariable variable=ASIO_ROOT;]{process_env['ASIO_ROOT']}"
        )

    return dict(process_env)
