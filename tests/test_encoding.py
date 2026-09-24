import base64
import hashlib
import json
import urllib.parse
import zlib

from edupagetasks.encoding import (
    chromium_b64decode,
    chromium_b64encode,
    decode_request_body,
    eqap_decode,
    eqap_encode,
    raw_deflate,
)


def test_raw_deflate_roundtrip_and_bare_stream():
    data = b'{"username":"student","password":"secret"}'
    out = raw_deflate(data)
    assert zlib.decompress(out, -15) == data
    assert not out.startswith(b"\x78")


def test_chromium_b64encode_matches_stdlib():
    data = b"Fr. equations"
    assert chromium_b64encode(data) == base64.b64encode(data).decode("ascii")


def test_chromium_b64decode_whitespace_and_padding_tolerance():
    assert chromium_b64decode("aGVs\nbG8=\t") == b"hello"
    assert chromium_b64decode("aGVsbG8") == b"hello"


def test_eqap_encode_structure():
    body = decode_request_body(
        {"rpcparams": json.dumps({"username": "u", "edupage": ""})}
    )
    enc = eqap_encode(body)
    assert enc["eqaz"] == "1"
    assert enc["eqap"].startswith("dz:")
    assert enc["eqacs"] == hashlib.sha1(enc["eqap"].encode("ascii")).hexdigest()


def test_eqap_roundtrip_recovers_original_body():
    payload = {"rpcparams": json.dumps({"username": "u", "edupage": "", "tu": None})}
    body = decode_request_body(payload)
    enc = eqap_encode(body)
    compressed = chromium_b64decode(enc["eqap"][len("dz:") :])
    recovered = zlib.decompress(compressed, -15).decode("utf-8")
    assert recovered == body


def test_decode_request_body_urlencodes_rpcparams():
    body = decode_request_body({"rpcparams": '{"a": "1"}'})
    assert body == urllib.parse.urlencode({"rpcparams": '{"a": "1"}'})


def test_eqap_decode_prefixes():
    assert eqap_decode("eqz:" + chromium_b64encode(b"payload")) == b"payload"
    assert eqap_decode("eqwd:" + chromium_b64encode(b"error")) == b"error"


def test_eqap_decode_plain_text_passthrough():
    raw = "plain text \u00e9"
    assert eqap_decode(raw) == raw.encode("latin-1")
