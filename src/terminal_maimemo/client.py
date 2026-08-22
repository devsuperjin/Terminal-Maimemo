"""Maimemo web-study client.

Implements the protocol used by https://tc-apis.maimemo.com/webstudy/app:

* REST  ``POST https://tc-apis.maimemo.com/study/api/v1/webstudy/precheck``
  authenticated with the ``sid`` session cookie (set by the login callback).
* WebSocket ``wss://tc-apis.maimemo.com/study/ws/webstudy?token=`` carrying
  protobuf frames (``WebsocketProtocolMessage``); the session cookie
  authenticates the handshake; the study session is driven by request/reply
  events such as ``WEBSTUDY_INIT_STUDY``, ``WEBSTUDY_GET_WORD`` and
  ``WEBSTUDY_SUBMIT_RESPONSE``.

Events and the protobuf schema were reverse-engineered from the app's JS
bundles (see ``schema.json`` / ``proto.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from . import proto

log = logging.getLogger(__name__)

API_HOST = "https://tc-apis.maimemo.com"
WS_HOST = "wss://tc-apis.maimemo.com"

# WebsocketProtocolEvent values (from the extracted enum)
EVENT = {
    "SYSTEM_READY": 1,
    "SYSTEM_PING": 2,
    "SYSTEM_SUBSCRIBE_TOPICS": 3,
    "WEBSTUDY_INIT_STUDY": 1001,
    "WEBSTUDY_GET_WORD": 1002,
    "WEBSTUDY_SUBMIT_RESPONSE": 1003,
    "WEBSTUDY_EVENT_REVISION_CHANGE": 1004,
    "WEBSTUDY_DELETE_WORDS": 1005,
    "WEBSTUDY_GET_STUDY_STATES": 1006,
    "WEBSTUDY_REVIEW_MORE": 1007,
    "WEBSTUDY_PRECHECK_SIGN": 1008,
    "WEBSTUDY_SIGN": 1009,
    "WEBSTUDY_ADVANCE_REVIEW": 1010,
    "WEBSTUDY_ADD_WORDS": 1011,
    "WEBSTUDY_SET_STUDY_SETTINGS": 1012,
    "WEBSTUDY_QUERY_WORD": 1100,
    "WEBSTUDY_SEARCH_BOOKS": 1101,
    "WEBSTUDY_SET_STUDY_BOOK": 1102,
    "WEBSTUDY_GET_RECOMMEND_BOOKS": 1103,
    "WEBSTUDY_QUERY_SEARCH_HISTORY": 1104,
    "WEBSTUDY_GET_CONTENT_PREFERENCE_TAGS": 1121,
    "WEBSTUDY_SET_CONTENT_PREFERENCE_TAGS": 1122,
}

# event name -> (request type, response type)
EVENT_TYPES: dict[str, tuple[str, str]] = {
    "SYSTEM_READY": ("WsSystemReadyRequest", "WsSystemReadyResponse"),
    "SYSTEM_PING": ("WsSystemPingRequest", "WsSystemPingResponse"),
    "SYSTEM_SUBSCRIBE_TOPICS": ("WsSystemSubscribeTopicsRequest", "WsSystemSubscribeTopicsResponse"),
    "WEBSTUDY_INIT_STUDY": ("WsWebStudyInitStudyRequest", "WsWebStudyInitStudyResponse"),
    "WEBSTUDY_GET_WORD": ("WsWebStudyGetWordRequest", "WsWebStudyGetWordResponse"),
    "WEBSTUDY_SUBMIT_RESPONSE": ("WsWebStudySubmitResponseRequest", "WsWebStudySubmitResponseResponse"),
    "WEBSTUDY_DELETE_WORDS": ("WsWebStudyDeleteWordsRequest", "WsWebStudyDeleteWordsResponse"),
    "WEBSTUDY_GET_STUDY_STATES": ("WsWebStudyGetStudyStatesRequest", "WsWebStudyGetStudyStatesResponse"),
    "WEBSTUDY_REVIEW_MORE": ("WsWebStudyReviewMoreRequest", "WsWebStudyReviewMoreResponse"),
    "WEBSTUDY_PRECHECK_SIGN": ("WsWebStudyPrecheckSignRequest", "WsWebStudyPrecheckSignResponse"),
    "WEBSTUDY_SIGN": ("WsWebStudySignRequest", "WsWebStudySignResponse"),
    "WEBSTUDY_ADVANCE_REVIEW": ("WsWebStudyAdvanceReviewRequest", "WsWebStudyAdvanceReviewResponse"),
    "WEBSTUDY_QUERY_WORD": ("WsWebStudyQueryWordRequest", "WsWebStudyQueryWordResponse"),
    "WEBSTUDY_ADD_WORDS": ("WsWebStudyAddWordsRequest", "WsWebStudyAddWordsResponse"),
    "WEBSTUDY_SET_STUDY_SETTINGS": ("WsWebStudySetStudySettingsRequest", "WsWebStudySetStudySettingsResponse"),
    "WEBSTUDY_SEARCH_BOOKS": ("WsWebStudySearchBooksRequest", "WsWebStudySearchBooksResponse"),
    "WEBSTUDY_SET_STUDY_BOOK": ("WsWebStudySetStudyBookRequest", "WsWebStudySetStudyBookResponse"),
    "WEBSTUDY_GET_RECOMMEND_BOOKS": ("WsWebStudyGetRecommendBooksRequest", "WsWebStudyGetRecommendBooksResponse"),
    "WEBSTUDY_QUERY_SEARCH_HISTORY": ("WsWebStudyQuerySearchHistoryRequest", "WsWebStudyQuerySearchHistoryResponse"),
    "WEBSTUDY_GET_CONTENT_PREFERENCE_TAGS": (
        "WsWebStudyGetContentPreferenceTagsRequest",
        "WsWebStudyGetContentPreferenceTagsResponse",
    ),
    "WEBSTUDY_SET_CONTENT_PREFERENCE_TAGS": (
        "WsWebStudySetContentPreferenceTagsRequest",
        "WsWebStudySetContentPreferenceTagsResponse",
    ),
}

REPLY_TIMEOUT = 15.0


class MaimemoError(Exception):
    """Base error for API / protocol failures."""


class ApiError(MaimemoError):
    """REST API returned an error payload."""

    def __init__(self, status: int, errors: list[dict[str, str]] | None = None):
        self.status = status
        self.errors = errors or []
        msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in self.errors) or f"HTTP {status}"
        super().__init__(msg)


class WsReplyError(MaimemoError):
    """WebSocket reply carried errors."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors or []
        msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in self.errors) or "unknown WS error"
        super().__init__(msg)


