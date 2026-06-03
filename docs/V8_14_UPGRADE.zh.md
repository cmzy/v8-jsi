# V8 14.8 升级 / Hermes JSI 同步说明

> English version: [V8_14_UPGRADE.md](./V8_14_UPGRADE.md)

记录从 V8 12.1 升到 14.8 (Chrome 148 stable) 并同步 Hermes 的最新 JSI
（含 testlib）过程中所有非显然的代码改动和 V8 编译选项调整。每条都附带触发问题
和原因，方便后续 V8 再升级或换平台时定位回归。

---

## 1. V8Runtime 代码改动

### Property accessor 用 TryCatch + ReportException 包装

文件：`src/V8JsiRuntime.cpp`
范围：`getProperty(String/PropNameID)`、`hasProperty(String/PropNameID)`、
`setPropertyValue(PropNameID/String)`、`getValueAtIndex`、`setValueAtIndexImpl`。

V8 14 把 `MaybeLocal::ToLocalChecked()` 改成在 empty 时 FATAL abort。
之前测试随手写：

```cpp
auto v = obj->Get(ctx, key).ToLocalChecked();   // V8 13: 返回 Empty
                                                 // V8 14: abort()
```

只要 JS 里抛了异常（包括 Proxy 的 get trap、accessor 抛错、JS getter throw），
ToLocalChecked 就把进程干掉。新模板：

```cpp
v8::TryCatch tc(GetIsolate());
v8::MaybeLocal<v8::Value> result = objectRef(obj)->Get(GetContextLocal(), stringRef(name));
if (result.IsEmpty()) {
  if (tc.HasCaught()) ReportException(&tc);
  throw jsi::JSError(*this, "V8Runtime::getProperty failed.");
}
return createValue(result.ToLocalChecked());
```

### ReportException 保留非 Error / 非 String 的抛出值

文件：`src/V8JsiRuntime.cpp` 的 `ReportException`。

之前 ReportException 把任何 V8 异常都转成 string 塞进 JSError 的 message。
但 JS `throw 72` / `throw {x:1}` / `throw Symbol(...)` 这类值经此一过就只剩
toString() 后的字符串，原 Value 信息丢失。新逻辑：

```cpp
v8::Local<v8::Value> v8exception = try_catch->Exception();
if (!v8exception.IsEmpty()
    && !v8exception->IsNativeError()
    && !v8exception->IsString()) {
  // 不是 Error/string —— 走 Value 保留路径，让 JSError 把原对象/数字带回去
  throw jsi::JSError(*this, createValue(v8exception));
}
// 否则按原来的 message + stack 拼接
```

`jsi::JSError(rt, Value&&)` 的 ctor 把原 Value 存进 `value_` 字段，调用方
`try { ... } catch (const JSError& e) { e.value() }` 拿到的就是原始 throw 值。

### 跨 .so 异常用 `jsi::JSIException` 体系

文件：`src/V8JsiRuntime_impl.h`（`HostObjectProxy::GetInternal/SetInternal`、
`HostFunctionProxy::call`）、`src/makev8jsi.lst`。

V8 monolith 把 libcxx 静态链接进 libv8jsi。embedder 端用系统 libcxx。
两边的 `std::exception` typeinfo 是 private-external，跨 .so 不一致 ——
dylib 里 `catch (const std::exception&)` 接不住 embedder 抛的
`std::runtime_error`，会一路落到 `catch (...)`，丢掉 message。

约定：embedder 必须抛 `jsi::JSError` 或 `jsi::JSINativeException`。dylib 改
catch `jsi::JSIException&`（它是 jsi 自己定义的，typeinfo 在
`facebook::jsi::*` 命名空间）：

```cpp
} catch (const jsi::JSError &error) {
  isolate->ThrowException(runtime.valueReference(error.value()));
} catch (const jsi::JSIException &ex) {
  isolate->ThrowException(v8::Exception::Error(NewString(ex.what())));
} catch (...) {
  isolate->ThrowException(v8::Exception::Error(NewString("<unknown>")));
}
```

