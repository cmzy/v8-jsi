"""Pull V8 source, pin to the version recorded in ``config.json``, apply
the in-tree patches, run ``gclient sync``, and prune unneeded subtrees.

Mirrors ``scripts/fetch_code.ps1``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from . import env


# Paths under ``build/v8`` we delete after sync, copied verbatim from
# ``fetch_code.ps1``. These shave off hundreds of MB and don't affect what
# the v8jsi target compiles.
_PRUNE_PATHS = (
    "depot_tools/external_bin/gsutil",
    "v8/test/test262/data/tools",
    "v8/third_party/depot_tools/external_bin/gsutil",
    "v8/third_party/perfetto",
    # NOTE: v8/third_party/protobuf is intentionally NOT pruned --
    # V8 14.x fuzztest references "$protobuf_target_prefix:protobuf_lite"
    # from its BUILD.gn during `gn gen`, even with v8_enable_test_features
    # off, so deleting it makes gn fail before ninja can run. Inherited
    # from the V8 13 PowerShell scripts where protobuf was unused.
    "v8/third_party/rust",
    # NOTE: v8/third_party/rust-toolchain is intentionally NOT pruned —
    # build/config/rust.gni reads its VERSION file unconditionally during
    # `gn gen`, even when enable_rust=false / v8_enable_temporal_support=
    # false. Removing it makes Mac gn gen fail with a missing-file error.
    "v8/bazel",
    "v8/tools/clusterfuzz",
    "v8/tools/package-lock.json",
    "v8/tools/package.json",
    "v8/tools/turbolizer",
)


def _read_config(sources_path: Path) -> dict:
    with (sources_path / "config.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _apply_patch(repo: Path, patch: Path) -> None:
    print(f"Applying patch {patch.name} in {repo}...", flush=True)
    # --ignore-whitespace matches the PS invocation; we also pass --3way so
    # small drift is reported (with markers) instead of silently failing.
    env.run(
        ["git", "apply", "--ignore-whitespace", "--3way", str(patch)],
        cwd=repo,
    )


def _capture_v8_version(v8_dir: Path) -> tuple[str, str]:
    """Return ``(git_revision, v8_version)`` based on the current checkout."""
    rev = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(v8_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    header = v8_dir / "include" / "v8-version.h"
    text = header.read_text(encoding="utf-8")
    m_major = re.search(r"V8_MAJOR_VERSION\s+(\d+)", text)
    m_minor = re.search(r"V8_MINOR_VERSION\s+(\d+)", text)
    m_build = re.search(r"V8_BUILD_NUMBER\s+(\d+)", text)
    m_patch = re.search(r"V8_PATCH_LEVEL\s+(\d+)", text)
    if not (m_major and m_minor and m_build and m_patch):
        raise SystemExit(f"failed to parse v8-version.h at {header}")
    v8_version = (
        f"{m_major.group(1)}.{m_minor.group(1)}."
        f"{m_build.group(1)}.{m_patch.group(1)}"
    )
    return rev, v8_version


def _write_version_files(
    sources_path: Path, v8_version: str, git_revision: str
) -> str:
    """Expand the version.rc / source_link.json templates. Returns the full
    ``verString`` we emit to ADO (kept compatible with PS output)."""
    config = _read_config(sources_path)
    version = config["version"]  # e.g. "0.79.5"
    parts = version.split(".")
    if len(parts) < 3:
        raise SystemExit(f"unexpected config.json version: {version!r}")
    major, minor, build = parts[0], parts[1], parts[2]
    v8_underscored = v8_version.replace(".", "_")

    src_dir = sources_path / "src"
    rc_template = (src_dir / "version.rc").read_text(encoding="utf-8")
    rc_filled = (
        rc_template
        .replace("V8JSIVER_MAJOR", major)
        .replace("V8JSIVER_MINOR", minor)
        .replace("V8JSIVER_BUILD", build)
        .replace("V8JSIVER_V8REF", v8_underscored)
    )
    (src_dir / "version_gen.rc").write_text(rc_filled, encoding="utf-8")

    sl_template = (src_dir / "source_link.json").read_text(encoding="utf-8")
    sl_filled = (
        sl_template
        .replace("LOCAL_PATH", str(sources_path).replace("\\", "\\\\"))
        .replace("V8JSI_GIT_HASH", _our_git_hash(sources_path))
        .replace("V8JSIVER_V8REF", v8_version)
    )
    (src_dir / "source_link_gen.json").write_text(sl_filled, encoding="utf-8")

    return f"{version}-v8_{v8_underscored}"


def _our_git_hash(sources_path: Path) -> str:
    """Repo hash, used to stamp source_link.json. Returns ``"unknown"`` when
    the sources tree isn't a git checkout (development rsync, CI tarball,
    ...) so the script can still complete in those cases."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(sources_path),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _prune_on_rm_error(func, path, exc_info):
    """shutil.rmtree onerror handler: clear the read-only bit and retry.

    Windows depot_tools leaves git pack files (``*.idx``, ``*.pack``) inside
    third_party/perfetto/.git/objects/pack with the read-only attribute set,
    which makes the default ``os.unlink`` raise ``PermissionError: [WinError
    5] Access is denied``. Clearing read-only and retrying is the standard
    Python idiom (see CPython issue #26660)."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _prune(work: Path) -> None:
    for rel in _PRUNE_PATHS:
        target = work / rel
        if not target.exists():
            continue
        print(f"Pruning {target}", flush=True)
        if target.is_dir():
            shutil.rmtree(target, onerror=_prune_on_rm_error)
        else:
            try:
                target.unlink()
            except PermissionError:
                _prune_on_rm_error(os.unlink, str(target), None)


def fetch(
    sources_path: Path,
    *,
    app_platform: str,
    emit_ado: bool = False,
    skip_patches: bool = False,
) -> None:
    work = env.build_work_dir(sources_path)
    work.mkdir(parents=True, exist_ok=True)
    v8 = env.v8_dir(sources_path)

    # Step 1: fetch v8 if not already present, otherwise we'll just re-checkout.
    if not v8.exists():
        os.environ["GIT_REDIRECT_STDERR"] = "2>&1"
        env.run(["fetch", "--no-history", "--nohooks", "v8"], cwd=work)
    else:
        print(f"v8 tree already present at {v8}, skipping fetch", flush=True)

    # Step 2: when targeting Android/Linux/macOS the .gclient file needs an
    # extra ``target_os`` entry so gclient pulls platform-specific deps.
    if app_platform in ("android", "linux", "mac"):
        gclient_path = work / ".gclient"
        marker = f"target_os= ['{app_platform}']"
        if gclient_path.exists():
            content = gclient_path.read_text(encoding="utf-8")
            if marker not in content:
                with gclient_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"\n{marker}\n")

    # Step 3: pin to the v8ref recorded in config.json.
    config = _read_config(sources_path)
    v8ref = config["v8ref"]
    env.run(["git", "fetch", "origin", v8ref], cwd=v8)
    env.run(["git", "checkout", "FETCH_HEAD"], cwd=v8)

    # Step 4: gclient sync refuses to operate on dirty sub-repos. If a
    # previous fetch left patches applied to v8/build or third_party/zlib
    # (typical when calling fetch a second time to add another target_os),
    # roll them back so sync can run. We re-apply the patches at the end.
    for sub in (v8 / "build", v8 / "third_party" / "zlib"):
        if (sub / ".git").exists():
            try:
                env.run(["git", "reset", "--hard", "HEAD"], cwd=sub, check=False)
            except Exception:
                pass

    # Apply the v8/ patches (src.diff). This must happen BEFORE gclient
    # runhooks because src.diff edits DEPS (the rc_win hook).
    patch_dir = sources_path / "scripts" / "patch"
    if not skip_patches:
        _apply_patch(v8, patch_dir / "src.diff")

    # gclient runhooks + sync pull a complete checkout.
    env.run(["gclient", "runhooks"], cwd=v8)
    env.run(["gclient", "sync"], cwd=v8)

    if not skip_patches:
        _apply_patch(v8 / "build", patch_dir / "build.diff")
        _apply_patch(v8 / "third_party" / "zlib", patch_dir / "zlib.diff")

    # Step 5: stamp version.rc / source_link.json.
    revision, v8_version = _capture_v8_version(v8)
    ver_string = _write_version_files(sources_path, v8_version, revision)

    if emit_ado:
        print(f"##vso[task.setvariable variable=V8JSI_VERSION;]{ver_string}")
        build_number = os.environ.get("BUILD_BUILDNUMBER")
        if build_number:
            v8_underscored = v8_version.replace(".", "_")
            if not build_number.endswith(v8_underscored):
                semver_parts = config["version"].split(".")
                semver = f"{semver_parts[0]}.{semver_parts[1]}.{semver_parts[2]}"
                new_build_number = (
                    f"{build_number} - {semver}.{v8_underscored}"
                )
                print(
                    f"##vso[build.updateBuildNumber]{new_build_number}"
                )

    # Step 6: install distro deps for the Linux/Android cross-compiles.
    # Uses `sudo -n` so non-interactive runs (CI, ssh w/o tty) skip the
    # apt-get step and continue instead of blocking on a password prompt.
    # Run `sudo -v` once beforehand to cache credentials if you want the
    # deps actually installed.
    if env.is_linux() and app_platform in ("android", "linux"):
        script = "install-build-deps-android.sh" if app_platform == "android" \
                 else "install-build-deps.sh"
        deps_script = v8 / "build" / script
        if deps_script.exists():
            try:
                env.run(["sudo", "-n", "bash", str(deps_script)], cwd=v8)
            except SystemExit:
                print(
                    f"NOTE: skipped `sudo bash {deps_script.name}` (no passwordless "
                    "sudo available). If the build later complains about missing "
                    "system libraries (especially for Android cross-compile), run "
                    "the script manually as root and re-trigger the build.",
                    flush=True,
                )

    _prune(work)

    # V8 14 + is_official_build=true 触发某些 BUILD.gn / exec_script 读
    # v8/chrome/VERSION（Chromium 主仓约定，V8 standalone gclient sync 不拉
    # chrome/ 子树）。stub 一个最小 VERSION 让 gn gen 跳过；运行时不读。
    chrome_version = v8 / "chrome" / "VERSION"
    if not chrome_version.exists():
        chrome_version.parent.mkdir(parents=True, exist_ok=True)
        chrome_version.write_text("MAJOR=140\nMINOR=0\nBUILD=0\nPATCH=0\n",
                                  encoding="utf-8")
        print(f"Stubbed {chrome_version} (V8 standalone build needs MAJOR/.../PATCH).",
              flush=True)