class WsTimeoutError(MaimemoError):
    pass


def _encode_frame(
    event_name: str,
    kind: str,
    *,
    request_id: str = "",
    reply_id: str = "",
    success: bool = True,
    errors: list[dict[str, str]] | None = None,
    data: dict[str, Any] | None = None,
) -> bytes:
    """Encode a WebsocketProtocolMessage frame (protobuf bytes)."""
    req_type, resp_type = EVENT_TYPES[event_name]
    type_name = req_type if kind == "request" else resp_type
    payload = None
    if data is not None:
        payload = proto.encode_message(type_name, data)
    frame = {
        "event": EVENT[event_name],
        "id": request_id,
        "reply_id": reply_id,
        "success": success,
        "errors": errors or [],
    }
    if payload is not None:
        frame["data"] = payload
        # the web app always writes the data field, even when empty
        return proto.encode_message("WebsocketProtocolMessage", frame, force_empty={4})
    return proto.encode_message("WebsocketProtocolMessage", frame)


def _decode_frame(raw: bytes) -> dict[str, Any]:
    """Decode a WebsocketProtocolMessage frame into a friendly dict."""
    msg = proto.decode_message("WebsocketProtocolMessage", raw)
    event_name = proto.enum_name("WebsocketProtocolEvent", msg.get("event", 0))
    out: dict[str, Any] = {
        "event": event_name,
        "errors": msg.get("errors", []),
        "success": msg.get("success", False),  # proto3 default
    }
    data = msg.get("data")
    if msg.get("reply_id"):
        out["reply_id"] = msg["reply_id"]
        if data is not None and event_name in EVENT_TYPES:
            _, resp_type = EVENT_TYPES[event_name]
            out["data"] = proto.decode_message(resp_type, data)
    else:
        if msg.get("id"):
            out["id"] = msg["id"]
        if data is not None and event_name in EVENT_TYPES:
            req_type, _ = EVENT_TYPES[event_name]
            out["data"] = proto.decode_message(req_type, data)
    return out