配套：`src/makev8jsi.lst` 显式导出 `_ZTIN8facebook3jsi*` / `_ZTSN8facebook3jsi*` /
`_ZTVN8facebook3jsi*`（typeinfo / typeinfo-name / vtable），让 Linux flat
namespace + version script 能按名字 unify 这些符号。

### JSStringToSTLString 走 V8 V2 string API

V8 14 把 `String::Utf8Length(isolate)` 和 `String::WriteUtf8(...)` 标 deprecated。
新 API：

```cpp
size_t utfLen = string->Utf8LengthV2(isolate);
std::string result;
result.resize(utfLen);
string->WriteUtf8V2(isolate, result.data(), utfLen);
return result;
```

V1 的旧返回值（int + nullptr terminated flag 那一坨）在 V8 14 已经移除。

### 实现 `isHostFunction` / `getHostFunction`

文件：`src/V8JsiRuntime.cpp`、`src/V8JsiRuntime_impl.h`。

仓库历史上这俩函数一直是 `std::abort()`，新 Hermes testlib 的 HostFunctionTest
调到了，整个测试进程 abort。注意：因为编译开了 ICF (Identical Code Folding)，
所有 `{ std::abort(); }` 函数被 linker 折成同一个地址，gdb 把它误报成了
`NoInstrumentation::writeBasicBlockProfileTraceToFile`，看着像 vtable 错乱，
其实就是 `isHostFunction` 没实现。

实现思路：在 `createFunctionFromHostFunction` 里给 `v8::Function` 挂一个
`v8::Private` symbol，value 是包着 `HostFunctionProxy*` 的 `v8::External`：

```cpp
v8::Local<v8::Private> hostFunctionPrivateKey(v8::Isolate *isolate) {
  return v8::Private::ForApi(isolate,
      v8::String::NewFromUtf8Literal(isolate, "$v8jsi$HostFunctionProxy"));
}

newFunction->SetPrivate(ctx, hostFunctionPrivateKey(iso), external).Check();
```

`isHostFunction` 查 private 存在与否；`getHostFunction` 取回 External，
cast 回 `HostFunctionProxy*`，返回它的 `func_`。Proxy 类加了 public accessor
`getHostFunction()` 暴露 func_。

### `V8Runtime::size(Array)` 兼容 Proxy

文件：`src/V8JsiRuntime.cpp`。

`v8::Array::Length()` 读的是 v8::Array 的内部 length slot，而 Proxy 没这个
slot —— 返回 0。新测试 ArrayPush 用 `new Proxy([1], {})` 配上
RuntimeDecorator delegating to 默认 `Runtime::push`，链路是：

```
default Runtime::push:
  newSize = size(arr);            // 0（错的！应该 1）
  arr.setProperty(rt, 0, true);   // 覆盖了原 [1]
  arr.setProperty(rt, 1, "...");
```

修复：检测 Proxy，走 JS 属性读取路径，Proxy 的 get trap 会正确返回 length。

```cpp
if (obj->IsProxy()) {
  v8::Local<v8::Value> len;
  if (!obj->Get(ctx, /*"length"*/...).ToLocal(&len)) { ... }
  return len->Uint32Value(ctx).FromMaybe(0);
}
return v8::Local<v8::Array>::Cast(obj)->Length();   // 普通数组走快路径
```

### macOS / iOS 加 `-Wl,-flat_namespace`

文件：`src/BUILD.gn`（jsitests 和 v8jsi 两个 target 都加）。

跟上面跨 .so 异常同一个根因的 macOS 表现。Linux 上 `--version-script` 导出
`facebook::jsi::*` typeinfo + flat namespace 自动 unify 就够了；macOS 默认
是 two-level namespace，dylib 和 executable 各持一份 typeinfo，
`catch (jsi::JSIException&)` 跨 dylib 接不住。

