"""Ingress web panel server (stdlib only, shared verbatim by every app in this repo).

Edit this file in ``shared/app`` only, then run ``python tools/sync_shared.py``.

The page in ``web/`` is generic: it renders whatever view model ``get_state``
returns (sections -> cards -> stats / toggles / links / notice / actions) and
posts changes back:

* ``GET  api/state``              -> ``get_state()``
* ``POST api/toggle``  {id, value} -> ``on_toggle(id, value)``
* ``POST api/action``  {id, args}  -> ``on_action(id, args)``

Cards, sections, toggles, links and actions accept an optional ``icon`` (an
``mdi:`` name); ``web/icons.js`` holds the SVG paths of the names in use.

An action with ``upload`` opens the browser's file picker first and sends the
chosen text files as ``args["files"]`` (read them with :func:`uploaded_files`).

Only the Supervisor ingress proxy (172.30.32.2) may connect; Home Assistant has
already authenticated the user (and ``panel_admin`` limits it to admins).

Defence in depth on top of that:

* a change (POST) must be JSON, carry the ``X-Panel-Request`` header the page
  adds and, when the browser says where it came from (``Sec-Fetch-Site``), come
  from the panel's own origin - so another web site cannot make the signed-in
  browser press a button (CSRF);
* every answer has a Content-Security-Policy that only allows the panel's own
  files, and may only be framed by Home Assistant itself;
* connections time out, and the size of a request is checked before it is read.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

_LOGGER = logging.getLogger(__name__)

INGRESS_PORT = 8099
ALLOWED_CLIENTS = frozenset({"172.30.32.2", "127.0.0.1", "::1"})
MAX_BODY_BYTES = 1024 * 1024  # room for a few uploaded text files
MAX_UPLOAD_FILES = 8
MAX_UPLOAD_BYTES = 64 * 1024  # per file; the page checks this before sending
MAX_TEXT_CHARS = 2000  # a line typed into a prompt
REQUEST_TIMEOUT_SECONDS = 15  # a client that stops sending is dropped
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

# The page's own fetch() adds this header. A form on another site cannot, and a
# script on another site would need a CORS preflight, which is never answered.
REQUEST_HEADER = "X-Panel-Request"
SAME_ORIGIN_SITES = frozenset({"same-origin", "none"})  # Sec-Fetch-Site values of the panel itself
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)

StateGetter = Callable[[], dict[str, object]]
ToggleHandler = Callable[[str, bool], None]
ActionHandler = Callable[[str, "PanelArgs"], str | None]


class PanelError(ValueError):
    """A user-facing error returned to the panel as HTTP 400."""


class _RejectedError(Exception):
    """A request that is refused before it reaches the app (status, message for the page)."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class PanelArgs(dict[str, object]):
    """The ``args`` of an action, as sent by the page: untrusted, so read them through these checks.

    It is still a ``dict``; the methods raise :class:`PanelError` (shown to the
    user) when a value is missing or is not what the action expects.
    """

    def text(self, key: str = "text", *, max_len: int = MAX_TEXT_CHARS, required: bool = False) -> str:
        """A line of text: stripped, without control characters, at most ``max_len`` characters."""

        value = self.get(key)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise PanelError(f"{key} must be text")
        text = value.strip()
        if len(text) > max_len:
            raise PanelError(f"{key} is longer than {max_len} characters")
        if any(not char.isprintable() for char in text):
            raise PanelError(f"{key} must be a single line of text")
        if required and not text:
            raise PanelError(f"{key} is required")
        return text

    def ident(self, key: str, pattern: re.Pattern[str] | None = None, *, max_len: int = 200) -> str:
        """An identifier the page echoes back (an account, a device, a key): required, and matching ``pattern``."""

        text = self.text(key, max_len=max_len, required=True)
        if pattern is not None and not pattern.fullmatch(text):
            raise PanelError(f"{key} is not valid")
        return text

    def flag(self, key: str, *, default: bool | None = None) -> bool:
        """A true/false value (``default`` when the page did not send it)."""

        value = self.get(key)
        if value is None and default is not None:
            return default
        if not isinstance(value, bool):
            raise PanelError(f"{key} must be true or false")
        return value

    def files(self) -> list[tuple[str, str]]:
        """``(name, text)`` of the files an ``upload`` action sent (see :func:`uploaded_files`)."""

        return uploaded_files(self)


