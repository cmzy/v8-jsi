// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
// This code is based on the old node inspector implementation. See LICENSE_NODE for Node.js' project license details
#include "inspector_utils.h"

// MultiByteToWideChar lives in <windows.h>; V8Windows.h is a no-op elsewhere.
#include "V8Windows.h"

#include <limits>
#include <stdexcept>

namespace inspector {
namespace utils {

// These are copied from react-native code.
const uint16_t kUtf8OneByteBoundary = 0x80;
const uint16_t kUtf8TwoBytesBoundary = 0x800;
const uint16_t kUtf16HighSubLowBoundary = 0xD800;
const uint16_t kUtf16HighSubHighBoundary = 0xDC00;
const uint16_t kUtf16LowSubHighBoundary = 0xE000;

size_t utf16toUTF8Length(const uint16_t* utf16String, size_t utf16StringLen) {
  if (!utf16String || utf16StringLen == 0) {
    return 0;
  }

  uint32_t utf8StringLen = 0;
  auto utf16StringEnd = utf16String + utf16StringLen;
  auto idx16 = utf16String;
  while (idx16 < utf16StringEnd) {
    auto ch = *idx16++;
    if (ch < kUtf8OneByteBoundary) {
      utf8StringLen++;
    }
    else if (ch < kUtf8TwoBytesBoundary) {
      utf8StringLen += 2;
    }
    else if (
      (ch >= kUtf16HighSubLowBoundary) && (ch < kUtf16HighSubHighBoundary) &&
      (idx16 < utf16StringEnd) &&
      (*idx16 >= kUtf16HighSubHighBoundary) && (*idx16 < kUtf16LowSubHighBoundary)) {
      utf8StringLen += 4;
      idx16++;
    }
    else {
      utf8StringLen += 3;
    }
  }

  return utf8StringLen;
}

std::string utf16toUTF8(const uint16_t* utf16String, size_t utf16StringLen) noexcept {
  if (!utf16String || utf16StringLen <= 0) {
    return "";
  }

  std::string utf8String(utf16toUTF8Length(utf16String, utf16StringLen), '\0');
  auto idx8 = utf8String.begin();
  auto idx16 = utf16String;
  auto utf16StringEnd = utf16String + utf16StringLen;
  while (idx16 < utf16StringEnd) {
    auto ch = *idx16++;
    if (ch < kUtf8OneByteBoundary) {
      *idx8++ = (ch & 0x7F);
    }
    else if (ch < kUtf8TwoBytesBoundary) {
#ifdef _MSC_VER
#pragma warning(suppress: 4244)
      *idx8++ = 0b11000000 | (ch >> 6);
#else
      *idx8++ = 0b11000000 | (ch >> 6);
#endif
      *idx8++ = 0b10000000 | (ch & 0x3F);
    }
    else if (
      (ch >= kUtf16HighSubLowBoundary) && (ch < kUtf16HighSubHighBoundary) &&
      (idx16 < utf16StringEnd) &&
      (*idx16 >= kUtf16HighSubHighBoundary) && (*idx16 < kUtf16LowSubHighBoundary)) {
      auto ch2 = *idx16++;
      uint8_t trunc_byte = (((ch >> 6) & 0x0F) + 1);
      *idx8++ = 0b11110000 | (trunc_byte >> 2);
      *idx8++ = 0b10000000 | ((trunc_byte & 0x03) << 4) | ((ch >> 2) & 0x0F);
      *idx8++ = 0b10000000 | ((ch & 0x03) << 4) | ((ch2 >> 6) & 0x0F);
      *idx8++ = 0b10000000 | (ch2 & 0x3F);
    }
    else {
      *idx8++ = 0b11100000 | (ch >> 12);
      *idx8++ = 0b10000000 | ((ch >> 6) & 0x3F);
      *idx8++ = 0b10000000 | (ch & 0x3F);
    }
  }

  return utf8String;
}

std::u16string Utf8ToUtf16(const char* utf8, size_t utf8Len)
{
  std::u16string utf16{};

  if (utf8Len == 0)
  {
    return utf16;
  }

#ifdef _WIN32
  // Windows path: defer to the platform converter and copy through wchar_t,
  // which is the same width as char16_t on this OS.

  // Extra parentheses needed here to prevent expanding max as a
  // Windows-specific preprocessor macro.
  if (utf8Len > static_cast<size_t>((std::numeric_limits<int>::max)()))
  {
    throw std::overflow_error("Input string too long: size_t-length doesn't fit into int.");
  }

  const int utf8Length = static_cast<int>(utf8Len);

  // Fail if an invalid UTF-8 character is encountered in the input string.
  constexpr DWORD flags = MB_ERR_INVALID_CHARS;

  const int utf16Length = ::MultiByteToWideChar(
    CP_UTF8, flags, utf8, utf8Length, nullptr, 0);

  if (utf16Length == 0)
  {
    throw std::runtime_error("Cannot get result string length when converting from UTF-8 to UTF-16 (MultiByteToWideChar failed).");
  }

  utf16.resize(utf16Length);

  int result = ::MultiByteToWideChar(
    CP_UTF8, flags, utf8, utf8Length,
    reinterpret_cast<wchar_t*>(&utf16[0]),
    utf16Length);

  if (result == 0)
  {
    throw std::runtime_error("Cannot convert from UTF-8 to UTF-16 (MultiByteToWideChar failed).");
  }

  return utf16;
#else
  // POSIX path: hand-rolled UTF-8 decoder. wchar_t is 32-bit on POSIX, so we
  // can't use MultiByteToWideChar's signature even with iconv; the simplest
  // and dependency-free option is to decode the four UTF-8 forms directly and
  // emit surrogate pairs for code points >= U+10000. Mirrors the validation
  // semantics of MB_ERR_INVALID_CHARS: malformed input throws.
  utf16.reserve(utf8Len);
  const unsigned char* p = reinterpret_cast<const unsigned char*>(utf8);
  const unsigned char* end = p + utf8Len;

  auto cont = [&](const unsigned char*& q) -> uint32_t {
    if (q >= end || (*q & 0xC0) != 0x80) {
      throw std::runtime_error("Invalid UTF-8 continuation byte while converting to UTF-16.");
    }
    return static_cast<uint32_t>(*q++ & 0x3F);
  };

  while (p < end) {
    uint32_t cp;
    unsigned char b = *p++;
    if (b < 0x80) {
      cp = b;
    } else if ((b & 0xE0) == 0xC0) {
      cp = (static_cast<uint32_t>(b & 0x1F) << 6) | cont(p);
      if (cp < 0x80) throw std::runtime_error("Overlong UTF-8 sequence.");
    } else if ((b & 0xF0) == 0xE0) {
      uint32_t c1 = cont(p), c2 = cont(p);
      cp = (static_cast<uint32_t>(b & 0x0F) << 12) | (c1 << 6) | c2;
      if (cp < 0x800) throw std::runtime_error("Overlong UTF-8 sequence.");
      if (cp >= 0xD800 && cp <= 0xDFFF) throw std::runtime_error("UTF-8 encodes a surrogate.");
    } else if ((b & 0xF8) == 0xF0) {
      uint32_t c1 = cont(p), c2 = cont(p), c3 = cont(p);
      cp = (static_cast<uint32_t>(b & 0x07) << 18) | (c1 << 12) | (c2 << 6) | c3;
      if (cp < 0x10000 || cp > 0x10FFFF) throw std::runtime_error("UTF-8 out of Unicode range.");
    } else {
      throw std::runtime_error("Invalid UTF-8 leading byte.");
    }

    if (cp < 0x10000) {
      utf16.push_back(static_cast<char16_t>(cp));
    } else {
      cp -= 0x10000;
      utf16.push_back(static_cast<char16_t>(0xD800 | (cp >> 10)));
      utf16.push_back(static_cast<char16_t>(0xDC00 | (cp & 0x3FF)));
    }
  }

  return utf16;
#endif
}

char ToLower(char c) {
  return c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c;
}

std::string ToLower(const std::string& in) {
  std::string out(in.size(), 0);
  for (size_t i = 0; i < in.size(); ++i)
    out[i] = ToLower(in[i]);
  return out;
}

bool StringEqualNoCase(const char* a, const char* b) {
  do {
    if (*a == '\0')
      return *b == '\0';
    if (*b == '\0')
      return *a == '\0';
  } while (ToLower(*a++) == ToLower(*b++));
  return false;
}

bool StringEqualNoCaseN(const char* a, const char* b, size_t length) {
  for (size_t i = 0; i < length; i++) {
    if (ToLower(a[i]) != ToLower(b[i]))
      return false;
    if (a[i] == '\0')
      return true;
  }
  return true;
}

size_t base64_encode(const char* src, size_t slen, char* dst, size_t dlen) {
  // We know how much we'll write, just make sure that there's space.
  // CHECK(dlen >= base64_encoded_size(slen) && "not enough space provided for base64 encode");

  dlen = base64_encoded_size(slen);

  unsigned a;
  unsigned b;
  unsigned c;
  unsigned i;
  unsigned k;
  unsigned n;

  static const char table[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789+/";

  i = 0;
  k = 0;
  n = static_cast<int>(slen) / 3 * 3;

  while (i < n) {
    a = src[i + 0] & 0xff;
    b = src[i + 1] & 0xff;
    c = src[i + 2] & 0xff;

    dst[k + 0] = table[a >> 2];
    dst[k + 1] = table[((a & 3) << 4) | (b >> 4)];
    dst[k + 2] = table[((b & 0x0f) << 2) | (c >> 6)];
    dst[k + 3] = table[c & 0x3f];

    i += 3;
    k += 4;
  }

  if (n != slen) {
    switch (slen - n) {
    case 1:
      a = src[i + 0] & 0xff;
      dst[k + 0] = table[a >> 2];
      dst[k + 1] = table[(a & 3) << 4];
      dst[k + 2] = '=';
      dst[k + 3] = '=';
      break;

    case 2:
      a = src[i + 0] & 0xff;
      b = src[i + 1] & 0xff;
      dst[k + 0] = table[a >> 2];
      dst[k + 1] = table[((a & 3) << 4) | (b >> 4)];
      dst[k + 2] = table[(b & 0x0f) << 2];
      dst[k + 3] = '=';
      break;
    }
  }

  return dlen;
}

}
}