加 `-Wl,-flat_namespace` 让 dyld 按符号名 coalesce —— weak external typeinfo
在两边合并到同一个地址，跨 dylib catch 就 work 了。`flat_namespace` 在 macOS
上有 deprecation 警告但功能正常。

### 测试侧调整

`src/jsi/test/testlib.cpp` 和 `testlib_ext.cpp` 里 host callback 抛出
`std::logic_error` / `std::runtime_error` 的地方改成 `jsi::JSINativeException`。
原因：

- `JSError::what()` 会拼上 `"\n\n" + stack`，但测试用的是 exact-match
  字符串断言，stack 一加就过不了
- `JSINativeException::what()` 直接返回构造时的字符串，干净

约束：tests 里凡是要让 host callback 抛错给 JS 接住的，**必须**用
`JSError` 或 `JSINativeException`，不能用 `std::*`（跨 .so 异常约定的代价）。

---

## 2. V8 GN 编译选项

完整列表在 `scripts/build_lib/build.py` 的 `gn_args()` 里。下面只列**非默认**且
**有踩坑历史**的：

### 共用（所有平台）

| 选项 | 值 | 为什么 |
|------|-----|--------|
| `v8_monolithic` | `true` | 把所有 V8 内部静态库 link 成一个 `obj/libv8_monolith.a`，dylib 链接时只 deal with 一个 archive。|
| `v8_monolithic_for_shared_library` | `true` | V8 14 加的开关，把 `thread_local` 全局变量（`g_current_isolate_`、`g_current_local_heap_`）从 `local-exec` TLS 模型切到 `local-dynamic`。**不开**的话 Linux 上链 `libv8jsi.so` 直接报 `R_X86_64_TPOFF32` 重定位错误。|
| `v8_enable_temporal_support` | `false` | V8 14 自带 Temporal 提案的 Rust 实现 (`temporal_capi`)。monolith 不带这个 Rust 静态库，开着会报一堆 `temporal_rs_*` 未定义符号。**要 Temporal 的 embedder 自己加这个 dep**。|
| `use_partition_alloc_as_malloc` | `false` | **核心坑**。V8 14 默认把 Chromium 的 PartitionAlloc 当 process-wide malloc shim。结果 dylib 里 libcxx 的 `std::string` buffer 走 PartitionAlloc pool 分配，embedder（jsitests 或 RN app）用 glibc free()，崩在 `free(): invalid size` 上。关掉。|
| `use_allocator_shim` | `false` | 配合上一条，彻底走系统 allocator。|
| `v8_enable_i18n_support` | `false` | 老选项，不带 ICU，省 ~10MB。|
| `v8_use_external_startup_data` | `false` | snapshot 编进 .so，无外部 `.bin` 文件。|
| `v8_enable_sandbox` | `false` | Node-API external ArrayBuffer 实现不满足 V8 sandbox 规则；开了 sandbox 测试就跑不了。|
| `is_component_build` | `false` | 我们就要单一 monolithic .so/.dylib/.dll。|

### 64 位平台

| 选项 | 值 | 为什么 |
|------|-----|--------|
| `v8_enable_pointer_compression` | `true` | 64 位下用 32 位压缩指针，省内存 + 提速。32 位平台默认关。|

### iOS 专用

| 选项 | 值 | 为什么 |
|------|-----|--------|
| `target_environment` | `"device"` | Chromium iOS 配置三选一：`device` / `simulator` / `catalyst`。不指定 gn gen 直接报错。默认 device 产 arm64 framework。|
| `v8_enable_webassembly` | `false` | iOS 上 JIT 被禁，V8 jitless；wasm 关掉，不然 Torque 生成 builtin 表时引用 `WasmFuncRef` 但 wasm `.tq` 没参与首次生成，链接报缺定义。|

### Windows 专用

| 选项 | 值 | 为什么 |
|------|-----|--------|
| `use_custom_libcxx` | `false` | Hybrid CRT：复用系统 UCRT，不要 V8 自带的 libcxx，跟 MSVC ABI 对齐。|
| `is_clang` | 视情况 | 跟 MSVC 工具链选择一致。|

