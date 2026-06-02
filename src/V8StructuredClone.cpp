// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
//
// Implementation of v8runtime::IStructuredClone on top of V8's native
// ValueSerializer / ValueDeserializer. The public-facing methods live as
// V8Runtime members; the V8 Delegate subclasses are file-local in the
// v8runtime::detail namespace and friended by V8Runtime so they can reach
// the runtime's internal jsi <-> v8 conversion helpers.

#include "public/V8StructuredClone.h"

#include "V8JsiRuntime_impl.h"

#include "v8.h"

#include <cassert>
#include <cstdlib>
#include <optional>
#include <string>
#include <utility>
#include <vector>

using facebook::jsi::JSError;
using facebook::jsi::Object;
using facebook::jsi::Runtime;
using facebook::jsi::Value;

namespace v8runtime {
namespace detail {

namespace {

// Prefix that the host (AmeCanvas) recognizes as "this was a structured-clone
// rejection" so it can re-raise the error as a DOMException("DataCloneError").
constexpr const char *kDataCloneErrorPrefix = "DataCloneError: ";

// Read a v8::Local<v8::String> into a std::string. Returns empty string on
// failure (e.g. empty handle), never throws.
std::string ToStdString(v8::Isolate *isolate, v8::Local<v8::String> str) {
  if (str.IsEmpty()) return {};
  v8::String::Utf8Value u(isolate, str);
  return *u ? std::string(*u, static_cast<size_t>(u.length())) : std::string{};
}

std::string ToStdString(v8::Isolate *isolate, v8::Local<v8::Value> v) {
  if (v.IsEmpty()) return {};
  v8::String::Utf8Value u(isolate, v);
  return *u ? std::string(*u, static_cast<size_t>(u.length())) : std::string{};
}

} // namespace

// ---------------------------------------------------------------------------
// SerializerDelegate
// ---------------------------------------------------------------------------

struct SerializerDelegate final : v8::ValueSerializer::Delegate {
  V8Runtime &v8rt;
  IHostObjectCodec &codec;

  // Set by `serialize` after constructing the v8::ValueSerializer so that
  // WriteHostObject can call WriteUint32 / WriteRawBytes on it. Cleared
  // before the delegate goes out of scope.
  v8::ValueSerializer *serializer = nullptr;

  // First DataCloneError message captured. V8's WriteValue returns
  // Nothing<bool>() after the delegate throws; we keep the message so the
  // top-level call can raise a jsi::JSError that carries it.
  std::optional<std::string> errorMessage;

  SerializerDelegate(V8Runtime &r, IHostObjectCodec &c) : v8rt(r), codec(c) {}

  void recordError(std::string msg, bool isDataCloneError) {
    if (errorMessage) return; // keep the first one
    if (isDataCloneError && msg.rfind(kDataCloneErrorPrefix, 0) != 0) {
      msg.insert(0, kDataCloneErrorPrefix);
    }
    errorMessage = std::move(msg);
  }

  // --- v8::ValueSerializer::Delegate overrides -----------------------------

  void ThrowDataCloneError(v8::Local<v8::String> message) override {
    v8::Isolate *iso = v8rt.GetIsolate();
    recordError(ToStdString(iso, message), /*isDataCloneError=*/true);
    iso->ThrowException(v8::Exception::Error(message));
  }

  bool HasCustomHostObject(v8::Isolate * /*iso*/) override { return true; }

