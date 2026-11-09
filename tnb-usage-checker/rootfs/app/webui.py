"""Ingress web panel server (stdlib only, shared verbatim by every app in this repo).

The page in ``web/`` is generic: it renders whatever view model ``get_state``
returns (sections -> cards -> stats / toggles / links / notice / actions) and
posts changes back:

* ``GET  api/state``              -> ``get_state()``
* ``POST api/toggle``  {id, value} -> ``on_toggle(id, value)``
* ``POST api/action``  {id, args}  -> ``on_action(id, args)``

Cards, sections, toggles, links and actions accept an optional ``icon`` (an
``mdi:`` name); ``web/icons.js`` holds the SVG paths of the names in use.

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


class PanelRouter:
    """Routes panel calls to whatever is active: a SetupView while options are invalid, then the app.

    Each app object provides ``panel_state()``, ``panel_toggle(id, value)`` and ``panel_action(id, args)``.
    """

    def __init__(self, target: object | None = None) -> None:
        self.target = target

    def get_state(self) -> dict[str, object]:
        """Current view model."""

        if self.target is None:
            return {"title": "Starting", "subtitle": "Starting up...", "sections": []}
        return self.target.panel_state()  # type: ignore[attr-defined]

    def on_toggle(self, toggle_id: str, value: bool) -> None:
        """Forward a switch."""

        if self.target is None:
            raise PanelError("Still starting; try again in a moment")
        self.target.panel_toggle(toggle_id, value)  # type: ignore[attr-defined]

    def on_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Forward a button."""

        if self.target is None:
            raise PanelError("Still starting; try again in a moment")
        return self.target.panel_action(action_id, args)  # type: ignore[attr-defined]


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

    def panel_action(self, _action_id: str, _args: dict[str, object]) -> str | None:
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
) -> dict[str, object]:
    """A button; ``confirm`` asks first, ``reason`` explains why it is disabled.

    ``prompt`` asks for a line of text first (pre-filled with ``prompt_value``); it arrives as ``args["text"]``.
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
    }


def link(label: str, path: str, description: str = "", *, button: str = "Edit", icon: str = "") -> dict[str, object]:
    """A row (in ``links``) or button (in ``actions``) that opens a Home Assistant page, e.g. an automation."""

    if not path.startswith(HA_PATH_PREFIX):
        raise ValueError(f"panel links must point into {HA_PATH_PREFIX}")
    return {"label": label, "path": path, "description": description, "button": button, "icon": icon}


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
