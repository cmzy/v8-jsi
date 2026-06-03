# V8 14.8 Upgrade / Hermes JSI Sync Notes

> Chinese version: [V8_14_UPGRADE.zh.md](./V8_14_UPGRADE.zh.md)

This document records every non-obvious code change and V8 build option
introduced while upgrading from V8 12.1 to 14.8 (Chrome 148 stable) and
syncing to the latest Hermes JSI (which brought in a new testlib). Each
entry names the symptom that prompted it and the underlying cause, so the
next V8 bump or port to a new platform has a paper trail instead of folklore.

---

## 1. V8Runtime Code Changes

### Wrap property accessors with TryCatch + ReportException

File: `src/V8JsiRuntime.cpp`
Scope: `getProperty(String/PropNameID)`, `hasProperty(String/PropNameID)`,
`setPropertyValue(PropNameID/String)`, `getValueAtIndex`, `setValueAtIndexImpl`.

V8 14 changes `MaybeLocal::ToLocalChecked()` to FATAL-abort when the
MaybeLocal is empty. Older code was casually written as:

```cpp
auto v = obj->Get(ctx, key).ToLocalChecked();   // V8 13: returns empty
                                                 // V8 14: abort()
```

The MaybeLocal is empty whenever JS throws — Proxy get traps, accessor
exceptions, JS-side getter throws — and ToLocalChecked kills the process.
The new template:

```cpp
v8::TryCatch tc(GetIsolate());
v8::MaybeLocal<v8::Value> result = objectRef(obj)->Get(GetContextLocal(), stringRef(name));
if (result.IsEmpty()) {
  if (tc.HasCaught()) ReportException(&tc);
  throw jsi::JSError(*this, "V8Runtime::getProperty failed.");
}
return createValue(result.ToLocalChecked());
```

### ReportException preserves non-Error / non-String thrown values

File: `src/V8JsiRuntime.cpp`, in `ReportException`.

Previously ReportException stringified any V8 exception into a JSError
message. But JS code can do `throw 72` / `throw {x:1}` / `throw Symbol(...)`,
and the original Value would get reduced to its toString() result. New logic:

```cpp
v8::Local<v8::Value> v8exception = try_catch->Exception();
if (!v8exception.IsEmpty()
    && !v8exception->IsNativeError()
    && !v8exception->IsString()) {
  // Not an Error or string — keep the raw Value path so JSError carries
  // the original object/number/symbol back to the host.
  throw jsi::JSError(*this, createValue(v8exception));
}
// Otherwise stay on the old message + stack formatting path.
```

`jsi::JSError(rt, Value&&)` stores the raw Value in its `value_` field,
so the catching code can recover it via `e.value()`.

### Use the `jsi::JSIException` hierarchy across the .so boundary

Files: `src/V8JsiRuntime_impl.h` (`HostObjectProxy::GetInternal/SetInternal`,
`HostFunctionProxy::call`), `src/makev8jsi.lst`.

The V8 monolith statically links libcxx into libv8jsi. Embedders use their
own libcxx (system on macOS/Linux, MSVC STL on Windows). The two `std::exception`
typeinfos are private-external and therefore not unified across the .so —
`catch (const std::exception&)` inside the dylib cannot match a
`std::runtime_error` thrown from the embedder, and the exception silently
falls through to `catch (...)`, losing its message.

Convention: embedders **must** throw `jsi::JSError` or
`jsi::JSINativeException`. The dylib catches `jsi::JSIException&` (a class
defined in jsi.h, so its typeinfo lives in the `facebook::jsi::*`
namespace and can be exported):

```cpp
} catch (const jsi::JSError &error) {
  isolate->ThrowException(runtime.valueReference(error.value()));
} catch (const jsi::JSIException &ex) {
  isolate->ThrowException(v8::Exception::Error(NewString(ex.what())));
} catch (...) {
  isolate->ThrowException(v8::Exception::Error(NewString("<unknown>")));
}
```

Supporting change: `src/makev8jsi.lst` explicitly exports
`_ZTIN8facebook3jsi*` / `_ZTSN8facebook3jsi*` / `_ZTVN8facebook3jsi*`
(typeinfo / typeinfo-name / vtable), so on Linux the flat namespace + version
script can unify these symbols by name across the boundary.

### JSStringToSTLString uses the V2 string API

V8 14 deprecates `String::Utf8Length(isolate)` and `String::WriteUtf8(...)`.
The V2 API:

```cpp
size_t utfLen = string->Utf8LengthV2(isolate);
std::string result;
result.resize(utfLen);
string->WriteUtf8V2(isolate, result.data(), utfLen);
return result;
```

The V1 return shape (int length + a NUL-termination flag) is gone in V8 14.

### Implement `isHostFunction` / `getHostFunction`

Files: `src/V8JsiRuntime.cpp`, `src/V8JsiRuntime_impl.h`.

These two methods had historically been stubbed as `std::abort()`. The
Hermes-imported HostFunctionTest now exercises them and aborts the test
binary. Note: with ICF (Identical Code Folding) enabled, the linker folds
every `{ std::abort(); }` function into a single address, and gdb reports
that address as `NoInstrumentation::writeBasicBlockProfileTraceToFile` —
which made the failure look like a vtable corruption. It's just an
unimplemented method.

Implementation: in `createFunctionFromHostFunction`, attach a `v8::Private`
symbol to the `v8::Function` whose value is a `v8::External` wrapping the
`HostFunctionProxy*`:

```cpp
v8::Local<v8::Private> hostFunctionPrivateKey(v8::Isolate *isolate) {
  return v8::Private::ForApi(isolate,
      v8::String::NewFromUtf8Literal(isolate, "$v8jsi$HostFunctionProxy"));
}

newFunction->SetPrivate(ctx, hostFunctionPrivateKey(iso), external).Check();
```

`isHostFunction` checks whether the private exists; `getHostFunction`
reads the External back, casts to `HostFunctionProxy*`, and returns its
`func_`. The proxy class grew a public `getHostFunction()` accessor.

### `V8Runtime::size(Array)` handles Proxy

File: `src/V8JsiRuntime.cpp`.

`v8::Array::Length()` reads the v8::Array's internal length slot, which a
Proxy doesn't have — Length() returns 0. The new ArrayPush test uses
`new Proxy([1], {})` with a RuntimeDecorator delegating to the default
`Runtime::push`:

```
default Runtime::push:
  newSize = size(arr);            // 0 — wrong! should be 1
  arr.setProperty(rt, 0, true);   // overwrote the proxied [1]
  arr.setProperty(rt, 1, "...");
```

Fix: detect Proxy, fall back to property read (Proxy's get trap returns
the right value):

```cpp
if (obj->IsProxy()) {
  v8::Local<v8::Value> len;
  if (!obj->Get(ctx, /*"length"*/...).ToLocal(&len)) { ... }
  return len->Uint32Value(ctx).FromMaybe(0);
}
return v8::Local<v8::Array>::Cast(obj)->Length();   // fast path for real arrays
```

### macOS / iOS: add `-Wl,-flat_namespace`

File: `src/BUILD.gn` (applied to both `jsitests` and `v8jsi` targets).

Same root cause as the cross-DSO exception story above, manifesting
differently on macOS. On Linux, `--version-script` exporting
`facebook::jsi::*` typeinfo plus the default flat namespace is enough to
unify the typeinfo addresses across the .so boundary. macOS defaults to a
two-level namespace, so the dylib and the executable each carry their own
typeinfo and `catch (jsi::JSIException&)` inside the dylib can't match
what the embedder throws.

Adding `-Wl,-flat_namespace` tells dyld to coalesce symbols by name across
all loaded images — the weak-external typeinfo symbols on both sides
collapse to one address, and cross-DSO catch works. The flag is marked
deprecated on macOS but still functions.

### Test-side adjustments

In `src/jsi/test/testlib.cpp` and `testlib_ext.cpp`, host callbacks that
used to throw `std::logic_error` / `std::runtime_error` were changed to
`jsi::JSINativeException`. Reasons:

- `JSError::what()` appends `"\n\n" + stack`, but the tests use
  exact-match string assertions — the stack suffix breaks them.
- `JSINativeException::what()` returns the constructor argument verbatim —
  what the tests want.

Constraint: any host callback in the test suite that needs its error to
reach JS **must** throw `JSError` or `JSINativeException`, not `std::*`
(this is the cost of the cross-DSO exception convention above).

---

## 2. V8 GN Build Options

The full list lives in `scripts/build_lib/build.py`'s `gn_args()`. Below
we list **non-default** flags that **have a debugging story behind them**:

### Common to all platforms