  v8::Maybe<bool> IsHostObject(v8::Isolate *iso, v8::Local<v8::Object> obj) override {
    bool yes = false;
    try {
      // createValue is V8Runtime's jsi::Value factory; we are a friend.
      Value asValue = v8rt.createValue(obj.As<v8::Value>());
      Object jsiObj = std::move(asValue).getObject(static_cast<Runtime &>(v8rt));
      yes = codec.isHostObject(static_cast<Runtime &>(v8rt), jsiObj);
    } catch (const JSError &e) {
      recordError(std::string("isHostObject threw: ") + e.getMessage(), /*dce=*/false);
      iso->ThrowException(v8rt.valueReference(e.value()));
      return v8::Nothing<bool>();
    } catch (const std::exception &e) {
      recordError(std::string("isHostObject threw: ") + e.what(), /*dce=*/false);
      iso->ThrowException(v8::Exception::Error(
          v8::String::NewFromUtf8(iso, e.what()).ToLocalChecked()));
      return v8::Nothing<bool>();
    } catch (...) {
      recordError("isHostObject threw an unknown exception", /*dce=*/false);
      iso->ThrowException(v8::Exception::Error(
          v8::String::NewFromUtf8Literal(iso, "isHostObject threw")));
      return v8::Nothing<bool>();
    }
    return v8::Just(yes);
  }

  v8::Maybe<bool> WriteHostObject(v8::Isolate *iso, v8::Local<v8::Object> obj) override {
    std::vector<uint8_t> bytes;
    try {
      Value asValue = v8rt.createValue(obj.As<v8::Value>());
      Object jsiObj = std::move(asValue).getObject(static_cast<Runtime &>(v8rt));
      bytes = codec.writeHostObject(static_cast<Runtime &>(v8rt), jsiObj);
    } catch (const JSError &e) {
      // A JSError from the codec is the canonical signal "this object cannot
      // be cloned". Forward the message through the DataCloneError channel
      // so the host sees a single uniform error surface.
      recordError(e.getMessage(), /*isDataCloneError=*/true);
      iso->ThrowException(v8rt.valueReference(e.value()));
      return v8::Nothing<bool>();
    } catch (const std::exception &e) {
      recordError(e.what(), /*isDataCloneError=*/true);
      iso->ThrowException(v8::Exception::Error(
          v8::String::NewFromUtf8(iso, e.what()).ToLocalChecked()));
      return v8::Nothing<bool>();
    } catch (...) {
      recordError("writeHostObject threw an unknown exception", /*dce=*/true);
      iso->ThrowException(v8::Exception::Error(
          v8::String::NewFromUtf8Literal(iso, "writeHostObject threw")));
      return v8::Nothing<bool>();
    }

    // Frame the payload with a length prefix so the deserializer can hand the
    // exact same byte span back to readHostObject without the codec needing
    // to encode its own length.
    serializer->WriteUint32(static_cast<uint32_t>(bytes.size()));
    if (!bytes.empty()) {
      serializer->WriteRawBytes(bytes.data(), bytes.size());
    }
    return v8::Just(true);
  }

  v8::Maybe<uint32_t> GetSharedArrayBufferId(
      v8::Isolate *iso, v8::Local<v8::SharedArrayBuffer> /*sab*/) override {
    recordError("SharedArrayBuffer is not supported", /*isDataCloneError=*/true);
    iso->ThrowException(v8::Exception::Error(
        v8::String::NewFromUtf8Literal(iso, "SharedArrayBuffer is not supported")));
    return v8::Nothing<uint32_t>();
  }

  // Default ReallocateBufferMemory / FreeBufferMemory (realloc / free)
  // are fine; we copy the bytes out before destruction in any case.
};

// ---------------------------------------------------------------------------
// DeserializerDelegate
// ---------------------------------------------------------------------------

struct DeserializerDelegate final : v8::ValueDeserializer::Delegate {
  V8Runtime &v8rt;
  IHostObjectCodec &codec;

  v8::ValueDeserializer *deserializer = nullptr;
  std::optional<std::string> errorMessage;

  DeserializerDelegate(V8Runtime &r, IHostObjectCodec &c) : v8rt(r), codec(c) {}

  void recordError(std::string msg) {
    if (!errorMessage) errorMessage = std::move(msg);
  }