### 值得知道的几个 cflag

不在 GN args 里，但在编译命令行能看到：

| 选项 | 备注 |
|------|------|
| `-fvisibility=hidden` | 从 V8 toolchain 继承下来的默认。配上这个默认，只有 `JSI_EXPORT` 宏（= `__attribute__((visibility("default")))`）标记的成员才会从 libv8jsi 导出 —— `jsi.h` 的公共类型靠这个生效。|
| `-fno-rtti`（dylib）/ `-frtti`（jsitests） | **不对称**。dylib 跟 V8 monolith 走 `-fno-rtti`。但 catch 用的 typeinfo 编译器即便在 `-fno-rtti` 下也会按需 emit（vague linkage / weak external），所以跨 .so catch 还能用。|

其它像 `-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_EXTENSIVE` 这种是从
Chromium 默认继承的，列在这里只是因为编译命令里会看到 —— 它控制的是 libc++
内部断言密度，不改容器布局也不改 ABI，embedder 自己用别的 hardening mode
编译都没问题。

### 链接器选项（src/BUILD.gn）

| 平台 | 选项 | 为什么 |
|------|------|--------|
| Linux | `-Wl,--allow-multiple-definition` | monolith 里有重复符号，放行。|
| Linux | `-Wl,--version-script=jsi/makev8jsi.lst` | 只导出 jsi 公共 API 和 `facebook::jsi::*` typeinfo，其他符号全部 hide。Linux 上 flat namespace + 这份 script 是跨 .so `catch (jsi::JSIException&)` work 的关键。|
| macOS / iOS | `-Wl,-flat_namespace` | 见 §1。让 dyld 按符号名 coalesce 跨 dylib 的 weak external typeinfo。|
| Windows | `/OPT:REF /INCREMENTAL:NO` | 标准发布配置。|

### 不要踩的坑：`extra_cflags_cc`

V8 monolith 的 GN target **没有** expose `extra_cflags_cc` 这个常见自定义编译
flag 的入口。`gn gen` 时给它会输出 `Build argument has no effect.` 警告，
flag 不会传到 v8jsi target。如果需要给 v8jsi 单独加 cflag，在 `src/BUILD.gn`
的 v8jsi target 里直接 `cflags += [...]`，**不要**走 args.gn。

---

## 3. Python build runner（`scripts/build_lib/`）

新增 / 调整的细节：

- `fetch.py`
  - **不要**裁剪 `v8/third_party/rust-toolchain` —— macOS 上 gn gen 不论
    `enable_rust` / `v8_enable_temporal_support` 是不是 false，`build/config/rust.gni`
    都会无条件读 `rust-toolchain/VERSION`，没了就 gn gen 失败。
  - `sudo -n bash install-build-deps.sh` —— `-n` 让非交互的 SSH/CI 环境
    skip 而不是卡在密码提示。
  - gclient sync 前先 `git reset --hard HEAD` `v8/build` 和
    `v8/third_party/zlib`，防止上次留下来的 patch 让 sync 报 "dirty"。
  - macOS 上还要手动 stub `v8/third_party/protobuf` 的 BUILD.gn 空 group，
    因为 fuzztest 会引用它但我们裁掉了原版。

- `build.py`
  - macOS / iOS 上需要 `ASIO_ROOT` 环境变量（src/BUILD.gn 里写死了
    `getenv("ASIO_ROOT")`），runner 自动指向 `deps/asio`。
  - Linux 的 sync 在第二次 fetch（追加另一个 `target_os`）时需要 reset
    dirty sub-repos，见上。

### Inspector 变体

每次 `build` / `all` 默认会**同时**产出带 inspector 和不带 inspector 两份
二进制（`--inspector both`；传 `with` / `without` 跳过其中一个）。

- 中间对象目录按 variant 隔开：
  `build/v8/out/<app>/<cpu>/<cfg>/{with-inspector,noinspector}/`。
