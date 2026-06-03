"""Generate GN, drive Ninja, and assemble the NuGet-shaped output tree.

Mirrors ``scripts/build.ps1``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import env


def _gn_arg_string(
    *,
    platform_cpu: str,
    configuration: str,
    app_platform: str,
    use_clang: bool,
    use_libcpp: bool,
    enable_inspector: bool,
) -> str:
    """Compose the GN ``args`` string identical to the PowerShell version."""
    building_windows = app_platform == "win32"

    flags: list[str] = [
        "v8_enable_i18n_support=false",
        "is_component_build=false",
        "v8_monolithic=true",
        # Builds V8 internal TLS variables with the local-dynamic TLS model so
        # they can be linked into our libv8jsi.so. Without this the link step
        # fails with R_X86_64_TPOFF32 relocations against V8's thread_local
        # globals (g_current_isolate_, g_current_local_heap_) on Linux.
        "v8_monolithic_for_shared_library=true",
        # V8 14 ships a Temporal proposal implementation backed by the Rust
        # crate `temporal_capi`. The v8_monolith target does not bundle that
        # static library, so leaving it on results in a flood of
        # `temporal_rs_*` undefined-symbol link errors. Embedders that need
        # Temporal can flip this back to true and add the dep explicitly.
        "v8_enable_temporal_support=false",
        # V8 14 enables Chromium's PartitionAlloc as the process-wide
        # malloc shim by default. That means libv8jsi.so's libcxx allocates
        # std::string buffers from PartitionAlloc's pool, but consumers
        # (jsitests, embedder apps) free() those buffers through glibc and
        # crash with "free(): invalid size" on the PartitionAlloc address
        # range. Disabling the allocator shim makes libv8jsi.so go through
        # the system allocator and matches embedder expectations.
        "use_partition_alloc_as_malloc=false",
        "use_allocator_shim=false",
        "v8_use_external_startup_data=false",
        "treat_warnings_as_errors=false",
        # Chrome DevTools inspector. Currently only wired up on Windows (the
        # inspector source files in src/BUILD.gn are gated on is_win, and the
        # uses in V8JsiRuntime.cpp are gated on _WIN32 && V8JSI_ENABLE_INSPECTOR).
        # On non-Windows platforms this arg toggles the V8JSI_ENABLE_INSPECTOR
        # define but produces functionally identical binaries — we ship both
        # variants anyway so the packaging pipeline is uniform across platforms
        # and so embedders can pin on the binary name.
        f"v8jsi_enable_inspector={'true' if enable_inspector else 'false'}",
        # Size: drop V8 debug/introspection helpers that production embedders
        # never trigger. Each one shaves between a few hundred KB and a few
        # MB off the .so/.dll; together they are worth ~5-8 MB. Functional
        # behavior of JS execution is unchanged — these only affect
        # --print-code / heap-snapshot-verify / runtime-call-stats and other
        # developer-side diagnostics.
        "v8_enable_disassembler=false",
        "v8_enable_object_print=false",
        "v8_enable_gdbjit=false",
        "v8_enable_v8_checks=false",
        "v8_enable_runtime_call_stats=false",
        # Drops the V8 heap-snapshot self-verification pass. Only useful
        # when actively debugging the heap snapshot infrastructure; ships
        # a few hundred KB of dead checks otherwise.
        "v8_enable_heap_snapshot_verify=false",
        # Skip compiling the Node-API binding layer into libv8jsi. This
        # removes ~2-3 MB of N-API + the node-api-jsi runtime wrapper.
        # **Functional impact**: embedders that consume v8jsi through
        # N-API instead of JSI directly will need to re-enable this in
        # a custom build (set `v8jsi_enable_node_api=true` in args.gn).
        "v8jsi_enable_node_api=false",
    ]

    if not building_windows:
        flags.append("use_goma=false")
        flags.append(f'target_os="{app_platform}"')
        if app_platform == "ios":
            # Chromium iOS requires explicit target_environment: device,
            # simulator, or catalyst. Default to device so the build
            # produces an arm64 artifact suitable for installing on a
            # real device or shipping in an embedder framework.
            flags.append('target_environment="device"')
            # iOS forbids JIT (App Store rules) and Apple lockdown mode,
            # so V8 is built jitless and WebAssembly must be disabled —
            # otherwise Torque references WasmFuncRef when generating the
            # builtin tables without the corresponding wasm .tq files
            # being part of the first generation step. Disable
            # WebAssembly explicitly here.
            flags.append("v8_enable_webassembly=false")
    else:
        if not use_libcpp:
            flags.append("use_custom_libcxx=false")

    flags.append(f'target_cpu="{platform_cpu}"')

    if building_windows:
        flags.append(f"is_clang={'true' if use_clang else 'false'}")

    if platform_cpu.endswith("64"):
        # Pointer compression only makes sense on 64-bit builds.
        flags.append("v8_enable_pointer_compression=true")

    # The Node-API external ArrayBuffer impl doesn't satisfy V8 sandbox rules.
    flags.append("v8_enable_sandbox=false")

    if "ebug" in configuration:
        flags.append("is_debug=true")
        # ARM64 mksnapshot can't build with MSVC iterator debugging on.
        if building_windows and platform_cpu != "arm64":
            flags.append("enable_iterator_debugging=true")
    else:
        flags.extend(["enable_iterator_debugging=false", "is_debug=false"])
        # Production-only size optimizations.
        flags.extend([
            # Drop DWARF / line-table info from the binary entirely. We
            # ship release builds without separate symbol packages; keeping
            # -g2 (V8's release default) bloats libv8jsi by ~10-15 MB on
            # Linux/macOS without helping anyone. Embedders that need
            # crash-analysis frames can rebuild with symbol_level=2.
            "symbol_level=0",
            # Turn on Chromium's "official build" knob. Triggers tighter
            # inlining heuristics, drops dchecks unconditionally, removes
            # extra reflection/debug data, and tightens visibility — the
            # bundle of release-mode optimizations Chrome actually ships
            # with. Saves a few MB across the binary.
            "is_official_build=true",
            # is_official_build also implicitly enables PGO via
            # `chrome_pgo_phase=2`, which tries to `exec_script` a
            # Chromium tooling helper (tools/update_pgo_profiles.py)
            # that V8 doesn't ship. We don't have PGO profile data
            # anyway, so disable that whole codepath.
            "chrome_pgo_phase=0",
            # is_official_build implicitly flips on use_thin_lto. We
            # **explicitly disable global thin-LTO** here so the V8
            # monolith static library is not rebuilt as bitcode (~3-5x
            # build-time cost, lld warnings about non-bitcode archive
            # members) — the user-driven decision is to LTO only the
            # libv8jsi shared library, not the V8 static lib it links
            # against. Per-target LTO for the v8jsi shared library is
            # added in src/BUILD.gn via cflags/ldflags so it applies
            # only to our ~20 TUs.
            "use_thin_lto=false",
            "thin_lto_enable_optimizations=false",
            # is_official_build also turns on Control Flow Integrity
            # (is_cfi=true) on Linux/Android, and CFI requires global
            # ThinLTO (`assert(!is_cfi || use_thin_lto)` in
            # build/config/compiler/compiler.gni). Since we deliberately
            # disabled global LTO above, we must also disable CFI to
            # keep the assertion satisfied.
            "is_cfi=false",
        ])

    return " ".join(flags)


def _copy_jsi_tree(sources_path: Path, dest: Path) -> None:
    """Drop a clean copy of ``src/`` into ``build/v8/jsi`` (the BUILD.gn at
    ``//jsi/BUILD.gn`` lives in that copied tree)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    src = sources_path / "src"
    for entry in src.iterdir():
        target = dest / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)


