"""Ingress web panel server (stdlib only, shared verbatim by every app in this repo).

The page in ``web/`` is generic: it renders whatever view model ``get_state``
returns (sections -> cards -> stats / toggles / links / notice / actions) and
posts changes back:

* ``GET  api/state``              -> ``get_state()``
* ``POST api/toggle``  {id, value} -> ``on_toggle(id, value)``
* ``POST api/action``  {id, args}  -> ``on_action(id, args)``

Only the Supervisor ingress proxy (172.30.32.2) may connect; Home Assistant has
already authenticated the user (and ``panel_admin`` limits it to admins).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

_LOGGER = logging.getLogger(__name__)

INGRESS_PORT = 8099
ALLOWED_CLIENTS = frozenset({"172.30.32.2", "127.0.0.1", "::1"})
MAX_BODY_BYTES = 64 * 1024
STATIC_TYPES = {".html", ".js", ".css", ".svg", ".png", ".ico"}

StateGetter = Callable[[], dict[str, object]]
ToggleHandler = Callable[[str, bool], None]
ActionHandler = Callable[[str, dict[str, object]], str | None]


class PanelError(ValueError):
    """A user-facing error returned to the panel as HTTP 400."""


class PanelServer:
    """Serves the panel files and the small JSON API on the ingress port."""

    def __init__(
        self,
        web_dir: Path,
        get_state: StateGetter,
        on_toggle: ToggleHandler,
        on_action: ActionHandler,
        port: int = INGRESS_PORT,
    ) -> None:
        self.web_dir = web_dir.resolve()
        self.get_state = get_state
        self.on_toggle = on_toggle
        self.on_action = on_action
        self.port = port
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        """Start serving in a daemon thread."""

        panel = self

        class Handler(_Handler):
            server_panel = panel

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)  # noqa: S104 - ingress proxy reaches the container IP
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="panel", daemon=True).start()
        _LOGGER.info("Web panel listening on port %d (open it from the Home Assistant sidebar)", self.port)

    def stop(self) -> None:
        """Stop serving."""

        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_panel: PanelServer
    server_version = "panel/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        _LOGGER.debug("panel: " + format, *args)

    def _allowed(self) -> bool:
        if self.client_address[0] in ALLOWED_CLIENTS:
            return True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "Open this page from the Home Assistant sidebar"})
        return False

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if not self._allowed():
            return
        path = urlsplit(self.path).path
        if path.endswith("/api/state") or path == "api/state":
            try:
                self._send_json(HTTPStatus.OK, self.server_panel.get_state())
            except Exception:
                _LOGGER.exception("Panel state failed")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Could not build the panel state"})
            return

        name = path.rsplit("/", 1)[-1] or "index.html"
        web_dir = self.server_panel.web_dir
        target = (web_dir / name).resolve()
        if target.parent != web_dir or target.suffix not in STATIC_TYPES or not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)

    def do_POST(self) -> None:
        if not self._allowed():
            return
        path = urlsplit(self.path).path
        if not path.endswith(("/api/toggle", "/api/action")):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            message = self._dispatch(path, self._read_body())
        except PanelError as err:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(err)})
            return
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request"})
            return
        except Exception:
            _LOGGER.exception("Panel request failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Something went wrong; see the app log"})
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "message": message})

    def _read_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise PanelError("Request too large")
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise PanelError("Expected a JSON object")
        return body

    def _dispatch(self, path: str, body: dict[str, object]) -> str | None:
        if path.endswith("/api/toggle"):
            value = body.get("value")
            if not isinstance(value, bool):
                raise PanelError("value must be true or false")
            self.server_panel.on_toggle(str(body.get("id") or ""), value)
            return None
        args = body.get("args")
        return self.server_panel.on_action(str(body.get("id") or ""), args if isinstance(args, dict) else {})


# -- View-model helpers (shared by the apps) -----------------------------------
def toggle(
    toggle_id: str, label: str, value: bool, description: str = "", *, disabled: bool = False
) -> dict[str, object]:
    """A switch row."""

    return {"id": toggle_id, "label": label, "value": value, "description": description, "disabled": disabled}


def stat(label: str, value: object, unit: str = "") -> dict[str, object]:
    """A label/value tile ("-" when value is None)."""

    return {"label": label, "value": "-" if value is None or value == "" else str(value), "unit": unit}


HA_PATH_PREFIX = "/config/"


def action(  # noqa: PLR0913 - view-model fields
    action_id: str,
    label: str,
    args: dict[str, object] | None = None,
    *,
    disabled: bool = False,
    confirm: str = "",
    danger: bool = False,
    reason: str = "",
) -> dict[str, object]:
    """A button; ``confirm`` asks first, ``reason`` explains why it is disabled."""

    return {
        "id": action_id,
        "label": label,
        "args": args or {},
        "disabled": disabled,
        "confirm": confirm,
        "danger": danger,
        "reason": reason,
    }


def link(label: str, path: str, description: str = "", *, button: str = "Edit") -> dict[str, object]:
    """A row (in ``links``) or button (in ``actions``) that opens a Home Assistant page, e.g. an automation."""

    if not path.startswith(HA_PATH_PREFIX):
        raise ValueError(f"panel links must point into {HA_PATH_PREFIX}")
    return {"label": label, "path": path, "description": description, "button": button}


def notice(text: str, tone: str = "warn") -> dict[str, str]:
    """A highlighted message inside a card; tone is ok | warn | error | neutral."""

    return {"text": text, "tone": tone}


def badge(text: str, tone: str = "neutral") -> dict[str, str]:
    """A status chip; tone is ok | warn | error | neutral | off."""

    return {"text": text, "tone": tone}