- 打包后的二进制**并排**放在同一个 `lib_dir`（`out/lib/<app>/<cfg>/<cpu>/`），默认命名是带 inspector 的（向后兼容），不带 inspector 的加 `-noinspector` 后缀。

各平台打包布局：

| 平台 | 带 inspector | 不带 inspector | 备注 |
|------|--------------|----------------|------|
| win32 | `v8jsi.dll`（+ `.dll.lib`、`.dll.pdb`） | `v8jsi-noinspector.dll`（+ `.dll.lib`、`.dll.pdb`） | 三个文件都加后缀。|
| mac | `libv8jsi.dylib` | `libv8jsi-noinspector.dylib` | |
| linux | `libv8jsi.so` | `libv8jsi-noinspector.so` | |
| android | `libv8jsi.so`（+ JNI-ABI 别名路径） | `libv8jsi-noinspector.so`（+ JNI-ABI 别名路径） | 见下方"Android JNI 镜像"。|
| ios | `libv8jsi.framework/` | `libv8jsi-noinspector.framework/` | bundle 目录 + 内部二进制 + `Info.plist::CFBundleExecutable` 一起改名。|

每个 variant 对应的 `args.gn` 也存进 `lib_dir`：
`args.gn`（带 inspector）和 `args-noinspector.gn`。

Inspector 代码目前只在 Windows 上真的接通了 —— `src/inspector/` 下的源
文件在 BUILD.gn 里被 `is_win` 包着，调用点都被 `_WIN32 && V8JSI_ENABLE_INSPECTOR`
双重 gate。非 Windows 平台上两个 variant 因此产出**完全相同**的二进制；
我们仍然出两份，是为了让打包流水线和 embedder 侧的命名约定全平台一致，
将来 inspector 移植到 POSIX 后能无缝接上。

### Android JNI 镜像

我们的 GN `target_cpu` 命名（`x64`、`x86`、`arm64`）跟 Android NDK ABI
（Gradle `jniLibs.srcDirs` 期待的 `x86_64`、`x86`、`arm64-v8a`）不一致。
`app_platform == "android"` 时打包步骤会把 `.so` **同时**写到两套布局：

- `out/lib/android/<cfg>/<gn-cpu>/libv8jsi.so` —— 旧的 GN 风格路径。
- `out/lib/android/<cfg>/<jni-abi>/libv8jsi.so` —— 直接 drop 给
  `jniLibs.srcDirs '...lib/android/<cfg>'` 用，AAR 流水线不再需要中间
  rename 脚本。

映射表写在 `scripts/build_lib/build.py` 的 `_ANDROID_CPU_TO_ABI`；后续若
要加 32 位 Android target，在那里补 `"arm" -> "armeabi-v7a"` 一条即可。

### iOS framework 打包

iOS 跟其他平台不同 —— GN 产物是一个目录 bundle（`libv8jsi.framework/`），
不是单文件。打包步骤：

1. 把 bundle 拷到 `lib_dir/libv8jsi<suffix>.framework/`。
2. 改名 bundle 内部的二进制，让它跟 bundle 名一致（Xcode 拒绝加载
   binary 名和 bundle 名对不上的 framework）。
3. 修正 `Info.plist::CFBundleExecutable` 改成新名字。

framework 内部的 symlink 会保留。

### Release 专属的体积优化

叠加在 V8 14 默认之上，**只在 `is_debug=false` 时生效**。"全局"和"只
应用到 v8jsi 共享库"的拆分是刻意的 —— V8 monolith 静态库**永远**不
会因为这些 knob 重编，embedder 每次 V8 升级只付一次 V8 编译代价。

**全局 GN args（`scripts/build_lib/build.py`）**