class PanelTarget(Protocol):
    """What the router forwards to: the app, or a :class:`SetupView` while the options are invalid."""

    def panel_state(self) -> dict[str, object]:
        """Current view model."""

    def panel_toggle(self, toggle_id: str, value: bool, /) -> None:
        """A switch was flipped."""

    def panel_action(self, action_id: str, args: PanelArgs, /) -> str | None:
        """A button was pressed; the returned text is shown to the user."""


class PanelRouter:
    """Routes panel calls to whatever is active: a SetupView while options are invalid, then the app.

    Each app object provides ``panel_state()``, ``panel_toggle(id, value)`` and ``panel_action(id, args)``.
    """

    def __init__(self, target: PanelTarget | None = None) -> None:
        self.target = target

    def get_state(self) -> dict[str, object]:
        """Current view model."""

        if self.target is None:
            return {"title": "Starting", "subtitle": "Starting up...", "sections": []}
        return self.target.panel_state()

    def on_toggle(self, toggle_id: str, value: bool) -> None:
        """Forward a switch."""

        if self.target is None:
            raise PanelError("Still starting; try again in a moment")
        self.target.panel_toggle(toggle_id, value)

    def on_action(self, action_id: str, args: PanelArgs) -> str | None:
        """Forward a button."""

        if self.target is None:
            raise PanelError("Still starting; try again in a moment")
        return self.target.panel_action(action_id, args)


class SetupView:
    """Panel shown while the app options are invalid: the app waits instead of crash-looping."""

    def __init__(self, title: str, error: str, how_to_fix: str, example: str = "") -> None:
        self.title = title
        self.error = error
        self.how_to_fix = how_to_fix
        self.example = example

    def panel_state(self) -> dict[str, object]:
        """View model: the problem, how to fix it and an example."""

        cards: list[dict[str, object]] = [
            {
                "title": "Configuration needs attention",
                "icon": "mdi:alert-circle",
                "badge": badge("Waiting", "warn"),
                "notice": notice(self.error, "error"),
                "empty": self.how_to_fix,
            }
        ]
        if self.example:
            cards.append({"title": "Example", "icon": "mdi:file-document-edit", "empty": self.example, "mono": True})
        return {
            "title": self.title,
            "subtitle": "Waiting for a valid configuration",
            "sections": [{"title": "Setup", "icon": "mdi:cog", "cards": cards}],
        }

    def panel_toggle(self, _toggle_id: str, _value: bool) -> None:
        """Nothing to switch yet."""

        raise PanelError("Fix the configuration first")

    def panel_action(self, _action_id: str, _args: PanelArgs) -> str | None:
        """Nothing to run yet."""

        raise PanelError("Fix the configuration first")


SECRET_PREFIX = "!secret "  # noqa: S105 - the reference syntax, not a secret


class SecretError(ValueError):
    """An option names a ``!secret`` whose value this run does not have."""


class SecretRefs:
    """``!secret name`` references in the stored options, with the values the Supervisor resolved.

    The Supervisor resolves ``!secret`` only into /data/options.json, when the
    app starts; its API (``/addons/self/info``) returns the references. Live
    option changes therefore come back as references: resolve them with the
    values from start-up, and write the references (never the values) back.
    """

    def __init__(self) -> None:
        self._values: dict[str, object] = {}

    def __len__(self) -> int:
        return len(self._values)

    def learn(self, stored: object, resolved: object) -> None:
        """Record the resolved value of every reference in ``stored`` (walked in step with ``resolved``)."""

        if isinstance(stored, str):
            if stored.startswith(SECRET_PREFIX) and resolved is not None and resolved != stored:
                self._values[stored[len(SECRET_PREFIX) :].strip()] = resolved
        elif isinstance(stored, dict) and isinstance(resolved, dict):
            for key, value in stored.items():
                self.learn(value, resolved.get(key))
        elif isinstance(stored, list) and isinstance(resolved, list):
            for value, resolved_value in zip(stored, resolved, strict=False):
                self.learn(value, resolved_value)

    def resolve(self, raw: object) -> object:
        """A copy of ``raw`` with every reference replaced by its value (raises SecretError)."""

        if isinstance(raw, str) and raw.startswith(SECRET_PREFIX):
            name = raw[len(SECRET_PREFIX) :].strip()
            if name not in self._values:
                raise SecretError(
                    f"An option uses !secret {name}, which was added after the app started; restart the app to load it"
                )
            return self._values[name]
        if isinstance(raw, dict):
            return {key: self.resolve(value) for key, value in raw.items()}
        if isinstance(raw, list):
            return [self.resolve(value) for value in raw]
        return raw