| Option | Value | Why |
|--------|-------|-----|
| `v8_monolithic` | `true` | Roll all internal V8 static libraries into one `obj/libv8_monolith.a` so the dylib link only deals with a single archive. |
| `v8_monolithic_for_shared_library` | `true` | Switch on by V8 14. Compiles V8's `thread_local` globals (`g_current_isolate_`, `g_current_local_heap_`) with the `local-dynamic` TLS model instead of `local-exec`. **Without this**, linking `libv8jsi.so` on Linux fails with `R_X86_64_TPOFF32` relocations against V8's thread_local globals. |
| `v8_enable_temporal_support` | `false` | V8 14 ships the Temporal proposal backed by a Rust crate `temporal_capi`. The monolith doesn't bundle that static lib, so leaving it on produces a flood of `temporal_rs_*` undefined-symbol link errors. Embedders that need Temporal must add the Rust dep themselves and flip this back to true. |
| `use_partition_alloc_as_malloc` | `false` | **The big trap.** V8 14 enables Chromium's PartitionAlloc as the process-wide malloc shim by default. That means libv8jsi's libcxx allocates `std::string` buffers from PartitionAlloc's pool, but the embedder (jsitests, a React Native app) frees them via glibc and crashes with `free(): invalid size`. Disable. |
| `use_allocator_shim` | `false` | Companion to the above — make sure everything goes through the system allocator. |
| `v8_enable_i18n_support` | `false` | Long-standing choice. No ICU, save ~10MB. |
| `v8_use_external_startup_data` | `false` | Embed the snapshot in the .so — no external `.bin` payload to ship. |
| `v8_enable_sandbox` | `false` | The Node-API external ArrayBuffer implementation doesn't satisfy V8 sandbox rules; enabling the sandbox breaks tests. |
| `is_component_build` | `false` | We want a single monolithic .so / .dylib / .dll. |

### 64-bit platforms

| Option | Value | Why |
|--------|-------|-----|
| `v8_enable_pointer_compression` | `true` | 32-bit compressed pointers on 64-bit: less memory, faster. Off by default on 32-bit platforms. |

### iOS only

| Option | Value | Why |
|--------|-------|-----|
| `target_environment` | `"device"` | Chromium iOS requires one of `device` / `simulator` / `catalyst`. Without it, gn gen errors out. Default to `device` so the build produces an arm64 framework suitable for installing on real hardware. |
| `v8_enable_webassembly` | `false` | iOS forbids JIT, V8 is built jitless. With wasm on, Torque references `WasmFuncRef` when generating builtin tables but the wasm `.tq` files aren't part of the first generation pass — link fails on missing definitions. |

### Windows only

| Option | Value | Why |
|--------|-------|-----|
| `use_custom_libcxx` | `false` | Hybrid CRT — reuse the system UCRT, don't pull in V8's bundled libcxx, stay ABI-compatible with MSVC. |
| `is_clang` | (varies) | Chosen to match the active MSVC toolchain. |

### Shared cflags worth knowing

Not in GN args, but visible in the compile command line:

| Flag | Note |
|------|------|
| `-fvisibility=hidden` | Inherited from the V8 toolchain. With this default, only `JSI_EXPORT` (`__attribute__((visibility("default")))`) members get exported from libv8jsi — `jsi.h`'s public types rely on this. |
| `-fno-rtti` (dylib) / `-frtti` (jsitests) | **Asymmetric.** The dylib follows V8 monolith with `-fno-rtti`. catch-clause typeinfo is still emitted (vague linkage / weak external) even under `-fno-rtti`, so cross-.so catch still works. |

Other flags such as `-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_EXTENSIVE`
are inherited from Chromium's defaults and listed here only because you'll
see them in the build commands — they control assertion density inside
libc++, not container layout or ABI, and the embedder is free to compile
with a different hardening mode.

### Linker flags (src/BUILD.gn)

| Platform | Flag | Why |
|----------|------|-----|
| Linux | `-Wl,--allow-multiple-definition` | The monolith contains duplicate symbols — let them slide. |
| Linux | `-Wl,--version-script=jsi/makev8jsi.lst` | Export only the jsi public API and `facebook::jsi::*` typeinfo, hide everything else. On Linux, the flat namespace + this script is what makes the cross-DSO `catch (jsi::JSIException&)` work. |
| macOS / iOS | `-Wl,-flat_namespace` | See §1. Tells dyld to coalesce weak-external typeinfo across the dylib/executable boundary. |
| Windows | `/OPT:REF /INCREMENTAL:NO` | Standard release-build flags. |

