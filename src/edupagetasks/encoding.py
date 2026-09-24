"""EduPage wire encodings: eqap request wrapper and prefix-based response decoding.

The login RPC (and send_message) request bodies are built in two steps:
``decode_request_body`` urlencodes the JSON-ish request dict, then
``eqap_encode`` wraps that string into the eqap/eqacs/eqaz form fields where
``eqap = "dz:" + chromium-b64(raw-deflate(urlencoded body))``.  Responses come
back base64-only and are decoded by prefix (``eqz:`` / ``eqwd:``) or used as
plain bytes; JSON parsing is the caller's job.
"""

from __future__ import annotations

import base64
import hashlib
import urllib.parse
import zlib
from typing import Any

_CHROME_FOLDING = str.maketrans("", "", "\t\n\f\r")


def chromium_b64encode(data: bytes) -> str:
    """RFC 4648 base64 matching EduPage's (btoa-compatible) encoder."""
    return base64.b64encode(data).decode("ascii")


def chromium_b64decode(data: str) -> bytes:
    """Lenient decoder: strips line-folding whitespace and restores padding."""
    clean = data.translate(_CHROME_FOLDING)
    padding = (-len(clean)) % 4
    if padding:
        clean += "=" * padding
    return base64.b64decode(clean, validate=True)


def raw_deflate(data: bytes) -> bytes:
    """Raw DEFLATE stream (no zlib header), as EduPage expects on request bodies."""
    compressor = zlib.compressobj(-1, zlib.DEFLATED, -15, 8, zlib.Z_DEFAULT_STRATEGY)
    return compressor.compress(data) + compressor.flush()


def decode_request_body(body: dict[str, Any]) -> str:
    """urlencode a request dict -- the ``RequestData.encode_request_body`` step."""
    return urllib.parse.urlencode(body)


def eqap_encode(request_body: str) -> dict[str, str]:
    """Wrap an already-urlencoded request body into the eqap/eqacs/eqaz fields."""
    eqap = "dz:" + chromium_b64encode(raw_deflate(request_body.encode("utf-8")))
    return {
        "eqap": eqap,
        "eqacs": hashlib.sha1(eqap.encode("ascii")).hexdigest(),
        "eqaz": "1",
    }


def eqap_decode(data: str) -> bytes:
    """Decode an eqz:/eqwd:-prefixed base64 payload, or plain text, to bytes."""
    text = data.lstrip()
    if text.startswith("eqz:"):
        return chromium_b64decode(text[4:])
    if text.startswith("eqwd:"):
        return chromium_b64decode(text[5:])
    return text.encode("latin-1")