| 选项 | 为什么 |
|------|--------|
| `is_official_build=true` | Chromium 的 ship 优化套餐：更紧的 inlining、丢掉 DCHECK、去多余 reflection。|
| `chrome_pgo_phase=0` | `is_official_build` 会隐式开 PGO，调 `tools/update_pgo_profiles.py` —— V8 不带这个脚本，关掉避免 gn gen 挂掉。|
| `use_thin_lto=false` + `thin_lto_enable_optimizations=false` | 覆盖 `is_official_build` 隐式开的全局 LTO —— 见下面"Per-target LTO"那条解释为什么。|
| `is_cfi=false` | Linux/Android 上 `is_official_build` 会顺带开 CFI；CFI 又强制要 LTO，所以必须一起关。|
| `symbol_level=0` | 彻底丢 DWARF。比 V8 默认 `-g2` 在 Linux/macOS 各省 10-15 MB。|
| `v8_enable_disassembler=false` | 关 `--print-code` / `--print-opt-code`。生产 embedder 用不到。|
| `v8_enable_object_print=false` | 去掉 `Object::Print()`。|
| `v8_enable_gdbjit=false` | 去掉 gdb JIT 接口表。|
| `v8_enable_v8_checks=false` | V8 内部 CHECK 宏在 release 下变 no-op。|
| `v8_enable_runtime_call_stats=false` | 去掉 `--runtime-call-stats` 输出器。|
| `v8_enable_heap_snapshot_verify=false` | 去掉堆快照自检。|
| `v8jsi_enable_node_api=false` | **功能性**：完全不编 Node-API 绑定层。要 N-API 的 embedder 必须自己重编打开 `v8jsi_enable_node_api=true`。|

**v8jsi 共享库专属 cflag（`src/BUILD.gn`，仅 Release）**

| 选项 | 平台 | 为什么 |
|------|------|--------|
| `-Os` / `/Os` | 全 | 我们这 ~20 个 TU 走小代码优先。V8 monolith 保持 `-O2`（JS 跑分优先）。|
| `-flto=thin`（cflags + ldflags） | 全（Windows 仅 clang-cl） | **Per-target LTO**：我们的 TU 编成 bitcode，lld 链接 shared library 时跨这些 TU LTO codegen；V8 monolith 静态归档作为不透明 .o 被吞 —— 正是你要的"so 包开 LTO、静态库不开 LTO"。全局 `use_thin_lto` 保持 off，V8 不用重编。|
| `-fno-unique-section-names` | 非 Win | 缩小 section-name 字符串表。|
| `-fno-plt` | Linux/Android | extern 调用跳过 PLT/GOT 间接。|

**v8jsi 共享库专属 ldflag（`src/BUILD.gn`，除非另注）**

| 选项 | 平台 | 为什么 |
|------|------|--------|
| `-Wl,--icf=all` | Linux/Android | Identical Code Folding。V8 builtins 模板实例化多，能折叠不少。macOS 走 V8 默认。|
| `-Wl,--exclude-libs,ALL` | Linux/Android | 静态归档里的符号全 hide，只有 `--version-script` 列的进 `.dynsym`。|
| `-Wl,--hash-style=gnu` | Linux/Android | `.gnu.hash` 比 SysV `.hash` 紧凑。|
| `-Wl,-no_function_starts` | macOS/iOS | 干掉 `LC_FUNCTION_STARTS` load command（只有 `atos`/`leaks`/`dtrace` 用）。|
| `-Wl,-no_data_in_code_info` | macOS/iOS | 干掉 `LC_DATA_IN_CODE`（现代 Mach-O 是空的）。|

**Post-link strip（`build.py::_strip_packaged_binary`，仅 Release）**

把二进制拷到 `lib_dir` 后跑 `strip`。动态导出表（ELF 的 `.dynsym`、
Mach-O 的 dynamic symtab）保留，embedder 还能正常 link/load。

- Linux / Android：`strip --strip-all`
- macOS / iOS：`strip -S -x`（debug + local 符号）
- Windows：跳过 —— PDB 已分离，`/OPT:REF /OPT:ICF` 干剩下的。

**为什么不开全局 LTO？**

