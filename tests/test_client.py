"""Tests for the maimemo client frame codec (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal_maimemo import client, proto

passed = 0


def check(label, cond):
    global passed
    assert cond, f"FAIL: {label}"
    passed += 1
    print(f"ok: {label}")


# 1. Request frame encode/decode round trip
raw = client._encode_frame(
    "WEBSTUDY_SUBMIT_RESPONSE",
    "request",
    request_id="0.42",
    data={"voc_id": "w1", "response": 3, "study_method": 1, "recall_duration": 100, "study_duration": 200},
)
frame = client._decode_frame(raw)
check("submit frame event", frame["event"] == "WEBSTUDY_SUBMIT_RESPONSE")
check("submit frame id", frame.get("id") == "0.42")
check("submit frame data", frame["data"]["voc_id"] == "w1" and frame["data"]["response"] == 3)

# 2. Reply frame decode (simulate a server reply)
reply_raw = client._encode_frame(
    "WEBSTUDY_SUBMIT_RESPONSE",
    "response",
    reply_id="0.42",
    data={"next": {"word": {"id": "w2", "spelling": "go"}, "progress": {"finished": 1, "total": 10}}},
)
frame = client._decode_frame(reply_raw)
check("reply frame reply_id", frame.get("reply_id") == "0.42")
check("reply frame next word", frame["data"]["next"]["word"]["spelling"] == "go")

# 3. Errors in reply (server omits success=false; errors signal failure)
err_raw = client._encode_frame(
    "WEBSTUDY_GET_WORD",
    "response",
    reply_id="x",
    success=False,
    errors=[{"code": "some_error", "message": "boom", "info": ""}],
)
frame = client._decode_frame(err_raw)
check("reply errors decoded", frame.get("success") is False and frame["errors"][0]["message"] == "boom")
# successful reply carries success=true explicitly
ok_raw = client._encode_frame(
    "WEBSTUDY_GET_WORD", "response", reply_id="y", success=True, data={}
)
check("reply success true", client._decode_frame(ok_raw).get("success") is True)

# 4. initStudy frame round trip with settings
raw = client._encode_frame(
    "WEBSTUDY_INIT_STUDY",
    "request",
    request_id="0.1",
    data={},
)
frame = client._decode_frame(raw)
check("init frame ok", frame["event"] == "WEBSTUDY_INIT_STUDY" and frame.get("id") == "0.1")

# 5. get word request frame
raw = client._encode_frame("WEBSTUDY_GET_WORD", "request", request_id="0.2", data={"back": False})
frame = client._decode_frame(raw)
check("get word frame", frame["event"] == "WEBSTUDY_GET_WORD")

# 6. review more
raw = client._encode_frame("WEBSTUDY_REVIEW_MORE", "request", request_id="0.3", data={"count": 10})
frame = client._decode_frame(raw)
check("review more frame", frame["data"]["count"] == 10)

# 7. WebsocketProtocolEvent mapping sanity
check("event numbers", client.EVENT["WEBSTUDY_INIT_STUDY"] == 1001 and client.EVENT["SYSTEM_READY"] == 1)
check("all EVENT_TYPES in schema", all(
    client.EVENT_TYPES[name][0] in proto.TYPES and client.EVENT_TYPES[name][1] in proto.TYPES
    for name in client.EVENT_TYPES
))

# 8. study states frame
raw = client._encode_frame(
    "WEBSTUDY_GET_STUDY_STATES",
    "request",
    request_id="0.4",
    data={f: True for f in proto.field_names("WsWebStudyGetStudyStatesRequest")},
)
frame = client._decode_frame(raw)
check("study states frame", frame["event"] == "WEBSTUDY_GET_STUDY_STATES")

print(f"\nALL {passed} CHECKS PASSED")
