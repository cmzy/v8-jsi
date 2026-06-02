"""Generate GN, drive Ninja, and assemble the NuGet-shaped output tree.

Mirrors ``scripts/build.ps1``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import env


def _gn_arg_string(
    *,
    platform_cpu: str,
    configuration: str,
    app_platform: str,
    use_clang: bool,
    use_libcpp: bool,
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
        "v8_use_external_startup_data=false",
        "treat_warnings_as_errors=false",
    ]

    if not building_windows:
        flags.append("use_goma=false")
        flags.append(f'target_os="{app_platform}"')
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
    for name in ("v8jsi.dll", "libv8jsi.so", "libv8jsi.dylib"):
        candidate = out_dir / name
        if candidate.exists():
            return candidate
    raise SystemExit(
        f"build appears to have failed: no v8jsi shared library found in {out_dir}"
    )


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
    )

    out_dir = v8 / "out" / app_platform / platform_cpu / configuration

    print(f"gn command line: gn gen {out_dir} --args='{gn_args}'", flush=True)
    if not fake_build:
        env.run(["gn", "gen", str(out_dir), f"--args={gn_args}"], cwd=v8)
    else:
        print("gn command skipped: fake build", flush=True)

    jobs = env.cpu_count_for_build()

    ninja_targets = ["v8jsi"]
    if app_platform != "android":
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
) -> None:
    """Drop binaries + headers into the NuGet layout the .nuspec expects."""
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

    # --- binaries ---
    if app_platform == "win32":
        _copy(out_dir / "v8jsi.dll", lib_dir / "v8jsi.dll")
        _copy(out_dir / "v8jsi.dll.lib", lib_dir / "v8jsi.dll.lib")
        if platform_cpu == "arm64":
            _copy(
                out_dir / "v8jsi_stripped.dll.pdb",
                lib_dir / "v8jsi.dll.pdb",
            )
        else:
            _copy(out_dir / "v8jsi.dll.pdb", lib_dir / "v8jsi.dll.pdb")
    elif app_platform == "mac":
        _copy(out_dir / "libv8jsi.dylib", lib_dir / "libv8jsi.dylib")
    elif app_platform in ("linux", "android"):
        _copy(out_dir / "libv8jsi.so", lib_dir / "libv8jsi.so")

    _copy(out_dir / "args.gn", lib_dir / "args.gn")

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
        for fname in ("v8jsi.dll", "v8jsi.dll.lib", "v8jsi.dll.pdb"):
            (lib_dir / fname).touch(exist_ok=True)

    print("Done!", flush=True)