def _validate_build_output(out_dir: Path) -> Path:
    """Return whichever of v8jsi.{dll,so,dylib} is present."""
    # iOS framework builds may nest the binary under libv8jsi.framework/.
    for name in (
        "v8jsi.dll",
        "libv8jsi.so",
        "libv8jsi.dylib",
        "libv8jsi.framework/libv8jsi",
    ):
        candidate = out_dir / name
        if candidate.exists():
            return candidate
    raise SystemExit(
        f"build appears to have failed: no v8jsi shared library found in {out_dir}"
    )


def _variant_dir_name(enable_inspector: bool) -> str:
    """Subdirectory under ``out/<app>/<cpu>/<cfg>`` that holds the build for
    the inspector variant. We use distinct subdirectories so the two
    variants don't trample each other's intermediate objects."""
    return "with-inspector" if enable_inspector else "noinspector"


def _binary_suffix(enable_inspector: bool) -> str:
    """Filename suffix appended to the packaged binary for the
    no-inspector variant. The with-inspector build keeps the original
    name so existing consumers / .targets references don't need to
    change."""
    return "" if enable_inspector else "-noinspector"


# Our GN target_cpu names vs Android's NDK ABI names used in jniLibs/.
# The packager writes the .so into BOTH the GN-cpu path (legacy) and the
# Android-ABI path so embedders can point their AAR/Gradle jniLibs config
# directly at our output tree without an intermediate rename script.
_ANDROID_CPU_TO_ABI = {
    "x64": "x86_64",
    "x86": "x86",
    "arm64": "arm64-v8a",
    "arm": "armeabi-v7a",
}


