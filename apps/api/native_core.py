"""Python ctypes bridge to the C++20 native security & verification core.

Provides memory-safe, zero-dependency UTF-8 Spanish normalization, literal
citation span lookup, prompt-marker/XXE/zip-entry guards, and 8-gram checks,
with automatic pure-Python fallback if the compiled library is absent.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
import re
import sys
import unicodedata

_NATIVE_DIR = Path(__file__).resolve().parents[1] / "native"
if sys.platform == "win32":
    _LIB_NAME = "eduandes_core.dll"
elif sys.platform == "darwin":
    _LIB_NAME = "eduandes_core.dylib"
else:
    _LIB_NAME = "eduandes_core.so"

_LIB_PATH = _NATIVE_DIR / _LIB_NAME
_RESERVED_TEXT = ("<|im_start|>", "<|im_end|>", "<|endoftext|>", "<think>", "</think>")


def _load_library():
    if not _LIB_PATH.is_file():
        return None
    try:
        lib = ctypes.CDLL(str(_LIB_PATH))
        lib.ea_version.argtypes = []
        lib.ea_version.restype = ctypes.c_char_p

        lib.ea_ct_equal.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
        lib.ea_ct_equal.restype = ctypes.c_int

        lib.ea_free.argtypes = [ctypes.c_void_p]
        lib.ea_free.restype = None

        lib.ea_scan_safety.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        lib.ea_scan_safety.restype = ctypes.c_int

        lib.ea_check_xml_safety.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        lib.ea_check_xml_safety.restype = ctypes.c_int

        lib.ea_is_safe_zip_entry.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        lib.ea_is_safe_zip_entry.restype = ctypes.c_int

        lib.ea_normalize_utf8.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        lib.ea_normalize_utf8.restype = ctypes.c_void_p

        lib.ea_find_literal_span.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.ea_find_literal_span.restype = ctypes.c_int

        lib.ea_is_distinct_8gram.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.ea_is_distinct_8gram.restype = ctypes.c_int
        return lib
    except OSError:
        return None


_LIB = _load_library()


def native_status() -> dict:
    """Return telemetry about the active C++20 native module."""
    if _LIB is None:
        return {
            "active": False,
            "backend": "python-fallback",
            "version": None,
            "library": _LIB_NAME,
            "size_bytes": 0,
        }
    version = _LIB.ea_version()
    return {
        "active": True,
        "backend": "cpp20-native",
        "version": version.decode("utf-8") if version else "unknown",
        "library": _LIB_NAME,
        "size_bytes": _LIB_PATH.stat().st_size,
    }


def has_reserved_markers(text: str) -> bool:
    """Return True if reserved chat markers or NUL bytes appear in text."""
    if _LIB is not None:
        raw = text.encode("utf-8", errors="replace")
        res = _LIB.ea_scan_safety(raw, len(raw))
        if res == 0:
            return True
        if res == 1:
            return False
    return "\x00" in text or any(marker in text for marker in _RESERVED_TEXT)


def is_xml_safe(data: bytes) -> bool:
    """Return True if XML payload contains no DOCTYPE/ENTITY injection and respects size cap."""
    if _LIB is not None:
        res = _LIB.ea_check_xml_safety(data, len(data))
        if res in (0, 1):
            return res == 1
    upper = data.upper()
    return len(data) <= 10 * 1024 * 1024 and b"\x00" not in data and b"<!DOCTYPE" not in upper and b"<!ENTITY" not in upper


def is_safe_zip_entry(path: str) -> bool:
    """Return True if a PPTX zip entry path has no traversal, absolute path, or VBA macros."""
    if _LIB is not None:
        raw = path.encode("utf-8", errors="replace")
        return _LIB.ea_is_safe_zip_entry(raw, len(raw)) == 1
    norm = path.replace("\\", "/")
    if not path or path.startswith(("/", "\\")) or "vbaproject" in path.lower():
        return False
    if len(path) >= 2 and path[0].isalpha() and path[1] == ":":
        return False
    return ".." not in norm.split("/")


def normalized(value: str) -> str:
    """Normalize Spanish text (accent folding, lowercase, punctuation stripped, collapsed spaces)."""
    if _LIB is not None:
        raw = value.encode("utf-8", errors="replace")
        ptr = _LIB.ea_normalize_utf8(raw, len(raw))
        if ptr:
            try:
                return ctypes.string_at(ptr).decode("utf-8", errors="replace")
            finally:
                _LIB.ea_free(ptr)
    folded = "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", folded).split())


def find_literal_span(source: str, needle: str) -> tuple[int, int, str] | None:
    """Locate needle in source verbatim or via Spanish-normalized word boundaries."""
    words = needle.split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(word) for word in words)
    found = re.search(pattern, source, flags=re.IGNORECASE)
    if found:
        return found.start(), found.end(), found.group(0)

    if _LIB is not None:
        src_bytes = source.encode("utf-8")
        ndl_bytes = needle.encode("utf-8")
        start_cp = ctypes.c_size_t(0)
        end_cp = ctypes.c_size_t(0)
        rc = _LIB.ea_find_literal_span(
            src_bytes, len(src_bytes), ndl_bytes, len(ndl_bytes),
            ctypes.byref(start_cp), ctypes.byref(end_cp),
        )
        if rc == 1:
            s, e = int(start_cp.value), int(end_cp.value)
            if 0 <= s < e <= len(source):
                return s, e, source[s:e].strip()
        elif rc == 0:
            return None

    # Pure-Python fallback
    norm_chars: list[str] = []
    orig_indices: list[int] = []
    prev_space = True
    for idx, ch in enumerate(source):
        for piece in unicodedata.normalize("NFKD", ch.casefold()):
            if unicodedata.combining(piece):
                continue
            if piece.isalnum() or piece == "_":
                norm_chars.append(piece)
                orig_indices.append(idx)
                prev_space = False
            elif not prev_space:
                norm_chars.append(" ")
                orig_indices.append(idx)
                prev_space = True
    norm_source = "".join(norm_chars)
    ndl_norm = normalized(needle)
    for candidate in (
        ndl_norm,
        re.sub(r"^(?:el|la|los|las|un|una|unos|unas)\s+", "", ndl_norm),
    ):
        if len(candidate) < 3:
            continue
        match = re.search(r"(?<!\w)" + re.escape(candidate) + r"(?!\w)", norm_source)
        if match:
            start_pos = orig_indices[match.start()]
            end_pos = orig_indices[match.end() - 1] + 1
            return start_pos, end_pos, source[start_pos:end_pos].strip()
    return None


def ct_equal(left: str | bytes, right: str | bytes) -> bool:
    """Constant-time equality check for HMAC tokens and digests."""
    lb = left.encode("utf-8") if isinstance(left, str) else left
    rb = right.encode("utf-8") if isinstance(right, str) else right
    if _LIB is not None:
        rc = _LIB.ea_ct_equal(lb, len(lb), rb, len(rb))
        if rc in (0, 1):
            return rc == 1
    import hmac
    return hmac.compare_digest(lb, rb)


def is_distinct_8gram(left: str, right: str) -> bool:
    """Return True if left and right are non-identical and share no 8-word sequence."""
    if _LIB is not None:
        lb = left.encode("utf-8", errors="replace")
        rb = right.encode("utf-8", errors="replace")
        rc = _LIB.ea_is_distinct_8gram(lb, len(lb), rb, len(rb))
        if rc in (0, 1):
            return rc == 1
    a, b = normalized(left).split(), normalized(right).split()
    sequences = {tuple(a[i:i + 8]) for i in range(len(a) - 7)}
    return a != b and not any(tuple(b[i:i + 8]) in sequences for i in range(len(b) - 7))