V8 build 出的是 `libv8_monolith.a`，一个 ~1000 TU 的静态库。如果设
`use_thin_lto=true`，V8 的整个 build pipeline 都要重编成 bitcode（3-5
倍编译时间），lld 每次链 shared library 都得 LTO codegen 整个 archive。
还有一堆 `"object file is not bitcode"` warning，因为 V8 archive 里
有些成员（Rust ffi、预编 third_party 二进制）就是 native 的。务实折
中：LTO 只在 v8jsi shared library 这层开 —— 我们的 ~20 个 TU 参与，
V8 monolith 原样吞。

---

## 4. 测试结果

跑 `jsitests` 全套（60 个测试，包括 Hermes 新加的）：

- **Linux x64 Release**：60/60 PASS
- **macOS x64 Release**：60/60 PASS
- Windows / Android / iOS：未在本轮覆盖

---

## 5. 已知约束 / TODO

- **HostObject / HostFunction 实现必须抛 `jsi::JSError` 或
  `jsi::JSINativeException`**，不能直接抛 `std::runtime_error`
  之类。原因是 V8 monolith 静态链接 libcxx，std typeinfo 跨 .so 不通用。
  这是我们采用的约定的固有 trade-off。
- `-Wl,-flat_namespace` 在 macOS 上是 deprecated（不会失败，但有警告）。
  根治方案是让 dylib 不静态链 libcxx，但 V8 monolith 的标准配置就是
  静态 libcxx，改起来动 V8。
- Temporal 提案默认关。要用的 embedder 需要自己 link `temporal_capi` Rust
  库，然后翻开 `v8_enable_temporal_support`。
- iOS：当前只覆盖 device，simulator 没测。
- Android：本轮没跑测试，只跑了交叉编译。

---

## 6. 本项目对 V8 都做了什么

这一节梳理本项目接触 V8 的全部范围，方便后续维护时复现或审计。

### 6.1 V8 版本钉死

`config.json` 里的 `v8ref` 字段记录了 `chromium/v8` 的 git revision，
`scripts/build_lib/fetch.py` 按它 checkout：

1. 若 `build/v8/v8` 不存在，执行 `fetch --no-history --nohooks v8`。
2. `git fetch origin <v8ref> && git checkout FETCH_HEAD` 锁版本。
3. `gclient runhooks` + `gclient sync` 拉 `build/`、`third_party/`、自带
   工具链、libcxx、libcxxabi 等。

切到别的 V8 版本只需要改 `config.json` 的 `v8ref` 再跑一次 `fetch`。

### 6.2 应用到 V8 checkout 的 in-tree patch

所有 patch 都在 `scripts/patch/` 下，`fetch.py` 无条件 apply。patch 保持
尽量小，方便以后 V8 升级时 rebase。

#### `scripts/patch/src.diff`（apply 到 `build/v8/v8`）

- **在 `v8/BUILD.gn` 里加一个顶层 `group("jsi")`**，仅依赖 `jsi:v8jsi`。
  这是我们在 `out/<plat>/<cpu>/<cfg>/build.ninja` 里指定的 GN 入口 ——
  build `jsi` 就是 build 我们的 shared library。
- **删除 `v8_clusterfuzz` / `v8_clusterfuzz_fallbacks` target**，以及
  `d8` 上的 `v8_correctness_fuzzer` 依赖。这些是 Chromium 的 clusterfuzz
  工具和 Foozzie 实验，对 library build 无关，且引入了我们无法满足的依赖。
- **`DEPS` 加 `rc_win` hook**，让 Windows 上 checkout 时下载 resource
  compiler 二进制（`version_gen.rc` 需要）。upstream V8 不带 Windows RC
  工具，因为它自己的二进制没有 Win32 resources；我们的有（.dll 有
  `VERSIONINFO` 段）。
- **注释掉 `src/maglev/maglev-ir.h` 里两处 `DCHECK_EQ`**，这两处在调用
  模板被实例化时会触发类似 static_assert 的失败（在我们 ship 的配置下
  `opcode_of<Derived>` 和 `kProperties` 不可求值）。