  v8::MaybeLocal<v8::Object> ReadHostObject(v8::Isolate *iso) override {
    uint32_t len = 0;
    if (!deserializer->ReadUint32(&len)) {
      recordError("readHostObject: missing length prefix");
      return {};
    }
    const void *raw = nullptr;
    if (len > 0 && !deserializer->ReadRawBytes(len, &raw)) {
      recordError("readHostObject: truncated payload");
      return {};
    }

    Value v;
    try {
      v = codec.readHostObject(
          static_cast<Runtime &>(v8rt),
          static_cast<const uint8_t *>(raw),
          static_cast<size_t>(len));
    } catch (const JSError &e) {
      recordError(e.getMessage());
      iso->ThrowException(v8rt.valueReference(e.value()));
      return {};
    } catch (const std::exception &e) {
      recordError(e.what());
      iso->ThrowException(v8::Exception::Error(
          v8::String::NewFromUtf8(iso, e.what()).ToLocalChecked()));
      return {};
    } catch (...) {
      recordError("readHostObject threw an unknown exception");
      iso->ThrowException(v8::Exception::Error(
          v8::String::NewFromUtf8Literal(iso, "readHostObject threw")));
      return {};
    }

    if (!v.isObject()) {
      recordError("readHostObject did not return an Object");
      iso->ThrowException(v8::Exception::Error(
          v8::String::NewFromUtf8Literal(iso, "readHostObject did not return an Object")));
      return {};
    }

    Object obj = v.getObject(static_cast<Runtime &>(v8rt));
    return v8::MaybeLocal<v8::Object>(v8rt.objectRef(obj));
  }

