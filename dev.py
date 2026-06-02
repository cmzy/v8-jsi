#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
"""Cross-platform developer task runner for v8-jsi.

This is the Python port of ``dev.ps1`` + ``localbuild.ps1`` + the helper
``scripts/*.ps1`` scripts. It works identically on Windows, macOS and Linux
(plus Android cross-compiles, which are driven from a Linux host).

Common entry points:

    python3 dev.py setup
        Clone Chromium depot_tools and set the necessary env vars.

    python3 dev.py fetch [--app-platform PLATFORM]
        Pull V8 source, pin to the version in config.json, apply patches.

    python3 dev.py build [--platform CPU] [--config CFG] [--app-platform PLAT]
        Configure with GN and build with Ninja, then assemble the
        NuGet-shaped output tree under ``out/``.

    python3 dev.py all [...]
        Run setup + fetch + build in sequence; mirrors localbuild.ps1.

    python3 dev.py update-version [--beta] [--git-push]
        Poll chromiumdash for a new V8 release and bump config.json.

    python3 dev.py fork-sync -- [args]
        Pass-through to the npm ``@rnx-kit/fork-sync`` tool (Node-based).

The legacy ``.ps1`` scripts are kept in ``scripts/`` for back-compat with
existing CI; new automation should prefer this entry point.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# When invoked as ``python3 dev.py`` from the repo root, the script's directory
# is the repo root - make build_lib importable directly.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "scripts"))

from build_lib import build as _build  # noqa: E402
from build_lib import depot_tools as _depot  # noqa: E402
from build_lib import env as _env  # noqa: E402
from build_lib import fetch as _fetch  # noqa: E402
from build_lib import update_version as _uv  # noqa: E402


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    if not args.no_setup:
        _depot.ensure_installed(args.sources, skip_clone=False)
    else:
        _depot.ensure_installed(args.sources, skip_clone=True)
    _depot.configure_env(args.sources, emit_ado=args.ado)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    _depot.configure_env(args.sources)
    _fetch.fetch(
        args.sources,
        app_platform=args.app_platform,
        emit_ado=args.ado,
        skip_patches=args.skip_patches,
    )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    _depot.configure_env(args.sources)
    _build.build(
        args.sources,
        platform_cpu=args.platform,
        configuration=args.config,
        app_platform=args.app_platform,
        output_path=args.output,
        use_clang=args.use_clang,
        use_libcpp=args.use_libcpp,
        fake_build=args.fake_build,
    )
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    """``localbuild.ps1`` equivalent: setup + fetch + build for one matrix."""
    if not args.no_setup and not args.fake_build:
        _depot.ensure_installed(args.sources, skip_clone=False)
        _depot.configure_env(args.sources, emit_ado=args.ado)
        _fetch.fetch(
            args.sources,
            app_platform=args.app_platform[0],
            emit_ado=args.ado,
        )
    else:
        _depot.ensure_installed(args.sources, skip_clone=True)
        _depot.configure_env(args.sources)

    for plat in args.platform:
        for cfg in args.config:
            for app_plat in args.app_platform:
                print(
                    f"Building {app_plat} {plat} {cfg}...",
                    flush=True,
                )
                _build.build(
                    args.sources,
                    platform_cpu=plat,
                    configuration=cfg,
                    app_platform=app_plat,
                    output_path=args.output,
                    use_clang=args.use_clang,
                    use_libcpp=args.use_libcpp,
                    fake_build=args.fake_build,
                )
    return 0


def cmd_update_version(args: argparse.Namespace) -> int:
    return _uv.main(args.sources, beta=args.beta, git_push=args.git_push)


def cmd_fork_sync(args: argparse.Namespace) -> int:
    """Delegate to the existing npm ``@rnx-kit/fork-sync`` package.

    We mirror the PowerShell wrapper: ``npm install`` on first use, then
    invoke ``node scripts/fork-sync/node_modules/.../sync.js`` with whatever
    extra args the caller passed after ``--``.
    """
    fork_dir = args.sources / "scripts" / "fork-sync"
    stamp = fork_dir / "node_modules" / ".package-lock.json"
    package_json = fork_dir / "package.json"
    needs_install = (
        not stamp.exists()
        or stamp.stat().st_mtime < package_json.stat().st_mtime
    )
    if needs_install:
        print("Installing fork-sync dependencies...", flush=True)
        subprocess.run(
            ["npm", "install"], check=True, cwd=str(fork_dir),
        )
    sync_js = (
        fork_dir / "node_modules" / "@rnx-kit" / "fork-sync"
        / "lib" / "sync.js"
    )
    cmd = ["node", str(sync_js), *args.passthrough]
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev",
        description="v8-jsi developer task runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=_HERE,
        help="repository root (default: directory containing dev.py)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="install depot_tools and env")
    p_setup.add_argument(
        "--no-setup", action="store_true",
        help="skip cloning depot_tools (it must already exist)",
    )
    p_setup.add_argument(
        "--ado", action="store_true",
        help="emit Azure DevOps ##vso directives",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_fetch = sub.add_parser("fetch", help="fetch V8 and apply patches")
    p_fetch.add_argument(
        "--app-platform", choices=_env.APP_PLATFORMS,
        default=_env.host_app_platform(),
    )
    p_fetch.add_argument("--ado", action="store_true")
    p_fetch.add_argument(
        "--skip-patches", action="store_true",
        help="don't apply scripts/patch/*.diff (use when bringing up a new V8 version)",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    p_build = sub.add_parser("build", help="run gn gen + ninja + package")
    p_build.add_argument(
        "--platform", choices=_env.TARGET_CPUS, default="x64",
    )
    p_build.add_argument(
        "--config", choices=_env.CONFIGURATIONS, default="Release",
    )
    p_build.add_argument(
        "--app-platform", choices=_env.APP_PLATFORMS,
        default=_env.host_app_platform(),
    )
    p_build.add_argument(
        "--output", type=Path, default=_HERE / "out",
        help="root of the NuGet-shaped output tree",
    )
    p_build.add_argument(
        "--use-clang", action="store_true",
        help="build with clang-cl on Windows (no effect on Linux/macOS)",
    )
    p_build.add_argument(
        "--use-libcpp", action="store_true",
        help="use V8's bundled libc++ on Windows (no effect elsewhere)",
    )
    p_build.add_argument(
        "--fake-build", action="store_true",
        help="skip gn/ninja and produce placeholder outputs (CI smoke test)",
    )
    p_build.set_defaults(func=cmd_build)

    p_all = sub.add_parser("all", help="setup + fetch + build (localbuild.ps1)")
    p_all.add_argument(
        "--platform", choices=_env.TARGET_CPUS, nargs="+", default=["x64"],
    )
    p_all.add_argument(
        "--config", choices=_env.CONFIGURATIONS, nargs="+", default=["Debug"],
    )
    p_all.add_argument(
        "--app-platform", choices=_env.APP_PLATFORMS, nargs="+",
        default=[_env.host_app_platform()],
    )
    p_all.add_argument("--output", type=Path, default=_HERE / "out")
    p_all.add_argument("--no-setup", action="store_true")
    p_all.add_argument("--fake-build", action="store_true")
    p_all.add_argument("--use-clang", action="store_true")
    p_all.add_argument("--use-libcpp", action="store_true")
    p_all.add_argument("--ado", action="store_true")
    p_all.set_defaults(func=cmd_all)

    p_uv = sub.add_parser(
        "update-version",
        help="poll chromiumdash and bump config.json buildNumber",
    )
    p_uv.add_argument("--beta", action="store_true")
    p_uv.add_argument("--git-push", action="store_true")
    p_uv.set_defaults(func=cmd_update_version)

    p_fs = sub.add_parser(
        "fork-sync",
        help="delegate to @rnx-kit/fork-sync (pass extra args after --)",
    )
    p_fs.add_argument("passthrough", nargs=argparse.REMAINDER)
    p_fs.set_defaults(func=cmd_fork_sync)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # argparse's REMAINDER swallows a leading ``--`` literally; strip it for
    # cleaner pass-through to fork-sync.
    if getattr(args, "passthrough", None) and args.passthrough[:1] == ["--"]:
        args.passthrough = args.passthrough[1:]
    args.sources = args.sources.resolve()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