def _strip_packaged_binary(binary: Path, app_platform: str) -> None:
    """Run the platform's ``strip`` on the production-packaged copy of
    libv8jsi. Reduces final size by removing the static symbol table /
    leftover debug remnants — dynamic exports (``.dynsym`` on ELF, the
    Mach-O ``LC_SYMTAB`` dynamic part, the Windows ``.dll.lib`` import
    library) are preserved so embedders can still link/load normally.

    Windows builds are skipped: PDBs are already separate, and
    ``v8jsi.dll`` itself is stripped by ``/OPT:REF /OPT:ICF`` at link
    time.
    """
    if not binary.exists() or app_platform == "win32":
        return
    if app_platform in ("linux", "android"):
        cmd = ["strip", "--strip-all", str(binary)]
    elif app_platform in ("mac", "ios"):
        # -S = remove debug symbols, -x = remove local (non-dynamic)
        # symbols. Together: leave only the dynamic export table that
        # consumer code needs.
        cmd = ["strip", "-S", "-x", str(binary)]
    else:
        return
    try:
        subprocess.run(cmd, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(
            f"warning: strip step failed ({exc}); shipped binary is "
            f"unstripped — size will be larger than expected",
            flush=True,
        )


def _copy_ios_framework(
    src_fw: Path, dst_fw: Path, *, suffix: str
) -> None:
    """Copy a `libv8jsi.framework` bundle, renaming both the bundle dir
    and the internal binary when ``suffix`` is non-empty.

    Frameworks are directories, not files: a sibling
    ``libv8jsi-noinspector.framework`` whose internal binary is still
    named ``libv8jsi`` would make Xcode complain (the executable name
    inside the bundle must match the bundle name, and
    ``Info.plist::CFBundleExecutable`` references it). We rename the
    binary and best-effort patch the plist key — the plist V8 emits is
    minimal, so a string-level replace is safe enough.
    """
    if dst_fw.exists():
        shutil.rmtree(dst_fw)
    shutil.copytree(src_fw, dst_fw, symlinks=True)
    if not suffix:
        return
    old_bin = dst_fw / "libv8jsi"
    new_bin = dst_fw / f"libv8jsi{suffix}"
    if old_bin.exists():
        old_bin.rename(new_bin)
    info_plist = dst_fw / "Info.plist"
    if info_plist.exists():
        text = info_plist.read_text(encoding="utf-8")
        text = text.replace(
            "<string>libv8jsi</string>",
            f"<string>libv8jsi{suffix}</string>",
        )
        info_plist.write_text(text, encoding="utf-8")


_APPLE_FRAMEWORK_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleExecutable</key>
    <string>{binary_name}</string>
    <key>CFBundleIdentifier</key>
    <string>com.microsoft.v8jsi{id_suffix}</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>{binary_name}</string>
    <key>CFBundlePackageType</key>
    <string>FMWK</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
{platform_keys}
</dict>
</plist>
"""

_PLATFORM_PLIST_KEYS = {
    "ios": (
        "    <key>MinimumOSVersion</key>\n"
        "    <string>12.0</string>\n"
        "    <key>CFBundleSupportedPlatforms</key>\n"
        "    <array>\n"
        "        <string>iPhoneOS</string>\n"
        "    </array>"
    ),
    "mac": (
        "    <key>LSMinimumSystemVersion</key>\n"
        "    <string>12.0</string>\n"
        "    <key>CFBundleSupportedPlatforms</key>\n"
        "    <array>\n"
        "        <string>MacOSX</string>\n"
        "    </array>"
    ),
}


def _wrap_dylib_as_apple_framework(
    src_dylib: Path,
    dst_framework: Path,
    *,
    suffix: str,
    app_platform: str,
) -> Path:
    """Wrap a bare libv8jsi dylib into a flat (unversioned) Apple
    framework bundle on macOS or iOS.

    GN's `shared_library` template emits a bare `libv8jsi.dylib`;
    Xcode-style embedders expect a `libv8jsi.framework/` bundle with the
    `@rpath/libv8jsi.framework/libv8jsi` install name and an
    `Info.plist` declaring `CFBundleExecutable` / `CFBundlePackageType =
    FMWK`. We assemble that bundle here:

      libv8jsi.framework/
        libv8jsi          # the dylib, install-name fixed
        Info.plist        # minimal FMWK plist (platform-specific keys)

    Unversioned (no `Versions/A/...` indirection) is acceptable for
    embedded frameworks on both modern macOS and iOS; this keeps the
    layout consistent between the two and avoids the symlink dance the
    versioned-framework layout requires.

    Returns the path to the framework's internal binary so the caller
    can hand it off to the strip pass.
    """
    if dst_framework.exists():
        shutil.rmtree(dst_framework)
    dst_framework.mkdir(parents=True)
    binary_name = f"libv8jsi{suffix}"
    dst_binary = dst_framework / binary_name
    shutil.copy2(src_dylib, dst_binary)
    # Fix install name so apps that link against the framework resolve
    # the binary through the bundle structure, not the bare dylib path.
    try:
        subprocess.run(
            [
                "install_name_tool",
                "-id",
                f"@rpath/libv8jsi{suffix}.framework/{binary_name}",
                str(dst_binary),
            ],
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(
            f"warning: install_name_tool failed on {dst_binary} ({exc}); "
            f"the framework will still load but @rpath lookups may need "
            f"manual fixup",
            flush=True,
        )
    (dst_framework / "Info.plist").write_text(
        _APPLE_FRAMEWORK_INFO_PLIST.format(
            binary_name=binary_name,
            id_suffix=suffix.replace("-", "."),
            platform_keys=_PLATFORM_PLIST_KEYS[app_platform],
        ),
        encoding="utf-8",
    )
    return dst_binary


def build(
    sources_path: Path,
    *,
    platform_cpu: str,
    configuration: str,
    app_platform: str,
    output_path: Path,
    use_clang: bool = False,
    use_libcpp: bool = False,
    fake_build: bool = False,
    enable_inspector: bool = True,
) -> Path:
    """Run the GN+Ninja build and return the path to the produced binary
    (or to the dummy file written in ``fake_build`` mode)."""
    work = env.build_work_dir(sources_path)
    v8 = env.v8_dir(sources_path)

    jsi_in_v8 = v8 / "jsi"
    _copy_jsi_tree(sources_path, jsi_in_v8)

    gn_args = _gn_arg_string(
        platform_cpu=platform_cpu,
        configuration=configuration,
        app_platform=app_platform,
        use_clang=use_clang,
        use_libcpp=use_libcpp,
        enable_inspector=enable_inspector,
    )

    out_dir = (
        v8 / "out" / app_platform / platform_cpu / configuration
        / _variant_dir_name(enable_inspector)
    )

    print(f"gn command line: gn gen {out_dir} --args='{gn_args}'", flush=True)
    if not fake_build:
        env.run(["gn", "gen", str(out_dir), f"--args={gn_args}"], cwd=v8)
    else:
        print("gn command skipped: fake build", flush=True)

    jobs = env.cpu_count_for_build()

    ninja_targets = ["v8jsi"]
    # jsitests is a host gtest executable: it makes sense only on platforms
    # that can run a native binary directly (win32 / linux / mac). Android
    # and iOS cross-compiles build only the shared library; testing those
    # happens on-device through the embedder app.
    #
    # We also skip jsitests on Release builds because the Release config
    # disables Node-API (`v8jsi_enable_node_api=false`) for size, while
    # testmain.cpp unconditionally pulls in node-api-jsi headers. Tests
    # therefore run against Debug builds (which keep N-API on).
    is_release = "ebug" not in configuration
    if app_platform in ("win32", "linux", "mac") and not is_release:
        ninja_targets.append("jsitests")
        if app_platform == "win32":
            # node_api_tests pulls in child_process.cpp which uses
            # <strsafe.h>/<windows.h>; not portable. Same with v8windbg.
            ninja_targets.append("node_api_tests")
            ninja_targets.append("v8windbg")

    print(
        f"ninja command line: ninja -v -j {jobs} -C {out_dir} "
        + " ".join(ninja_targets),
        flush=True,
    )
    if not fake_build:
        # Tee output to build.log as the PS script does.
        log_path = sources_path / "build.log"
        with log_path.open("w", encoding="utf-8") as log:
            import subprocess as _subprocess
            cmd = ["ninja", "-v", "-j", str(jobs), "-C", str(out_dir), *ninja_targets]
            print(f"+ {' '.join(cmd)}", flush=True)
            proc = _subprocess.Popen(
                cmd, stdout=_subprocess.PIPE, stderr=_subprocess.STDOUT,
                text=True, cwd=str(v8),
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)
                log.write(line)
            rc = proc.wait()
        if rc != 0:
            raise SystemExit(
                f"ninja failed (exit {rc}); see {log_path} for details"
            )
        produced = _validate_build_output(out_dir)
    else:
        print("ninja command skipped: fake build", flush=True)
        # Write a placeholder so downstream packaging works in CI.
        out_dir.mkdir(parents=True, exist_ok=True)
        placeholder = out_dir / (
            "v8jsi.dll" if env.is_windows()
            else ("libv8jsi.dylib" if env.is_macos() else "libv8jsi.so")
        )
        placeholder.write_bytes(b"")
        produced = placeholder

    _package(
        sources_path=sources_path,
        output_path=output_path,
        platform_cpu=platform_cpu,
        configuration=configuration,
        app_platform=app_platform,
        out_dir=out_dir,
        produced_binary=produced,
        fake_build=fake_build,
        enable_inspector=enable_inspector,
    )

    return produced


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def _mkdirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def _copy(src: Path, dst: Path) -> None:
    if not src.exists():
        # Some files are only produced on certain platforms; that's fine.
        return
    if dst.is_dir() or str(dst).endswith(("/", "\\")):
        shutil.copy2(src, dst / src.name)
    else:
        shutil.copy2(src, dst)


def _package(
    *,
    sources_path: Path,
    output_path: Path,
    platform_cpu: str,
    configuration: str,
    app_platform: str,
    out_dir: Path,
    produced_binary: Path,
    fake_build: bool,
    enable_inspector: bool,
) -> None:
    """Drop binaries + headers into the NuGet layout the .nuspec expects.

    Both inspector variants land in the same ``lib_dir`` side-by-side; the
    no-inspector variant uses a ``-noinspector`` suffix in the filename so
    consumers can pin on whichever build they want. Headers, license, and
    packaging glue are platform-wide and are dropped once per package run
    (re-running for the second variant just overwrites with identical
    content, which is fine).
    """
    include_root = output_path / "build" / "native" / "include"
    inc_node_api = include_root / "node-api"
    inc_node_api_jsi = include_root / "node-api-jsi"
    inc_node_api_jsi_loaders = inc_node_api_jsi / "ApiLoaders"
    inc_jsi = include_root.parent / "jsi" / "jsi"
    license_dir = output_path / "license"
    lib_dir = (
        output_path / "lib" / app_platform / configuration / platform_cpu
    )
    _mkdirs(
        inc_node_api,
        inc_node_api_jsi,
        inc_node_api_jsi_loaders,
        inc_jsi,
        license_dir,
        lib_dir,
    )

    suffix = _binary_suffix(enable_inspector)
    is_release = "ebug" not in configuration

    # Track every packaged copy of the shared library so we can run a
    # post-link strip pass over each of them in Release.
    stripped_targets: list[Path] = []

    # --- binaries ---
    if app_platform == "win32":
        _copy(out_dir / "v8jsi.dll", lib_dir / f"v8jsi{suffix}.dll")
        _copy(out_dir / "v8jsi.dll.lib", lib_dir / f"v8jsi{suffix}.dll.lib")
        if platform_cpu == "arm64":
            _copy(
                out_dir / "v8jsi_stripped.dll.pdb",
                lib_dir / f"v8jsi{suffix}.dll.pdb",
            )
        else:
            _copy(
                out_dir / "v8jsi.dll.pdb",
                lib_dir / f"v8jsi{suffix}.dll.pdb",
            )
        # Windows: PDB is already separate, no post-link strip needed.
    elif app_platform == "mac":
        # Embed the dylib in a `libv8jsi.framework/` bundle so consumers
        # can drop it into an Xcode project's Frameworks group without
        # extra bundling steps.
        src_dy = out_dir / "libv8jsi.dylib"
        dst_fw = lib_dir / f"libv8jsi{suffix}.framework"
        if src_dy.exists():
            bin_inside = _wrap_dylib_as_apple_framework(
                src_dy, dst_fw, suffix=suffix, app_platform="mac"
            )
            stripped_targets.append(bin_inside)
    elif app_platform == "linux":
        dst = lib_dir / f"libv8jsi{suffix}.so"
        _copy(out_dir / "libv8jsi.so", dst)
        stripped_targets.append(dst)
    elif app_platform == "android":
        dst = lib_dir / f"libv8jsi{suffix}.so"
        _copy(out_dir / "libv8jsi.so", dst)
        stripped_targets.append(dst)
        # Mirror the .so under the NDK ABI name as well so embedders can
        # point Gradle's `jniLibs.srcDirs` at `out/lib/android/<cfg>/`
        # directly. The legacy `<cpu>` path above is preserved.
        abi = _ANDROID_CPU_TO_ABI.get(platform_cpu)
        if abi:
            jni_dir = output_path / "lib" / "android" / configuration / abi
            jni_dir.mkdir(parents=True, exist_ok=True)
            jni_dst = jni_dir / f"libv8jsi{suffix}.so"
            _copy(out_dir / "libv8jsi.so", jni_dst)
            stripped_targets.append(jni_dst)
    elif app_platform == "ios":
        # Same framework treatment as macOS — see `_wrap_dylib_as_apple_framework`.
        src_dy = out_dir / "libv8jsi.dylib"
        dst_fw = lib_dir / f"libv8jsi{suffix}.framework"
        if src_dy.exists():
            bin_inside = _wrap_dylib_as_apple_framework(
                src_dy, dst_fw, suffix=suffix, app_platform="ios"
            )
            stripped_targets.append(bin_inside)

    if is_release:
        for binary in stripped_targets:
            _strip_packaged_binary(binary, app_platform)

    args_name = "args.gn" if enable_inspector else "args-noinspector.gn"
    _copy(out_dir / "args.gn", lib_dir / args_name)

    # --- headers ---
    jsi_src = sources_path / "src"

    for h in ("js_native_api_types.h", "js_native_api.h", "js_runtime_api.h"):
        _copy(jsi_src / "node-api" / h, inc_node_api / h)

    api_loader_files = (
        "JSRuntimeApi.cpp", "JSRuntimeApi.h", "JSRuntimeApi.inc",
        "NodeApi_win.cpp", "NodeApi.cpp", "NodeApi.h", "NodeApi.inc",
        "V8Api.cpp", "V8Api.h", "V8Api.inc",
    )
    for f in api_loader_files:
        _copy(
            jsi_src / "node-api-jsi" / "ApiLoaders" / f,
            inc_node_api_jsi_loaders / f,
        )
    for f in ("NodeApiJsiRuntime.cpp", "NodeApiJsiRuntime.h"):
        _copy(jsi_src / "node-api-jsi" / f, inc_node_api_jsi / f)

    public_dir = jsi_src / "public"
    for f in (
        "compat.h", "Readme.md", "ScriptStore.h",
        "v8_api.h", "V8JsiRuntime.h", "V8StructuredClone.h",
    ):
        # V8StructuredClone.h is new on this branch; copy if present.
        _copy(public_dir / f, include_root / f)

    for f in ("jsi.h", "jsi-inl.h", "jsi.cpp", "instrumentation.h"):
        _copy(jsi_src / "jsi" / f, inc_jsi / f)

    # --- misc / packaging glue ---
    _copy(
        sources_path / "ReactNative.V8Jsi.Windows.targets",
        output_path / "build" / "native" / "ReactNative.V8Jsi.Windows.targets",
    )
    _copy(
        sources_path / "ReactNative.V8Jsi.Windows.nuspec",
        output_path / "ReactNative.V8Jsi.Windows.nuspec",
    )
    _copy(sources_path / "config.json", output_path / "config.json")
    for lic in ("LICENSE", "LICENSE.jsi.md", "LICENSE.napi.md", "LICENSE.v8.md"):
        _copy(sources_path / lic, license_dir / lic)

    if fake_build:
        # Emit empty placeholders for the Windows-only PDB / .lib paths so
        # packaging-only smoke tests in CI still produce the same tree shape.
        for stem in ("v8jsi.dll", "v8jsi.dll.lib", "v8jsi.dll.pdb"):
            base, _, ext = stem.partition(".")
            (lib_dir / f"{base}{suffix}.{ext}").touch(exist_ok=True)

    print("Done!", flush=True)