### Pitfall: `extra_cflags_cc`

The V8 monolith target does **not** expose `extra_cflags_cc`, the usual
GN entry point for custom compile flags. Passing it via `gn gen` produces
`Build argument has no effect.`; the flag is silently dropped. To add a
cflag specifically for v8jsi, edit `cflags += [...]` directly in the v8jsi
target inside `src/BUILD.gn`. Do **not** try to route it through `args.gn`.

---

## 3. Python Build Runner (`scripts/build_lib/`)

Non-obvious things in the runner:

- `fetch.py`
  - **Do not** prune `v8/third_party/rust-toolchain` — regardless of
    `enable_rust` or `v8_enable_temporal_support`, `build/config/rust.gni`
    unconditionally reads `rust-toolchain/VERSION` during gn gen on macOS,
    and a missing file aborts the build.
  - `sudo -n bash install-build-deps.sh` — `-n` makes non-interactive
    SSH / CI runs skip instead of blocking on a password prompt.
  - Before each gclient sync, `git reset --hard HEAD` on `v8/build` and
    `v8/third_party/zlib`, so leftover patches from a previous fetch
    don't trip the "dirty sub-repo" check.
  - On macOS, also stub `v8/third_party/protobuf`'s BUILD.gn with an
    empty group, because fuzztest references it after we prune the
    upstream copy.

- `build.py`
  - macOS / iOS require the `ASIO_ROOT` env var (hard-coded
    `getenv("ASIO_ROOT")` in `src/BUILD.gn`); the runner points it at
    `deps/asio` automatically.
  - On Linux, when a second fetch adds another `target_os`, the sync
    needs to reset dirty sub-repos as described above.

### Inspector variants

Every `build` / `all` invocation produces both an inspector-enabled and an
inspector-disabled binary by default (`--inspector both`; pass `with` /
`without` to skip one).

- Intermediate object dirs are separated per variant:
  `build/v8/out/<app>/<cpu>/<cfg>/{with-inspector,noinspector}/`.
- Packaged binaries land **side by side** in the same `lib_dir` (`out/lib/<app>/<cfg>/<cpu>/`).
  Default-named artifact is the with-inspector build (backward-compatible);
  the slim variant uses a `-noinspector` suffix.

Per-platform packaged layout:

| Platform | with-inspector | no-inspector | Notes |
|----------|----------------|--------------|-------|
| win32 | `v8jsi.dll` (+ `.dll.lib`, `.dll.pdb`) | `v8jsi-noinspector.dll` (+ `.dll.lib`, `.dll.pdb`) | Suffix applied to all three files. |
| mac | `libv8jsi.dylib` | `libv8jsi-noinspector.dylib` | |
| linux | `libv8jsi.so` | `libv8jsi-noinspector.so` | |
| android | `libv8jsi.so` (+ JNI-ABI alias) | `libv8jsi-noinspector.so` (+ JNI-ABI alias) | See "Android JNI mirroring" below. |
| ios | `libv8jsi.framework/` | `libv8jsi-noinspector.framework/` | Bundle dir + internal binary + `Info.plist::CFBundleExecutable` are all renamed together. |

For each variant the corresponding `args.gn` is also dropped into `lib_dir`:
`args.gn` (with-inspector) and `args-noinspector.gn`.

Inspector code is presently only wired up on Windows — the sources under
`src/inspector/` are gated on `is_win` in BUILD.gn and the call sites are
gated on `_WIN32 && V8JSI_ENABLE_INSPECTOR`. On non-Windows platforms the
two variants therefore produce binary-identical artifacts; we still ship
both so the packaging pipeline and embedder-side naming convention stay
uniform across platforms, and so a future POSIX inspector port slots in
without rearranging the output tree.

### Android JNI mirroring

Our GN `target_cpu` names (`x64`, `x86`, `arm64`) don't match the Android
NDK ABI names that Gradle's `jniLibs.srcDirs` expects (`x86_64`, `x86`,
`arm64-v8a`). When `app_platform == "android"`, the packager writes the
`.so` to **both** layouts:

- `out/lib/android/<cfg>/<gn-cpu>/libv8jsi.so` — legacy / GN-style path.
- `out/lib/android/<cfg>/<jni-abi>/libv8jsi.so` — drop-in for
  `jniLibs.srcDirs '...lib/android/<cfg>'` so the .aar pipeline doesn't
  need a rename step.