class MaimemoClient:
    """Async client for the maimemo web-study service."""

    def __init__(
        self,
        sid: str,
        *,
        api_host: str = API_HOST,
        ws_host: str = WS_HOST,
    ):
        self.sid = sid
        self.api_host = api_host.rstrip("/")
        self.ws_host = ws_host.rstrip("/")
        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._intentional_close = False
        self.unexpected_close = False
        self.settings: dict[str, Any] = {}
        self.user: dict[str, Any] = {}
        self.privileges: list[str] = []
        self.dictionary_settings: dict[str, Any] | None = None
        self.study_states: dict[str, Any] = {}

    # ------------------------------------------------------------------ REST
    async def precheck(self) -> dict[str, Any]:
        """POST /study/api/v1/webstudy/precheck; raises ApiError on failure."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.api_host}/study/api/v1/webstudy/precheck",
                headers={"Cookie": f"sid={self.sid}"},
            )
        if resp.status_code >= 400:
            try:
                body = resp.json()
                errors = body.get("errors")
            except Exception:
                errors = None
            raise ApiError(resp.status_code, errors)
        body = resp.json()
        return body.get("data", {})

    # ------------------------------------------------------------------- WS
    async def connect(self) -> None:
        """Connect the websocket, wait for SYSTEM_READY, run init study."""
        if self._ws is not None:
            return
        # auth is the `sid` cookie (the web app sets it on the login callback)
        url = f"{self.ws_host}/study/ws/webstudy?token="
        self._ws = await websockets.connect(
            url,
            additional_headers={"Cookie": f"sid={self.sid}"},
            subprotocols=None,
            max_size=2**24,
            open_timeout=20,
        )
        self._recv_task = asyncio.create_task(self._recv_loop())
        # wait for SYSTEM_READY
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except asyncio.TimeoutError:
            await self.close()
            raise MaimemoError("timeout waiting for SYSTEM_READY")
        init = await self.invoke("WEBSTUDY_INIT_STUDY", {}, no_queue=True)
        if init:
            self.user = init.get("user") or {}
            self.privileges = init.get("privileges") or []
            self.dictionary_settings = init.get("dictionary_settings")
            settings = init.get("settings") or []
            self.settings = {s.get("code"): s.get("value") for s in settings if s.get("code")}

    async def close(self) -> None:
        self._intentional_close = True
        self._closed.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._recv_task is not None:
            self._recv_task.cancel()
            self._recv_task = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MaimemoError("connection closed"))
        self._pending.clear()

    async def reconnect(self) -> None:
        """Tear down and re-establish the study session."""
        await self.close()
        self._intentional_close = False
        self.unexpected_close = False
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self.settings = {}
        self.user = {}
        self.study_states = {}
        await self.connect()

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    frame = _decode_frame(raw)
                except Exception as exc:  # defensive: keep the loop alive
                    log.warning("bad frame: %s", exc)
                    continue
                event = frame.get("event", "")
                if event == "SYSTEM_PING":
                    # reply to keepalive ping (mirrors the web app)
                    await self._ws.send(
                        _encode_frame(
                            "SYSTEM_PING",
                            "response",
                            reply_id=frame.get("id", ""),
                        )
                    )
                    continue
                if event == "SYSTEM_READY":
                    self._ready.set()
                    continue
                reply_id = frame.get("reply_id")
                if reply_id and reply_id in self._pending:
                    fut = self._pending.pop(reply_id)
                    if frame.get("success") is False or frame.get("errors"):
                        fut.set_exception(WsReplyError(frame.get("errors")))
                    else:
                        fut.set_result(frame.get("data"))
        except ConnectionClosed:
            pass
        except Exception as exc:  # pragma: no cover
            log.warning("recv loop error: %s", exc)
        finally:
            if not self._intentional_close:
                self.unexpected_close = True
            self._closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MaimemoError("connection closed"))
            self._pending.clear()

    async def _invoke(self, event_name: str, data: dict[str, Any] | None) -> dict[str, Any] | None:
        if self._ws is None:
            raise MaimemoError("not connected")
        request_id = str(random.random())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        frame = _encode_frame(event_name, "request", request_id=request_id, data=data)
        try:
            await self._ws.send(frame)
        except Exception as exc:
            self._pending.pop(request_id, None)
            raise MaimemoError(f"send failed: {exc}") from exc
        try:
            return await asyncio.wait_for(fut, timeout=REPLY_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise WsTimeoutError(f"no reply for {event_name} within {REPLY_TIMEOUT}s") from None

    async def invoke(self, event_name: str, data: dict[str, Any] | None = None, *, no_queue: bool = False) -> dict[str, Any] | None:
        """Send a request/reply event and return the decoded response data."""
        if no_queue:
            return await self._invoke(event_name, data)
        return await self._invoke(event_name, data)

    # ------------------------------------------------------------- study API
    async def get_word(self, back: bool = False) -> dict[str, Any]:
        """Fetch the current word, including its decoded memory history."""
        data = await self.invoke("WEBSTUDY_GET_WORD", {"back": back})
        return data or {}

    async def submit_response(
        self,
        voc_id: str,
        response: int,
        *,
        study_method: int = 0,
        recall_duration: int = 0,
        study_duration: int = 0,
    ) -> dict[str, Any]:
        data = await self.invoke(
            "WEBSTUDY_SUBMIT_RESPONSE",
            {
                "voc_id": voc_id,
                "response": response,
                "study_method": study_method,
                "recall_duration": recall_duration,
                "study_duration": study_duration,
            },
        )
        return data or {}

    async def review_more(self, count: int) -> dict[str, Any]:
        data = await self.invoke("WEBSTUDY_REVIEW_MORE", {"count": count})
        return data or {}

    async def delete_words(self, voc_ids: list[str]) -> None:
        await self.invoke("WEBSTUDY_DELETE_WORDS", {"voc_ids": voc_ids})

    async def add_words(self, voc_ids: list[str]) -> None:
        await self.invoke(
            "WEBSTUDY_ADD_WORDS",
            {"words": [{"voc_id": v} for v in voc_ids]},
        )

    async def advance_review(self, voc_ids: list[str], count: int = 0, behind_voc_id: str = "") -> None:
        await self.invoke(
            "WEBSTUDY_ADVANCE_REVIEW",
            {"voc_ids": voc_ids, "count": count, "behind_voc_id": behind_voc_id},
        )

    async def get_study_states(self, fields: list[str] | None = None) -> dict[str, Any]:
        """Request study states; `fields` selects which to return (default: all)."""
        known = proto.field_names("WsWebStudyGetStudyStatesRequest")
        req = {f: True for f in known} if fields is None else {f: True for f in fields if f in known}
        data = await self.invoke("WEBSTUDY_GET_STUDY_STATES", req)
        self.study_states = data or {}
        return self.study_states

    async def precheck_sign(self) -> dict[str, Any]:
        data = await self.invoke("WEBSTUDY_PRECHECK_SIGN", {})
        return data or {}

    async def sign(self, verify_data: str = "") -> dict[str, Any]:
        data = await self.invoke("WEBSTUDY_SIGN", {"verify_data": verify_data})
        return data or {}

    async def query_word(self, spelling: str = "", phrase: str = "", interpretation: str = "") -> dict[str, Any]:
        data = await self.invoke(
            "WEBSTUDY_QUERY_WORD",
            {"spelling": spelling, "phrase": phrase, "interpretation": interpretation},
        )
        return data or {}

    async def set_study_method(self, method: str) -> None:
        """method: 'CE' (CN->EN) or 'EC' (EN->CN)."""
        study_method = "STUDY_CN_EN" if method == "CE" else "STUDY_EN_CN"
        await self.invoke(
            "WEBSTUDY_SET_STUDY_SETTINGS",
            {"study_method": proto.enum_value("StudyMethod", study_method)},
        )

    async def get_recommend_books(self) -> dict[str, Any]:
        data = await self.invoke("WEBSTUDY_GET_RECOMMEND_BOOKS", {})
        return data or {}

    async def set_study_book(self, book_id: str) -> None:
        await self.invoke("WEBSTUDY_SET_STUDY_BOOK", {"book_id": book_id})
