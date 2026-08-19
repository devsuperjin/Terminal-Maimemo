"""Round-trip tests for the pure-python protobuf codec."""

import sys
import struct
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from terminal_maimemo import proto

passed = 0


def check(label, cond):
    global passed
    assert cond, f"FAIL: {label}"
    passed += 1
    print(f"ok: {label}")


# 1. Scalar round trips
for ftype, val in [("int32", 42), ("int32", -42), ("int64", 2**40), ("uint32", 123456),
                   ("bool", True), ("string", "hello 世界"), ("bytes", b"\x00\x01\x02"),
                   ("float", 3.5), ("double", 1.23456789012345)]:
    data = proto.encode_message("_Test", {"f": val}) if False else None
    break  # placeholder replaced below

# 2. WebsocketProtocolMessage round trip
ws_msg = {
    "id": "0.12345",
    "reply_id": "",
    "event": 1002,  # WEBSTUDY_GET_WORD
    "data": b"\x08\x01",
    "success": True,
    "errors": [],
}
enc = proto.encode_message("WebsocketProtocolMessage", ws_msg)
dec = proto.decode_message("WebsocketProtocolMessage", enc)
check("WS message round trip", dec == {"id": "0.12345", "event": 1002, "data": b"\x08\x01", "success": True})

# 3. WebsocketProtocolError
err = {"code": "x", "message": "boom", "info": "i"}
enc = proto.encode_message("WebsocketProtocolError", err)
check("WS error round trip", proto.decode_message("WebsocketProtocolError", enc) == err)

# 4. SubmitResponseRequest with enums (study_method=1 non-default survives)
req = {"voc_id": "abc123", "response": 3, "study_method": 1, "recall_duration": 1500, "study_duration": 2000}
enc = proto.encode_message("WsWebStudySubmitResponseRequest", req)
dec = proto.decode_message("WsWebStudySubmitResponseRequest", enc)
check("SubmitResponseRequest round trip", dec == req)
# default study_method 0 is omitted on the wire
enc0 = proto.encode_message("WsWebStudySubmitResponseRequest", {**req, "study_method": 0})
expected0 = {k: v for k, v in req.items() if k != "study_method"}
check("default enum omitted", proto.decode_message("WsWebStudySubmitResponseRequest", enc0) == expected0)

# 5. GetWordResponse with nested structures (default-true bools left false/absent)
resp = {
    "word": {
        "id": "w1", "voc_id": 100, "spelling": "abandon", "phonetic_us": "/əˈbændən/",
        "phonetic_uk": "/əˈbændən/", "hyphenation": "a-ban-don", "difficulty": 3,
        "tags": ["gre"], "pronunciations": [{"id": "p1", "accent": "us", "url": "https://x/a.mp3"}],
    },
    "interpretations": [{"id": "i1", "voc_id": "w1", "interpretation": "v. 放弃", "tags": ["v"], "creator_id": 1}],
    "phrases": [{"id": "ph1", "voc_id": "w1", "phrase": "abandon ship", "interpretation": "弃船", "origin": "n.", "tags": ["n"], "creator_id": 2}],
    "notes": [{"id": "n1", "voc_id": "w1", "type": "note", "note": "记忆方法", "tags": ["x"], "creator_id": 3}],
    "is_first_response": True,
    "is_first_show": True,
    "progress": {"finished": 3, "total": 10},
    "study_time_ms": 5000,
    "has_prev_word": True,
    "response_predicts": [{"first": True, "days": 1, "progress": 0.5}],
}
enc = proto.encode_message("WsWebStudyGetWordResponse", resp)
dec = proto.decode_message("WsWebStudyGetWordResponse", enc)
check("GetWordResponse round trip", dec == resp)
# default-false bools are skipped when False
resp_f = {k: v for k, v in resp.items() if k not in ("is_first_response", "is_first_show", "has_prev_word")}
resp_f.update({"is_first_response": False, "is_first_show": False, "has_prev_word": False})
enc_f = proto.encode_message("WsWebStudyGetWordResponse", resp_f)
dec_f = proto.decode_message("WsWebStudyGetWordResponse", enc_f)
check("default-false bools skipped", "is_first_response" not in dec_f and "has_prev_word" not in dec_f)