- **注释掉 `src/heap/cppgc/marking-state.h` 里一处 `DCHECK_EQ`** ——
  同一类不可求值检查。
- **把 `src/utils/allocation.cc` 里 `GetPlatformPageAllocator()` 改走
  `V8::GetCurrentPlatform()`**，不要走静态 `GetPageAllocatorInitializer()`。
  不改的话，v8jsi 通过 embedding API 暴露自定义 `v8::Platform` 时
  page allocator 指针是 `nullptr`。
- **强制 `googletest` 用 `rtti` + `exceptions` 编译**（在
  `third_party/googletest/BUILD.gn`）。我们的测试需要 RTTI 做 typeid 比较；
  upstream V8 把 gtest 编成 `no_rtti` / `no_exceptions`。
- **`comsuppwd.lib` → `comsuppw.lib`**（`tools/v8windbg/BUILD.gn`）——
  避免 Windows release 构建尝试用 debug 模式的 COM support 库去链 release CRT。

#### `scripts/patch/build.diff`（apply 到 `build/v8/build`）

- **重新打开 MSVC 警告 `/wd4244` 和 `/wd4267`**（注释掉 upstream 的抑制）。
  Chromium 把这些 size-narrowing 警告加了 whitelist，但我们的 embedder
  build 把它们当真正的 bug 关注。
- **新增 `win_msvc_cfg` 配置**，MSVC 下开 Control Flow Guard
  （cflags `/guard:cf` + `/Qspectre /W3`，ldflags `/guard:cf`），并挂到
  每个 `default_crt` 变体上。微软 SDL 流水线 ship v8jsi 时要求。
- **`toolchain/win/setup_toolchain.py` 加 `-vcvars_spectre_libs=spectre`**，
  让 MSVC 工具链 env 用 Spectre mitigated 的运行时库。

#### `scripts/patch/zlib.diff`（apply 到 `build/v8/third_party/zlib`）

- 一个 hunk：把 zlib 的 `zlib_internal_config` 里 MSVC 警告 `/wd4244`
  重新打开，理由同 `build.diff`。

### 6.3 sync 后被裁掉的子目录

`fetch.py` 在 gclient sync 后删掉这些子目录，缩小 checkout、避免引入用不到
的依赖：

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

特别**不**裁的（裁了会让 gn gen 挂）：

- `v8/third_party/rust-toolchain` —— `build/config/rust.gni` 在 configure
  时无条件读 `rust-toolchain/VERSION`，哪怕 Rust 依赖全关也一样。

### 6.4 GN 入口在哪儿

v8jsi 的源码树在 configure 时被复制到 `build/v8/jsi/`
（`scripts/build_lib/build.py` 的 `_copy_jsi_tree`）。这样 V8 源码树里就
有 `//jsi:v8jsi` 和 `//jsi:jsitests` 两个 GN 标签可用，我们能直接复用 V8
的工具链、libcxx、libcxxabi、gtest，而不用改 V8 让它认识外部的源码根目录。
§6.2 的 `group("jsi")` patch 把 `//jsi:v8jsi` 接入了顶层可构建目标。

shared library 产物再被复制到 `out/` 下 NuGet 风格的目录，给下游消费者用 ——
具体映射看 `scripts/build_lib/build.py`。

### 6.5 本项目**没有**对 V8 做的事

- 没有改 V8 的 C++ 源码（除了 `src.diff` 里那四处小补丁）。所有 embedder
  逻辑都在 `src/V8JsiRuntime.cpp` 等文件里，在 V8 源码树之外，**只**用 V8
  的公开 `include/v8*.h` API。
- 没有 fork libcxx / libcxxabi —— 用的就是 V8 自带的，并接受上文提到的
  跨 .so RTTI 后果。
- 除了 Windows 上需要的 `rc_win` hook，**没有**改 V8 的 `DEPS`。
- 没有 fork depot_tools —— 用 V8 自带的。