def initial_options(
    load_resolved: Callable[[], dict[str, object]],
    fetch_stored: Callable[[], dict[str, object]],
    secrets: SecretRefs,
) -> dict[str, object]:
    """Options to start with: the stored ones (with references) when the Supervisor answers, else the file.

    Teaches ``secrets`` the resolved values from /data/options.json on the way.
    """

    try:
        resolved: dict[str, object] | None = load_resolved()
    except ValueError:
        resolved = None
    try:
        stored: dict[str, object] | None = fetch_stored()
    except (OSError, RuntimeError, ValueError):
        stored = None
    if stored is not None and resolved is not None:
        secrets.learn(stored, resolved)
    if stored is not None:
        return stored
    return resolved or {}


def wait_for_valid_config(
    fetch: Callable[[], dict[str, object]],
    validate: Callable[[dict[str, object]], object],
    view: SetupView,
    stop: threading.Event,
    interval: float = 15.0,
) -> tuple[dict[str, object], object] | None:
    """Poll the saved options until ``validate`` accepts them; None when stopped first."""

    while not stop.wait(interval):
        try:
            raw = fetch()
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            return raw, validate(raw)
        except ValueError as err:
            if str(err) != view.error:
                view.error = str(err)
                _LOGGER.error("Configuration still invalid: %s", err)  # noqa: TRY400 - no traceback needed
    return None


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
        self._server: _Server | None = None

    def start(self) -> None:
        """Start serving in a daemon thread."""

        panel = self

        class Handler(_Handler):
            server_panel = panel

        self._server = _Server(("0.0.0.0", self.port), Handler)  # noqa: S104 - ingress proxy reaches the container IP
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        threading.Thread(target=self._server.serve_forever, name="panel", daemon=True).start()
        _LOGGER.info("Web panel listening on port %d (open it from the Home Assistant sidebar)", self.port)

    def stop(self) -> None:
        """Stop serving."""

        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


class _Server(ThreadingHTTPServer):
    """Drops a connection from anything but the ingress proxy before a thread is started for it."""

    def verify_request(self, request: object, client_address: object) -> bool:  # noqa: ARG002 - socketserver's signature
        return isinstance(client_address, tuple) and client_address[0] in ALLOWED_CLIENTS


class _Handler(BaseHTTPRequestHandler):
    server_panel: PanelServer
    server_version = "panel/1"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS

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
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
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
        content_type = STATIC_TYPES.get(target.suffix)
        if target.parent != web_dir or content_type is None or not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)

    def do_POST(self) -> None:
        if not self._allowed():
            return
        path = urlsplit(self.path).path
        if not path.endswith(("/api/toggle", "/api/action")):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            status, payload = self._answer(path)
        except TimeoutError:  # the client stopped sending
            self.close_connection = True
            return
        self._send_json(status, payload)

    def _answer(self, path: str) -> tuple[HTTPStatus, dict[str, object]]:
        """Run one change and say what to send back; nothing in a request can make this raise (but a timeout)."""

        try:
            self._check_origin()
            message = self._dispatch(path, self._read_body())
        except _RejectedError as err:
            self.close_connection = True  # the body may not have been read
            return err.status, {"error": err.message}
        except PanelError as err:
            return HTTPStatus.BAD_REQUEST, {"error": str(err)}
        except (ValueError, RecursionError):
            return HTTPStatus.BAD_REQUEST, {"error": "Invalid request"}
        except TimeoutError:
            raise
        except Exception:
            _LOGGER.exception("Panel request failed")
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Something went wrong; see the app log"}
        return HTTPStatus.OK, {"ok": True, "message": message}

    def _check_origin(self) -> None:
        """Refuse a change that did not come from the panel page itself (CSRF)."""

        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise _RejectedError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected application/json")
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if self.headers.get(REQUEST_HEADER) != "1" or (site and site not in SAME_ORIGIN_SITES):
            raise _RejectedError(HTTPStatus.FORBIDDEN, "Reload this page and try again")

    def _read_body(self) -> dict[str, object]:
        if self.headers.get("Transfer-Encoding"):
            raise _RejectedError(HTTPStatus.BAD_REQUEST, "Invalid request")
        raw_length = (self.headers.get("Content-Length") or "").strip()
        if not raw_length:
            raise _RejectedError(HTTPStatus.LENGTH_REQUIRED, "Invalid request")
        if not raw_length.isascii() or not raw_length.isdigit():
            raise _RejectedError(HTTPStatus.BAD_REQUEST, "Invalid request")
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            raise _RejectedError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _RejectedError(HTTPStatus.BAD_REQUEST, "Invalid request")
        body = json.loads(raw or b"{}")
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
        return self.server_panel.on_action(str(body.get("id") or ""), PanelArgs(args if isinstance(args, dict) else {}))