The mapping table lives in `scripts/build_lib/build.py` as
`_ANDROID_CPU_TO_ABI`:

| GN `target_cpu` | NDK ABI |
|-----------------|---------|
| `x64` | `x86_64` |
| `x86` | `x86` |
| `arm64` | `arm64-v8a` |
| `arm` | `armeabi-v7a` |

Each variant has to be built explicitly — `dev.py build --app-platform
android --platform <cpu>` once per ABI you want to ship.

### iOS framework packaging

Unlike the other platforms, the iOS GN output is a directory bundle
(`libv8jsi.framework/`) rather than a single file. The packager:

1. Copies the bundle to `lib_dir/libv8jsi<suffix>.framework/`.
2. Renames the internal binary so it matches the bundle name (Xcode
   refuses to load a framework where the binary doesn't match).
3. Patches `Info.plist::CFBundleExecutable` to the new name.

Symlinks inside the framework are preserved.

### Release-only size optimizations

Layered on top of the V8 14 defaults; only kick in when `is_debug=false`.
The split between "applies to everything" and "applies only to the v8jsi
shared library" is deliberate — the V8 monolith static archive is
**never** rebuilt through these knobs, so embedders pay only one V8
rebuild cost per V8 roll.

**Global GN args (`scripts/build_lib/build.py`)**

| Arg | Why |
|-----|-----|
| `is_official_build=true` | Chromium's "ship" optimization bundle: tighter inlining, drop DCHECKs, strip extra reflection. |
| `chrome_pgo_phase=0` | `is_official_build` implicitly enables PGO via a `tools/update_pgo_profiles.py` exec_script; V8 doesn't ship that script. |
| `use_thin_lto=false` + `thin_lto_enable_optimizations=false` | Override the implicit LTO that `is_official_build` would flip on — see "Per-target LTO" below for why. |
| `is_cfi=false` | `is_official_build` also turns on Control Flow Integrity on Linux/Android; CFI requires `use_thin_lto`, so they must move together. |
| `symbol_level=0` | Drop DWARF entirely. Saves ~10-15 MB on Linux/macOS vs the default `-g2`. |
| `v8_enable_disassembler=false` | `--print-code` / `--print-opt-code` disabled. Production embedders don't use them. |
| `v8_enable_object_print=false` | `Object::Print()` removed. |
| `v8_enable_gdbjit=false` | gdb JIT-interface descriptor table removed. |
| `v8_enable_v8_checks=false` | V8-internal CHECK macros expanded to no-ops in release. |
| `v8_enable_runtime_call_stats=false` | `--runtime-call-stats` CSV exporter removed. |
| `v8_enable_heap_snapshot_verify=false` | Heap-snapshot self-verification removed. |
| `v8jsi_enable_node_api=false` | **Functional**: drops the Node-API binding layer entirely. Embedders that consume v8jsi through N-API need a custom build with `v8jsi_enable_node_api=true`. |

**v8jsi shared-library-only cflags (`src/BUILD.gn`, Release)**

| Flag | Platform | Why |
|------|----------|-----|
| `-Os` / `/Os` | all | Favor small code in our ~20 TUs. V8 monolith stays on `-O2` since JS-execution perf matters more there. |
| `-flto=thin` (cflags + ldflags) | all (clang-cl only on Win) | **Per-target LTO**: our TUs become bitcode, lld LTO-codegens across them at shared-library link time, V8 monolith static archive is consumed as opaque .o — exactly the split the user asked for. Global `use_thin_lto` stays off so the V8 build doesn't pay the LTO cost. |
| `-fno-unique-section-names` | non-Win | Smaller section-name string table. |
| `-fno-plt` | Linux/Android | Skip the PLT/GOT indirection for extern calls. |

**v8jsi shared-library-only ldflags (`src/BUILD.gn`, all configs unless noted)**

| Flag | Platform | Why |
|------|----------|-----|
| `-Wl,--icf=all` | Linux/Android | Identical Code Folding. V8 builtins template-instantiate heavily; many fold cleanly. macOS gets this via V8 defaults. |
| `-Wl,--exclude-libs,ALL` | Linux/Android | Hide everything brought in from static archives so only `--version-script`-listed symbols hit `.dynsym`. |
| `-Wl,--hash-style=gnu` | Linux/Android | Smaller `.gnu.hash` vs SysV `.hash`. |
| `-Wl,-no_function_starts` | macOS/iOS | Drop `LC_FUNCTION_STARTS` load command (only `atos`/`leaks`/`dtrace` use it). |
| `-Wl,-no_data_in_code_info` | macOS/iOS | Drop `LC_DATA_IN_CODE` load command (empty on modern Mach-O). |

**Post-link strip (`_strip_packaged_binary` in `build.py`, Release only)**

After copying the binary into the packaged `lib_dir`, the script runs
`strip` on it. Dynamic exports (`.dynsym` on ELF, the Mach-O dynamic
symtab) are preserved so embedders can still link/load.

- Linux / Android: `strip --strip-all`
- macOS / iOS: `strip -S -x` (debug + local symbols)
- Windows: skipped — PDB is already separate, `/OPT:REF /OPT:ICF` handle the rest.

**Why no global LTO?**

The V8 build produces `libv8_monolith.a`, a ~1000-TU static archive. If
we set `use_thin_lto=true`, the V8 build pipeline re-compiles every TU
into bitcode (3-5x build-time cost) and lld then has to LTO-codegen the
whole thing on every shared-library link. There's also a steady stream
of `"object file is not bitcode"` warnings because some V8 archive
members (Rust ffi, prebuilt third-party blobs) intentionally stay
native. The pragmatic compromise: turn LTO on only at the v8jsi
shared-library boundary — our ~20 TUs participate, the V8 monolith
stays as it was.

---

## 4. Test Results

Running the full `jsitests` suite (60 tests including all the
Hermes-imported additions):

- **Linux x64 Release**: 60/60 PASS
- **macOS x64 Release**: 60/60 PASS
- Windows / Android / iOS: not covered this round

---

## 5. Known Constraints / TODO

- **HostObject / HostFunction implementations must throw `jsi::JSError`
  or `jsi::JSINativeException`**, not bare `std::runtime_error` and
  friends. The V8 monolith statically links libcxx, so std typeinfo is
  not shared across the .so boundary. This is a fundamental trade-off
  of the convention we adopted.
- `-Wl,-flat_namespace` is deprecated on macOS (warning, not failure).
  A proper fix would be to stop statically linking libcxx into the
  dylib, but that's a V8 monolith convention we'd need to fight.
- Temporal is off by default. Embedders that need it must link the
  `temporal_capi` Rust library themselves and flip
  `v8_enable_temporal_support`.
- iOS: only `device` is covered; simulator is untested.
- Android: only cross-compile is exercised this round, not tests.

---

## 6. What This Project Does to V8

This section describes the full surface the project touches on V8 itself,
so a future maintainer can reproduce or audit the integration.

### 6.1 V8 version pin

`config.json` carries `v8ref`, the git revision of `chromium/v8` that
`scripts/build_lib/fetch.py` checks out. The runner:

1. `fetch --no-history --nohooks v8` if `build/v8/v8` doesn't exist.
2. `git fetch origin <v8ref> && git checkout FETCH_HEAD` to pin.
3. `gclient runhooks` + `gclient sync` to pull `build/`, `third_party/`,
   the bundled toolchain, libcxx, libcxxabi, etc.

Re-pointing at a different V8 release is just editing `config.json`'s
`v8ref` and re-running `fetch`.

### 6.2 In-tree patches applied to the V8 checkout

All patches live in `scripts/patch/` and are applied unconditionally by
`fetch.py`. They are kept minimal so future V8 rolls have the smallest
possible surface to rebase.

#### `scripts/patch/src.diff` (applied to `build/v8/v8`)

- **Add a top-level `group("jsi")`** in `v8/BUILD.gn` whose only dep is
  `jsi:v8jsi`. This is the GN entry point we name from
  `out/<plat>/<cpu>/<cfg>/build.ninja` — building `jsi` builds our shared
  library.
- **Remove the `v8_clusterfuzz` / `v8_clusterfuzz_fallbacks` targets** and
  the `v8_correctness_fuzzer` deps on `d8`. These targets pull in
  Chromium's clusterfuzz tooling and the Foozzie experiment, none of
  which is relevant to a library build and all of which adds unsatisfiable
  deps.
- **Hook up `rc_win`** in `DEPS` so checkouts on Windows download the
  resource compiler binary needed by `version_gen.rc`. Upstream V8
  doesn't ship a Windows-only RC tool because its own binaries don't have
  Win32 resources; we do (the .dll has a `VERSIONINFO` block).
- **Suppress two `DCHECK_EQ` calls in `src/maglev/maglev-ir.h`** that
  cause static_assert-style failures when the calling templates are
  instantiated in the v8jsi build (`opcode_of<Derived>` and
  `kProperties` aren't evaluable in the configurations we ship).
- **Suppress a `DCHECK_EQ` in `src/heap/cppgc/marking-state.h`** —
  same kind of unevaluable check.
- **Route `GetPlatformPageAllocator()` through `V8::GetCurrentPlatform()`
  in `src/utils/allocation.cc`** instead of the static
  `GetPageAllocatorInitializer()`. Without this the page allocator
  pointer is `nullptr` when v8jsi exposes a custom `v8::Platform` via the
  embedding API.
- **Force `googletest` to compile with `rtti` + `exceptions`** in
  `third_party/googletest/BUILD.gn`. Our tests need RTTI for typeid
  comparisons; upstream V8 builds gtest with `no_rtti` / `no_exceptions`.
- **`comsuppwd.lib` → `comsuppw.lib`** in `tools/v8windbg/BUILD.gn` so
  Windows release builds don't try to link the debug-mode COM support
  library against a release CRT.

#### `scripts/patch/build.diff` (applied to `build/v8/build`)

- **Re-enable MSVC warnings `/wd4244` and `/wd4267`** by commenting out
  the upstream suppressions. These size-narrowing warnings were
  whitelisted in Chromium but our embedder build treats them as bugs
  worth surfacing.
- **Add a `win_msvc_cfg` config** that enables Control Flow Guard
  (`/guard:cf` + `/Qspectre /W3` cflags, `/guard:cf` ldflags) when
  building with MSVC, and attach it to every `default_crt` variant.
  Required by the Microsoft SDL pipeline that ships v8jsi.
- **Pass `-vcvars_spectre_libs=spectre`** in `toolchain/win/setup_toolchain.py`
  so the env loaded for the MSVC toolchain uses Spectre-mitigated runtime
  libraries.

#### `scripts/patch/zlib.diff` (applied to `build/v8/third_party/zlib`)

- Single hunk: re-enable MSVC warning `/wd4244` in zlib's
  `zlib_internal_config`, same reasoning as `build.diff`.

### 6.3 Subtrees pruned after sync

`fetch.py` deletes the following subtrees after gclient sync to shrink
the checkout and avoid pulling in deps the library doesn't use:

```
depot_tools/external_bin/gsutil
v8/test/test262/data/tools
v8/third_party/depot_tools/external_bin/gsutil
v8/third_party/perfetto
v8/third_party/protobuf
v8/third_party/rust
v8/bazel
v8/tools/clusterfuzz
v8/tools/package-lock.json
v8/tools/package.json
v8/tools/turbolizer
```

Explicitly **not** pruned (was tried, broke gn gen):

- `v8/third_party/rust-toolchain` — `build/config/rust.gni` reads
  `rust-toolchain/VERSION` unconditionally during configure, even when
  Rust deps are disabled.

### 6.4 GN entry point and where it lives

The v8jsi source tree is copied to `build/v8/jsi/` at configure time
(`_copy_jsi_tree` in `scripts/build_lib/build.py`). That gives us
`//jsi:v8jsi` and `//jsi:jsitests` as GN labels inside V8's source tree,
which lets us reuse V8's toolchain, libcxx, libcxxabi, and gtest without
patching V8 to know about an out-of-tree source root. The `group("jsi")`
patch in §6.2 wires `//jsi:v8jsi` up as a buildable top-level target.

The shared library output is then copied out to the NuGet-shaped layout
under `out/` for downstream consumers — see `scripts/build_lib/build.py`
for the exact mapping.

### 6.5 Things this project deliberately does **not** do to V8

- No changes to V8's C++ source files beyond the four small patches in
  `src.diff`. All embedder logic lives in `src/V8JsiRuntime.cpp` etc.,
  outside the V8 tree, and uses only V8's public `include/v8*.h` API.
- No fork of libcxx / libcxxabi — we use the ones V8 bundles, and accept
  the cross-DSO RTTI consequences described above.
- No changes to V8's `DEPS` file beyond the single `rc_win` hook needed
  on Windows.
- No fork of depot_tools — we use whatever V8 ships.