  v8::MaybeLocal<v8::SharedArrayBuffer> GetSharedArrayBufferFromId(
      v8::Isolate * /*iso*/, uint32_t /*id*/) override {
    // We refused to encode SharedArrayBuffer on the write side; deserialize
    // cannot legitimately ask for one.
    return {};
  }
};

} // namespace detail

// ---------------------------------------------------------------------------
// V8Runtime::serialize
// ---------------------------------------------------------------------------

std::vector<uint8_t> V8Runtime::serialize(
    Runtime &rt,
    const Value &value,
    const std::vector<Value> &transfer,
    IHostObjectCodec &codec) {
  // `rt` is the same Runtime instance whose vtable we dispatched through, so
  // a static_cast is sound; we still assert in debug to catch foreign-runtime
  // misuse early.
  assert(&rt == static_cast<Runtime *>(this));
  (void)rt;

  IsolateLocker locker(this);
  v8::Isolate *iso = GetIsolate();
  v8::Local<v8::Context> ctx = GetContextLocal();

  detail::SerializerDelegate del(*this, codec);
  v8::ValueSerializer serializer(iso, &del);
  del.serializer = &serializer;

  serializer.WriteHeader();

  // Step 1: register every transfer entry, validating it. We keep the
  // ArrayBuffer handles around so we can detach them once serialization
  // succeeds. Failure here throws straight through: nothing has been
  // mutated yet.
  std::vector<v8::Local<v8::ArrayBuffer>> transferBuffers;
  transferBuffers.reserve(transfer.size());
  uint32_t transferId = 0;
  for (const Value &entry : transfer) {
    if (!entry.isObject()) {
      throw JSError(*this, std::string(detail::kDataCloneErrorPrefix) +
          "transfer list entry is not an Object");
    }
    Object entryObj = entry.getObject(*this);
    v8::Local<v8::Object> entryV8 = objectRef(entryObj);
    if (!entryV8->IsArrayBuffer()) {
      throw JSError(*this, std::string(detail::kDataCloneErrorPrefix) +
          "transfer list entry is not an ArrayBuffer");
    }
    auto ab = entryV8.As<v8::ArrayBuffer>();
    if (ab->WasDetached()) {
      throw JSError(*this, std::string(detail::kDataCloneErrorPrefix) +
          "transfer list contains a detached ArrayBuffer");
    }
    if (!ab->IsDetachable()) {
      throw JSError(*this, std::string(detail::kDataCloneErrorPrefix) +
          "transfer list contains a non-detachable ArrayBuffer");
    }
    serializer.TransferArrayBuffer(transferId++, ab);
    transferBuffers.push_back(ab);
  }

  // Step 2: write the value graph. Any DataCloneError raised by the delegate
  // (detached buffer, OOB view, unsupported type, host-object codec refusal)
  // surfaces via WriteValue returning Nothing<bool>(); del.errorMessage holds
  // the human-readable reason.
  {
    v8::TryCatch tryCatch(iso);
    v8::Maybe<bool> ok = serializer.WriteValue(ctx, valueReference(value));
    if (ok.IsNothing()) {
      std::string msg = del.errorMessage.value_or(std::string{});
      if (msg.empty() && tryCatch.HasCaught()) {
        msg = detail::ToStdString(iso, tryCatch.Exception());
      }
      if (msg.empty()) {
        msg = std::string(detail::kDataCloneErrorPrefix) +
            "structured clone failed";
      }
      del.serializer = nullptr;
      throw JSError(*this, msg);
    }
  }

  // Step 3: serialization succeeded; detach the source ArrayBuffers. We use
  // the keyed Detach overload (the only one not deprecated) with a null key,
  // which matches any buffer that has not opted into ArrayBufferDetachKey.
  for (auto &ab : transferBuffers) {
    v8::Maybe<bool> r = ab->Detach(v8::Local<v8::Value>());
    if (r.IsNothing() || !r.FromMaybe(false)) {
      // Should be unreachable after the IsDetachable check above; if V8 ever
      // disagrees, fail loudly rather than leaving a half-transferred state.
      del.serializer = nullptr;
      throw JSError(*this, std::string(detail::kDataCloneErrorPrefix) +
          "ArrayBuffer refused detach after successful serialization");
    }
  }

  // Step 4: pull bytes out of the serializer. Release() transfers ownership
  // of a buffer allocated via the delegate's ReallocateBufferMemory (which
  // we left at the default `realloc`), so we must free() it.
  std::pair<uint8_t *, size_t> released = serializer.Release();
  del.serializer = nullptr;

  std::vector<uint8_t> out;
  if (released.first != nullptr) {
    out.assign(released.first, released.first + released.second);
    std::free(released.first);
  }
  return out;
}

// ---------------------------------------------------------------------------
// V8Runtime::deserialize
// ---------------------------------------------------------------------------

Value V8Runtime::deserialize(
    Runtime &rt,
    const uint8_t *data,
    size_t len,
    IHostObjectCodec &codec) {
  assert(&rt == static_cast<Runtime *>(this));
  (void)rt;

  IsolateLocker locker(this);
  v8::Isolate *iso = GetIsolate();
  v8::Local<v8::Context> ctx = GetContextLocal();

  detail::DeserializerDelegate del(*this, codec);
  v8::ValueDeserializer deserializer(iso, data, len, &del);
  del.deserializer = &deserializer;

  v8::TryCatch tryCatch(iso);

  if (deserializer.ReadHeader(ctx).IsNothing()) {
    std::string msg = del.errorMessage.value_or(std::string{});
    if (msg.empty() && tryCatch.HasCaught()) {
      msg = detail::ToStdString(iso, tryCatch.Exception());
    }
    if (msg.empty()) msg = "deserialize: invalid or unsupported header";
    del.deserializer = nullptr;
    throw JSError(*this, msg);
  }

  v8::Local<v8::Value> v;
  if (!deserializer.ReadValue(ctx).ToLocal(&v)) {
    std::string msg = del.errorMessage.value_or(std::string{});
    if (msg.empty() && tryCatch.HasCaught()) {
      msg = detail::ToStdString(iso, tryCatch.Exception());
    }
    if (msg.empty()) msg = "deserialize: failed to read value";
    del.deserializer = nullptr;
    throw JSError(*this, msg);
  }

  del.deserializer = nullptr;
  return createValue(v);
}

} // namespace v8runtime
