"""Pure-Python protobuf wire-format codec driven by the extracted maimemo schema.

The schema (``schema.json``) was extracted from the minified JS bundles of the
maimemo web-study app (``tc-apis.maimemo.com/webstudy/app``).  Every message is
a plain ``dict``; fields that are absent/empty/zero are omitted when encoding,
and only fields present on the wire are set when decoding (proto3 semantics,
matching the ts-proto code the web app ships).

Supported scalar types: int32/int64/uint32/uint64, sint32/sint64, bool, enum,
float, double, string, bytes, nested messages, ``google.protobuf.Value`` and
``google.protobuf.Timestamp``.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

_SCHEMA: dict[str, Any] = json.loads(
    (Path(__file__).parent / "schema.json").read_text(encoding="utf-8")
)
TYPES: dict[str, list[dict[str, Any]]] = _SCHEMA["types"]
ENUMS: dict[str, dict[str, int]] = _SCHEMA["enums"]

# name -> field number -> field descriptor (for messages)
_FIELDS: dict[str, dict[int, dict[str, Any]]] = {}
for _name, _fields in TYPES.items():
    _FIELDS[_name] = {f["number"]: f for f in sorted(_fields, key=lambda f: f["number"])}

_INT_TYPES = {"int32", "int64", "uint32", "uint64"}
_FIXED_TYPES = {"fixed32", "fixed64", "sfixed32", "sfixed64", "float", "double"}
_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LEN = 2
_WIRE_32BIT = 5


class SchemaError(ValueError):
    """Raised for unknown types / fields or malformed wire data."""


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def _write_varint(buf: bytearray, value: int) -> None:
    value &= (1 << 64) - 1  # protobuf varints are 64-bit two's complement
    while value >= 0x80:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value)


def _encode_value_bytes(value: Any) -> bytes:
    """Encode a python value as a google.protobuf.Value message (wire bytes)."""
    buf = bytearray()
    if value is None:
        _write_varint(buf, (1 << 3) | _WIRE_VARINT)
        _write_varint(buf, 0)
    elif isinstance(value, bool):
        _write_varint(buf, (4 << 3) | _WIRE_VARINT)
        _write_varint(buf, 1 if value else 0)
    elif isinstance(value, (int, float)):
        _write_varint(buf, (2 << 3) | _WIRE_64BIT)
        buf.extend(struct.pack("<d", float(value)))
    elif isinstance(value, str):
        _encode_field(buf, 3, _WIRE_LEN, value.encode("utf-8"))
    elif isinstance(value, (list, tuple)):
        inner = bytearray()
        for item in value:
            _encode_field(inner, 1, _WIRE_LEN, _encode_value_bytes(item))
        _encode_field(buf, 6, _WIRE_LEN, bytes(inner))  # list_value
    elif isinstance(value, dict):
        inner = bytearray()
        for key, item in value.items():
            entry = bytearray()
            _encode_field(entry, 1, _WIRE_LEN, str(key).encode("utf-8"))
            _encode_field(entry, 2, _WIRE_LEN, _encode_value_bytes(item))
            _encode_field(inner, 1, _WIRE_LEN, bytes(entry))
        _encode_field(buf, 5, _WIRE_LEN, bytes(inner))  # struct_value
    else:
        raise SchemaError(f"cannot wrap {type(value).__name__} in google.protobuf.Value")
    return bytes(buf)


def _decode_list_value(data: bytes) -> list[Any]:
    """Decode a ListValue message: repeated Value on field 1."""
    out: list[Any] = []
    pos = 0
    while pos < len(data):
        raw, pos = _read_varint(data, pos)
        fnum, wire = raw >> 3, raw & 7
        if fnum == 1 and wire == _WIRE_LEN:
            ln, pos = _read_varint(data, pos)
            out.append(_decode_value_bytes(data[pos:pos + ln]))
            pos += ln
        else:
            pos = _skip(data, pos, wire)
    return out


def _decode_struct_value(data: bytes) -> dict[str, Any]:
    """Decode a Struct message: map<string, Value> as repeated entry messages."""
    out: dict[str, Any] = {}
    pos = 0
    while pos < len(data):
        raw, pos = _read_varint(data, pos)
        fnum, wire = raw >> 3, raw & 7
        if fnum == 1 and wire == _WIRE_LEN:
            ln, pos = _read_varint(data, pos)
            entry = data[pos:pos + ln]
            pos += ln
            key = ""
            value: Any = None
            sub = 0
            while sub < len(entry):
                eraw, sub = _read_varint(entry, sub)
                efnum, ewire = eraw >> 3, eraw & 7
                if efnum == 1 and ewire == _WIRE_LEN:
                    eln, sub = _read_varint(entry, sub)
                    key = entry[sub:sub + eln].decode("utf-8", "replace")
                    sub += eln
                elif efnum == 2 and ewire == _WIRE_LEN:
                    eln, sub = _read_varint(entry, sub)
                    value = _decode_value_bytes(entry[sub:sub + eln])
                    sub += eln
                else:
                    sub = _skip(entry, sub, ewire)
            out[key] = value
        else:
            pos = _skip(data, pos, wire)
    return out


def _decode_value_bytes(data: bytes) -> Any:
    """Decode a google.protobuf.Value message into a python value."""
    result: Any = None
    pos = 0
    while pos < len(data):
        raw, pos = _read_varint(data, pos)
        fnum, wire = raw >> 3, raw & 7
        if fnum == 1 and wire == _WIRE_VARINT:  # null_value
            _, pos = _read_varint(data, pos)
            result = None
        elif fnum == 2 and wire == _WIRE_64BIT:  # number_value
            result = struct.unpack("<d", data[pos:pos + 8])[0]
            pos += 8
        elif fnum == 3 and wire == _WIRE_LEN:  # string_value
            ln, pos = _read_varint(data, pos)
            result = data[pos:pos + ln].decode("utf-8", "replace")
            pos += ln
        elif fnum == 4 and wire == _WIRE_VARINT:  # bool_value
            rv, pos = _read_varint(data, pos)
            result = bool(rv)
        elif fnum == 5 and wire == _WIRE_LEN:  # struct_value
            ln, pos = _read_varint(data, pos)
            result = _decode_struct_value(data[pos:pos + ln])
            pos += ln
        elif fnum == 6 and wire == _WIRE_LEN:  # list_value
            ln, pos = _read_varint(data, pos)
            result = _decode_list_value(data[pos:pos + ln])
            pos += ln
        else:
            pos = _skip(data, pos, wire)
    return result


def _encode_field(buf: bytearray, number: int, wire: int, payload: bytes | None = None) -> None:
    _write_varint(buf, (number << 3) | wire)
    if wire == _WIRE_LEN:
        _write_varint(buf, len(payload or b""))
        if payload:
            buf.extend(payload)
    elif wire == _WIRE_64BIT or wire == _WIRE_32BIT:
        if payload:
            buf.extend(payload)


def _encode_message(type_name: str, data: dict[str, Any] | None, force_empty: set[int] | None = None) -> bytes:
    """Encode a message dict into protobuf wire bytes.

    ``force_empty`` lists field numbers that should be written even when their
    value is the default (used by the websocket frame to always include the
    ``data`` bytes field, matching the web app).
    """
    force_empty = force_empty or set()
    if type_name not in _FIELDS:
        raise SchemaError(f"unknown message type: {type_name}")
    if not data:
        return b""
    buf = bytearray()
    fields = _FIELDS[type_name]
    for fnum, desc in fields.items():
        name, ftype = desc["name"], desc["type"]
        if name not in data:
            continue
        value = data[name]
        if desc.get("repeated"):
            if not value:
                continue
            if ftype in _INT_TYPES or ftype == "bool" or ftype == "enum":
                for item in value:
                    _write_varint(buf, (fnum << 3) | _WIRE_VARINT)
                    _write_scalar_varint(buf, ftype, item)
            elif ftype in ("sint32", "sint64"):
                for item in value:
                    _write_varint(buf, (fnum << 3) | _WIRE_VARINT)
                    _write_varint(buf, _zigzag(item))
            elif ftype in _FIXED_TYPES:
                for item in value:
                    _write_varint(buf, (fnum << 3) | _wire_for(ftype))
                    buf.extend(_pack_fixed(ftype, item))
            elif ftype == "string":
                for item in value:
                    _write_field(buf, fnum, ftype, item)
            elif ftype == "bytes":
                for item in value:
                    _write_field(buf, fnum, ftype, item)
            elif ftype.startswith("google.protobuf."):
                for item in value:
                    _write_field(buf, fnum, ftype, item)
            else:  # repeated message
                for item in value:
                    payload = _encode_message(ftype, item)
                    if payload or item is not None:
                        _encode_field(buf, fnum, _WIRE_LEN, payload)
        else:
            if fnum in force_empty and ftype == "bytes" and not value:
                _encode_field(buf, fnum, _WIRE_LEN, b"")
                continue
            _write_field(buf, fnum, ftype, value, default=desc.get("default"))
    return bytes(buf)


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63) if value < 0 else (value << 1)


def _unzigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _write_scalar_varint(buf: bytearray, ftype: str, value: Any) -> None:
    if ftype == "bool":
        _write_varint(buf, 1 if value else 0)
    elif ftype == "enum":
        _write_varint(buf, int(value))
    elif ftype in ("int32", "int64"):
        _write_varint(buf, int(value))
    elif ftype in ("uint32", "uint64"):
        _write_varint(buf, int(value) & ((1 << 64) - 1))
    elif ftype in ("sint32", "sint64"):
        _write_varint(buf, _zigzag(int(value)))
    else:  # pragma: no cover
        raise SchemaError(f"not a varint type: {ftype}")


def _wire_for(ftype: str) -> int:
    if ftype in ("float", "fixed32", "sfixed32"):
        return _WIRE_32BIT
    if ftype in ("double", "fixed64", "sfixed64"):
        return _WIRE_64BIT
    raise SchemaError(f"not a fixed-width type: {ftype}")


def _pack_fixed(ftype: str, value: Any) -> bytes:
    if ftype == "float":
        return struct.pack("<f", float(value))
    if ftype == "double":
        return struct.pack("<d", float(value))
    if ftype == "fixed32":
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    if ftype == "sfixed32":
        return struct.pack("<i", int(value))
    if ftype == "fixed64":
        return struct.pack("<Q", int(value) & ((1 << 64) - 1))
    if ftype == "sfixed64":
        return struct.pack("<q", int(value))
    raise SchemaError(f"unknown fixed type {ftype}")


def _write_field(buf: bytearray, fnum: int, ftype: str, value: Any, default: Any = None) -> None:
    if ftype in _INT_TYPES or ftype == "bool" or ftype == "enum":
        if ftype == "bool":
            d = True if default is True else False
            if value == d:
                return
        elif int(value) == 0:
            return
        _write_varint(buf, (fnum << 3) | _WIRE_VARINT)
        _write_scalar_varint(buf, ftype, value)
    elif ftype in ("sint32", "sint64"):
        if int(value) == 0:
            return
        _write_varint(buf, (fnum << 3) | _WIRE_VARINT)
        _write_varint(buf, _zigzag(int(value)))
    elif ftype in _FIXED_TYPES:
        if value == 0:
            return
        _write_varint(buf, (fnum << 3) | _wire_for(ftype))
        buf.extend(_pack_fixed(ftype, value))
    elif ftype == "string":
        if value == "":
            return
        payload = str(value).encode("utf-8")
        _encode_field(buf, fnum, _WIRE_LEN, payload)
    elif ftype == "bytes":
        if not value:
            return
        payload = value if isinstance(value, (bytes, bytearray)) else bytes(value)
        _encode_field(buf, fnum, _WIRE_LEN, bytes(payload))
    elif ftype == "google.protobuf.Value":
        _encode_field(buf, fnum, _WIRE_LEN, _encode_value_bytes(value))
    elif ftype == "google.protobuf.Timestamp":
        if isinstance(value, (int, float)):
            secs, nanos = int(value), int(round((value - int(value)) * 1e9))
        elif isinstance(value, dict):
            secs, nanos = int(value.get("seconds", 0)), int(value.get("nanos", 0))
        else:
            raise SchemaError(f"bad timestamp value: {value!r}")
        payload = _encode_message(
            "Timestamp", {"seconds": secs, "nanos": nanos}
        )
        _encode_field(buf, fnum, _WIRE_LEN, payload)
    else:  # nested message
        if value is None:
            return
        payload = _encode_message(ftype, value)
        if payload or value is not None:
            _encode_field(buf, fnum, _WIRE_LEN, payload)


def encode_message(type_name: str, data: dict[str, Any] | None, force_empty: set[int] | None = None) -> bytes:
    """Public entry point: encode a message dict to wire bytes."""
    return _encode_message(type_name, data, force_empty=force_empty)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise SchemaError("truncated varint")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            raise SchemaError("varint too long")
    return result, pos


def _sign_extend(value: int, bits: int) -> int:
    if value & (1 << (bits - 1)):
        return value - (1 << bits)
    return value


def _decode_scalar_varint(ftype: str, raw: int) -> Any:
    if ftype == "bool":
        return bool(raw)
    if ftype == "int32":
        return _sign_extend(raw & 0xFFFFFFFF, 32)
    if ftype == "int64":
        return _sign_extend(raw, 64)
    if ftype in ("uint32", "uint64"):
        return raw
    if ftype in ("sint32", "sint64"):
        return _unzigzag(raw)
    if ftype == "enum":
        return raw
    raise SchemaError(f"not a varint type: {ftype}")


def _decode_fixed(ftype: str, payload: bytes) -> Any:
    if ftype == "float":
        return struct.unpack("<f", payload)[0]
    if ftype == "double":
        return struct.unpack("<d", payload)[0]
    if ftype == "fixed32":
        return struct.unpack("<I", payload)[0]
    if ftype == "sfixed32":
        return struct.unpack("<i", payload)[0]
    if ftype == "fixed64":
        return struct.unpack("<Q", payload)[0]
    if ftype == "sfixed64":
        return struct.unpack("<q", payload)[0]
    raise SchemaError(f"unknown fixed type {ftype}")


def _decode_message(type_name: str, data: bytes) -> dict[str, Any]:
    if type_name not in _FIELDS:
        raise SchemaError(f"unknown message type: {type_name}")
    fields = _FIELDS[type_name]
    out: dict[str, Any] = {}
    pos = 0
    n = len(data)
    while pos < n:
        raw, pos = _read_varint(data, pos)
        fnum = raw >> 3
        wire = raw & 7
        desc = fields.get(fnum)
        if desc is None:
            # unknown field: skip
            pos = _skip(data, pos, wire)
            continue
        ftype = desc["type"]
        if wire == _WIRE_VARINT:
            rawval, pos = _read_varint(data, pos)
            value = _decode_scalar_varint(ftype, rawval)
            _append(out, desc, value)
        elif wire == _WIRE_64BIT:
            value = _decode_fixed(ftype, data[pos:pos + 8])
            pos += 8
            _append(out, desc, value)
        elif wire == _WIRE_32BIT:
            value = _decode_fixed(ftype, data[pos:pos + 4])
            pos += 4
            _append(out, desc, value)
        elif wire == _WIRE_LEN:
            length, pos = _read_varint(data, pos)
            payload = data[pos:pos + length]
            pos += length
            if ftype in ("string",):
                value = payload.decode("utf-8", "replace")
                _append(out, desc, value)
            elif ftype in ("bytes",):
                _append(out, desc, bytes(payload))
            elif ftype == "google.protobuf.Value":
                _append(out, desc, _decode_value_bytes(payload))
            elif ftype == "google.protobuf.Timestamp":
                ts = _decode_message("Timestamp", payload)
                secs = ts.get("seconds", 0)
                nanos = ts.get("nanos", 0)
                _append(out, desc, secs + nanos / 1e9)
            elif ftype in (_INT_TYPES | {"sint32", "sint64", "bool", "enum"} | _FIXED_TYPES):
                # packed repeated
                if desc.get("repeated"):
                    sub = 0
                    while sub < len(payload):
                        if ftype in _FIXED_TYPES:
                            width = 8 if ftype in ("double", "fixed64", "sfixed64") else 4
                            item = _decode_fixed(ftype, payload[sub:sub + width])
                            sub += width
                        else:
                            rawval, sub = _read_varint(payload, sub)
                            item = _decode_scalar_varint(ftype, rawval)
                        _append(out, desc, item)
                else:
                    raise SchemaError(f"unexpected length-delimited scalar {type_name}.{desc['name']}")
            else:  # nested message
                _append(out, desc, _decode_message(ftype, payload))
        else:
            raise SchemaError(f"unsupported wire type {wire}")
    return out


def _skip(data: bytes, pos: int, wire: int) -> int:
    if wire == _WIRE_VARINT:
        _, pos = _read_varint(data, pos)
        return pos
    if wire == _WIRE_64BIT:
        return pos + 8
    if wire == _WIRE_32BIT:
        return pos + 4
    if wire == _WIRE_LEN:
        length, pos = _read_varint(data, pos)
        return pos + length
    raise SchemaError(f"unsupported wire type {wire}")


def _append(out: dict[str, Any], desc: dict[str, Any], value: Any) -> None:
    name = desc["name"]
    if desc.get("repeated"):
        out.setdefault(name, []).append(value)
    else:
        out[name] = value


def decode_message(type_name: str, data: bytes) -> dict[str, Any]:
    """Public entry point: decode wire bytes into a message dict."""
    return _decode_message(type_name, data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def enum_name(enum_type: str, value: int) -> str:
    """Return the symbolic name for an enum value (e.g. FAMILIAR)."""
    mapping = ENUMS.get(enum_type, {})
    for name, val in mapping.items():
        if val == value:
            return name
    return str(value)


def enum_value(enum_type: str, name: str) -> int:
    """Return the numeric value for an enum name."""
    mapping = ENUMS.get(enum_type, {})
    if name in mapping:
        return mapping[name]
    raise SchemaError(f"unknown {enum_type} value: {name}")


def field_names(type_name: str) -> list[str]:
    return [f["name"] for f in TYPES.get(type_name, [])]
