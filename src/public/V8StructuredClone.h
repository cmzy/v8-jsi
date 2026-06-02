// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
#pragma once

#include "V8JsiRuntime.h" // for V8JSI_EXPORT

#include <jsi/jsi.h>

#include <cstddef>
#include <cstdint>
#include <vector>

namespace v8runtime {

// Embedder-supplied codec for "host" objects that the structured-clone wire
// format does not understand natively (Blob / ImageData / MessagePort / ...).
//
// V8's ValueSerializer asks `isHostObject` for every JS object it visits;
// when the answer is true, it routes the object to `writeHostObject`, whose
// raw bytes are embedded verbatim into the wire stream (v8-jsi prepends a
// length prefix). On the read side, `readHostObject` receives the same byte
// span and reconstructs the JS object.
//
// The codec is responsible for its own type-tag scheme inside the bytes it
// writes; v8-jsi treats the payload as opaque.
//
// Errors:
//   * Throwing a `jsi::JSError` or `std::exception` from any callback causes
//     the enclosing serialize/deserialize to fail; the message bubbles up as
//     the message of the JSError thrown from the top-level call. If the
//     callback's intent is to signal "this object is not cloneable", throw
//     with a message starting with "DataCloneError: " so the host can route
//     it accordingly.
class V8JSI_EXPORT IHostObjectCodec {
 public:
  virtual ~IHostObjectCodec() = default;

  // Return true when `obj` should be routed through `writeHostObject` rather
  // than V8's built-in serialization. Default: never.
  virtual bool isHostObject(
      facebook::jsi::Runtime& rt,
      const facebook::jsi::Object& obj) {
    (void)rt;
    (void)obj;
    return false;
  }

  // Encode a host object to an opaque byte string. Called only for objects
  // that `isHostObject` accepted. Must not return empty unless the codec
  // can also decode an empty payload back into a meaningful object.
  virtual std::vector<uint8_t> writeHostObject(
      facebook::jsi::Runtime& rt,
      const facebook::jsi::Object& obj) = 0;

  // Decode a host object from `[data, data + len)`. The byte span is owned
  // by v8-jsi and stays valid for the duration of this call only.
  virtual facebook::jsi::Value readHostObject(
      facebook::jsi::Runtime& rt,
      const uint8_t* data,
      size_t len) = 0;
};

// JSI interface exposing V8's native HTML structured-clone backend.
//
// Discovery:
//
//   if (auto* sc = facebook::jsi::castInterface<v8runtime::IStructuredClone>(&rt)) {
//     auto bytes = sc->serialize(rt, value, transfer, codec);
//   } else {
//     // not a v8-jsi runtime; use a portable fallback
//   }
//
// Lifetime: the returned pointer is owned by the Runtime; do not delete it.
//
// Threading: every method must be called on the Runtime's JS thread, same
// as `evaluateJavaScript`.
class V8JSI_EXPORT IStructuredClone : public facebook::jsi::ICast {
 public:
  // UUID v4, randomly generated, fixed forever for this interface.
  static constexpr facebook::jsi::UUID uuid{
      0x7a3f1c2e,
      0x9d5b,
      0x4e08,
      0xa1c7,
      0x4b6f8d2c5e91};

  // Serialize `value` (and any objects reachable from it) into a self-
  // contained byte stream using V8's structured-clone wire format.
  //
  // `transfer` is the structured-clone "transfer list". Every entry must be
  // an `ArrayBuffer` that is currently attached (not detached) and that this
  // runtime owns; on success the source buffers are detached
  // (their `byteLength` becomes 0). If serialization fails the source
  // buffers stay untouched.
  //
  // `codec` handles host-only object types; pass a no-op subclass when none
  // are needed.
  //
  // Errors are reported by throwing `jsi::JSError`. Messages produced by V8's
  // own structured-clone checks (detached buffer, out-of-bounds view,
  // unsupported type, ...) are prefixed with "DataCloneError: " so the host
  // can map them to the platform's `DOMException("...", "DataCloneError")`.
  virtual std::vector<uint8_t> serialize(
      facebook::jsi::Runtime& rt,
      const facebook::jsi::Value& value,
      const std::vector<facebook::jsi::Value>& transfer,
      IHostObjectCodec& codec) = 0;

  // Inverse of `serialize`. `data`/`len` must come from a previous
  // `serialize` call on a runtime of the same V8 major version; the wire
  // format is not stable across V8 upgrades and is unsuitable for
  // long-term storage.
  virtual facebook::jsi::Value deserialize(
      facebook::jsi::Runtime& rt,
      const uint8_t* data,
      size_t len,
      IHostObjectCodec& codec) = 0;

 protected:
  ~IStructuredClone() = default;
};

} // namespace v8runtime