# -- View-model helpers (shared by the apps) -----------------------------------
def toggle(  # noqa: PLR0913 - view-model fields
    toggle_id: str, label: str, value: bool, description: str = "", *, disabled: bool = False, icon: str = ""
) -> dict[str, object]:
    """A switch row (``icon`` is an ``mdi:`` name known to web/icons.js)."""

    return {
        "id": toggle_id,
        "label": label,
        "value": value,
        "description": description,
        "disabled": disabled,
        "icon": icon,
    }


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
    icon: str = "",
    prompt: str = "",
    prompt_value: str = "",
    upload: str = "",
) -> dict[str, object]:
    """A button; ``confirm`` asks first, ``reason`` explains why it is disabled.

    ``prompt`` asks for a line of text first (pre-filled with ``prompt_value``); it arrives as ``args["text"]``.
    ``upload`` (an ``accept`` list such as ``".pem,.crt"``) asks for text files first; they arrive as
    ``args["files"]`` (see :func:`uploaded_files`). Do not combine it with ``confirm``: browsers only open
    a file picker straight after a click.
    """

    return {
        "id": action_id,
        "label": label,
        "args": args or {},
        "disabled": disabled,
        "confirm": confirm,
        "danger": danger,
        "reason": reason,
        "icon": icon,
        "prompt": prompt,
        "prompt_value": prompt_value,
        "upload": upload,
    }


def uploaded_files(args: dict[str, object]) -> list[tuple[str, str]]:
    """``(name, text)`` of the files an ``upload`` action sent; raises PanelError when there are none or too many."""

    rows = args.get("files")
    if not isinstance(rows, list) or not rows:
        raise PanelError("Choose a file to upload")
    if len(rows) > MAX_UPLOAD_FILES:
        raise PanelError(f"Choose at most {MAX_UPLOAD_FILES} files")
    files: list[tuple[str, str]] = []
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise PanelError("Invalid upload")
        text = str(item["text"])
        name = str(item.get("name") or "file")[:120]
        if len(text.encode("utf-8")) > MAX_UPLOAD_BYTES:
            raise PanelError(f"{name} is larger than {MAX_UPLOAD_BYTES // 1024} KB")
        files.append((name, text))
    return files


def link(label: str, path: str, description: str = "", *, button: str = "Edit", icon: str = "") -> dict[str, object]:
    """A row (in ``links``) or button (in ``actions``) that opens a Home Assistant page, e.g. an automation."""

    if not path.startswith(HA_PATH_PREFIX):
        raise ValueError(f"panel links must point into {HA_PATH_PREFIX}")
    return {"label": label, "path": path, "description": description, "button": button, "icon": icon}


def external(
    label: str, url: str, description: str = "", *, button: str = "Open", icon: str = "mdi:open-in-new"
) -> dict[str, object]:
    """A row (in ``links``) or button (in ``actions``) that opens an https:// page in a new browser tab."""

    if not url.startswith("https://"):
        raise ValueError("external links must use https://")
    return {"label": label, "url": url, "path": "", "description": description, "button": button, "icon": icon}


def row(label: str, description: str = "", *, icon: str = "") -> dict[str, object]:
    """A plain row (in ``links``) without a button, e.g. a note or a list entry."""

    return {"label": label, "path": "", "description": description, "button": "", "icon": icon}


def notice(text: str, tone: str = "warn") -> dict[str, str]:
    """A highlighted message inside a card; tone is ok | warn | error | neutral."""

    return {"text": text, "tone": tone}


def badge(text: str, tone: str = "neutral") -> dict[str, str]:
    """A status chip; tone is ok | warn | error | neutral | off."""

    return {"text": text, "tone": tone}


def hero_item(label: str, value: object, unit: str = "") -> dict[str, object]:
    """A large headline figure (a card's ``hero`` is a list of these)."""

    return stat(label, value, unit)


def progress(fraction: float, label: str, caption: str = "") -> dict[str, object]:
    """A thin progress bar; ``fraction`` is clamped to 0..1."""

    return {"value": round(min(max(fraction, 0.0), 1.0), 4), "label": label, "caption": caption}


def chart(title: str, bars: list[tuple[str, float]], unit: str = "", caption: str = "") -> dict[str, object] | None:
    """A small bar chart of (label, value) pairs, oldest first; None when there is nothing to draw."""

    if not bars:
        return None
    return {
        "title": title,
        "unit": unit,
        "caption": caption,
        "bars": [{"label": label, "value": round(float(value), 3)} for label, value in bars],
    }