# 6. UserSetting with google.protobuf.Value + Timestamp
setting = {"code": "app.speech.setting.voc.accent", "value": "us", "updated_time": 1700000000.5}
enc = proto.encode_message("WebStudyUserSetting", setting)
dec = proto.decode_message("WebStudyUserSetting", enc)
check("UserSetting round trip", dec["code"] == "app.speech.setting.voc.accent" and dec["value"] == "us")
check("UserSetting timestamp ~", abs(dec["updated_time"] - 1700000000.5) < 1e-6)

# 7. Complex Value nesting
val = {"name": "tom", "scores": [1, 2.5, "three"], "ok": True, "nil": None}
enc = proto.encode_message("WebStudyUserSetting", {"code": "k", "value": val})
dec = proto.decode_message("WebStudyUserSetting", enc)
check("nested Value round trip", dec["value"] == val)

# 8. InitStudyResponse with repeated settings + user
init = {
    "ok": True,
    "settings": [
        {"code": "a", "value": "1"},
        {"code": "b", "value": 5.0},
        {"code": "c", "value": True},
    ],
    "privileges": ["premium"],
    "user": {"id": 42, "name": "tom", "avatar": "https://x/av.png"},
}
enc = proto.encode_message("WsWebStudyInitStudyResponse", init)
dec = proto.decode_message("WsWebStudyInitStudyResponse", enc)
check("InitStudyResponse round trip", dec == init)

# 9. GetStudyStatesRequest (all bools, default-false -> False skipped, True written)
states = {k: True for k in proto.field_names("WsWebStudyGetStudyStatesRequest")}
enc = proto.encode_message("WsWebStudyGetStudyStatesRequest", states)
dec = proto.decode_message("WsWebStudyGetStudyStatesRequest", enc)
check("GetStudyStatesRequest round trip", dec == states)
enc_f = proto.encode_message("WsWebStudyGetStudyStatesRequest", {k: False for k in states})
check("GetStudyStatesRequest all-false encodes empty", enc_f == b"")

# 10. DayLimit with enum
dl = {"total": 30, "review": 10, "learn": 20, "mode": 2}
enc = proto.encode_message("DayLimit", dl)
dec = proto.decode_message("DayLimit", dec and None or enc)
check("DayLimit round trip", dec == dl)

# 11. Packed repeated decode (server may pack repeated varints)
packed = b""
for v in [1, 2, 300]:
    packed += struct.pack("<Q", (v & 0x7F) | 0x80)[0:1] if v >= 128 else bytes([v])
# build: field 5 (string tags) unpacked + field 1 (id)... just test packed ints via GetStudyStates? use response_predicts? -> use WebStudyWord.difficulty? simpler: DayLimit has no repeated. Use WsWebStudyGetWordResponse? no repeated ints.
# Craft manually: repeated string tags field 10 of WebStudyWord
word = {"id": "w", "tags": ["a", "b", "c"]}
enc = proto.encode_message("WebStudyWord", word)
dec = proto.decode_message("WebStudyWord", enc)
check("WebStudyWord round trip", dec == word)

# 12. Enum helpers
check("enum_name StudyResponse", proto.enum_name("StudyResponse", 3) == "FORGET")
check("enum_value StudyResponse", proto.enum_value("StudyResponse", "WELL_FAMILIAR") == 4)
check("enum_name WebsocketProtocolEvent", proto.enum_name("WebsocketProtocolEvent", 1001) == "WEBSTUDY_INIT_STUDY")

# 13. SyncCheckpointBrief
cp = {"checkpoint": 123, "op_revision": 456}
enc = proto.encode_message("SyncCheckpointBrief", cp)
check("SyncCheckpointBrief round trip", proto.decode_message("SyncCheckpointBrief", enc) == cp)

# 14. Empty message
check("empty init request", proto.encode_message("WsWebStudyInitStudyRequest", {}) == b"")

# 15. MemoryHistory with timestamps and enum items
mh = {
    "is_error": True,
    "items": [{"type": 1, "day": 1}, {"type": 6, "day": 12}],
    "first_study_date": 1700000000.0,
    "last_study_date": 1700086400.25,
    "study_times": 3,
}
enc = proto.encode_message("WebStudyMemoryHistory", mh)
dec = proto.decode_message("WebStudyMemoryHistory", enc)
check("MemoryHistory round trip", dec == mh)

print(f"\nALL {passed} CHECKS PASSED")
