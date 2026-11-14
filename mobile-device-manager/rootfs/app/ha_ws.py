"""Minimal Home Assistant websocket client (via the Supervisor proxy)."""

from __future__ import annotations

import json
from typing import Self

import websocket

WS_URL = "ws://supervisor/core/websocket"
WS_TIMEOUT = 60


def _receive(ws: websocket.WebSocket) -> dict[str, object]:
    """The next message; ValueError when it is not a JSON object."""

    message = json.loads(ws.recv())
    if not isinstance(message, dict):
        raise ValueError("the reply is not a JSON object")  # noqa: TRY004 - handled with the other bad replies
    return message


class WsError(RuntimeError):
    """Raised when the websocket API rejects a request or the connection fails."""


class HaSocket:
    """One authenticated connection; use as a context manager."""

    def __init__(self, token: str, url: str = WS_URL) -> None:
        self._token = token
        self._url = url
        self._ws: websocket.WebSocket | None = None
        self._next_id = 1

    def __enter__(self) -> Self:
        try:
            self._ws = websocket.create_connection(self._url, timeout=WS_TIMEOUT)
            hello = _receive(self._ws)
            if hello.get("type") != "auth_required":
                raise WsError(f"Unexpected websocket greeting: {hello.get('type')}")
            self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            reply = _receive(self._ws)
        except (OSError, ValueError, websocket.WebSocketException) as err:
            self.__exit__()
            raise WsError(f"Websocket connection failed: {err}") from err
        if reply.get("type") != "auth_ok":
            self.__exit__()
            raise WsError("Websocket authentication failed")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def call(self, message_type: str, **payload: object) -> object:
        """Send one command and wait for its result."""

        if self._ws is None:
            raise WsError("Websocket is not connected")
        msg_id = self._next_id
        self._next_id += 1
        try:
            self._ws.send(json.dumps({"id": msg_id, "type": message_type, **payload}))
            while True:
                reply = _receive(self._ws)
                if reply.get("id") == msg_id:
                    break
        except (OSError, ValueError, websocket.WebSocketException) as err:
            raise WsError(f"{message_type} failed: {err}") from err
        if not reply.get("success"):
            error = reply.get("error")
            detail = error.get("message") if isinstance(error, dict) else None
            raise WsError(f"{message_type} failed: {detail or error or 'no reason given'}")
        return reply.get("result")
