"""Let Home Assistant accept requests that arrive through the tunnel.

Home Assistant answers "400 Bad Request" to proxied requests until its
``http:`` block trusts the proxy. This module adds that block to
``configuration.yaml`` when it can do so safely, and otherwise reports the
snippet to paste:

* no top-level ``http:`` key: the block is appended;
* a plain block-style ``http:`` mapping: the missing keys (or the missing
  ``trusted_proxies`` entry) are inserted at the block's own indentation;
* anything else (``!include``, flow style, ``use_x_forwarded_for: false``...):
  left alone, the user is shown the snippet.

The file is backed up first, and Home Assistant's own configuration check
must pass, or the backup is restored. Home Assistant is never restarted here.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from supervisor import Supervisor, SupervisorError

_LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("CF_TUNNEL_HA_CONFIG", "/homeassistant/configuration.yaml"))
BACKUP_SUFFIX = ".cloudflare-tunnel.bak"
DEFAULT_NETWORK = "172.30.33.0/24"  # the Supervisor's network for apps
COMMENT = "# Added by the Cloudflare Tunnel app: lets Home Assistant accept requests that arrive through the tunnel.\n"

STATUS_PRESENT = "present"  # already trusted: nothing to do
STATUS_ADDED = "added"  # written and checked; a Home Assistant restart applies it
STATUS_MANUAL = "manual"  # not safe to edit automatically: show the snippet
STATUS_INVALID = "invalid"  # the config check failed after the edit: restored
STATUS_ERROR = "error"  # could not read, write or check: restored

_TOP_KEY = re.compile(r"^(?P<key>[A-Za-z0-9_]+):(?P<rest>.*)$")
_CHILD = re.compile(r"^(?P<indent> +)(?P<key>[A-Za-z0-9_]+):(?P<rest>.*)$")
_ITEM = re.compile(r"^(?P<indent> *)- +(?P<value>.*)$")
_TRUE = {"true", "yes", "on"}


@dataclass(slots=True)
class Result:
    """What happened to configuration.yaml, in words for the panel."""

    status: str
    message: str


def network_for(ip: str | None) -> str:
    """The /24 around this app's address (normally 172.30.33.0/24)."""

    try:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False)) if ip else DEFAULT_NETWORK
    except ValueError:
        return DEFAULT_NETWORK


def snippet(network: str) -> str:
    """The block to paste into configuration.yaml by hand."""

    return f"http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - {network}"


def _value(text: str) -> str:
    """A YAML scalar without its trailing comment and quotes."""

    text = re.sub(r"\s+#.*$", "", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":  # noqa: PLR2004 - a quoted value
        text = text[1:-1]
    return text.strip()


def _covers(entries: list[str], network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    for entry in entries:
        try:
            trusted = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue  # !secret, a hostname... not ours to judge
        if trusted.version == network.version and network.subnet_of(trusted):  # type: ignore[arg-type]
            return True
    return False


def ensure_trusted_proxy(text: str, network: str) -> tuple[str | None, str]:  # noqa: PLR0911, PLR0912, PLR0915 - one linear YAML walk
    """``(new_text, status)``: the edited configuration, or None when nothing (or nothing safe) can be done."""

    net = ipaddress.ip_network(network, strict=False)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    start = next(
        (i for i, line in enumerate(lines) if (m := _TOP_KEY.match(line.rstrip("\r\n"))) and m["key"] == "http"),
        None,
    )
    if start is None:
        lead = "" if not text or text.endswith("\n") else newline
        block = COMMENT + snippet(str(net)) + "\n"
        return text + lead + newline + block.replace("\n", newline), STATUS_ADDED

    head = _TOP_KEY.match(lines[start].rstrip("\r\n"))
    if head is None or _value(head["rest"]):
        return None, STATUS_MANUAL  # http: !include ..., http: {...}

    end = start + 1
    while end < len(lines):
        raw = lines[end].rstrip("\r\n")
        if raw and not raw[0].isspace() and not raw.startswith("#"):
            break
        end += 1
    body = [lines[i].rstrip("\r\n") for i in range(start + 1, end)]
    if any("\t" in raw[: len(raw) - len(raw.lstrip())] for raw in body):
        return None, STATUS_MANUAL  # tab indentation: leave it to the user
    content = [raw for raw in body if raw.strip() and not raw.lstrip().startswith("#")]
    indent = (content[0][: len(content[0]) - len(content[0].lstrip())]) if content else "  "

    keys: dict[str, tuple[int, str]] = {}
    for offset, raw in enumerate(body):
        child = _CHILD.match(raw)
        if child and child["indent"] == indent:
            keys[child["key"]] = (start + 1 + offset, _value(child["rest"]))

    forwarded = keys.get("use_x_forwarded_for")
    if forwarded and forwarded[1].lower() not in _TRUE:
        return None, STATUS_MANUAL  # explicitly off: the user decided something

    item_at: int | None = None
    item_indent = indent + "  "
    proxies = keys.get("trusted_proxies")
    if proxies:
        index, rest = proxies
        if rest:  # inline: [a, b] or a single value
            entries = [_value(x) for x in rest.strip("[]").split(",")] if rest.startswith("[") else [rest]
            if not _covers(entries, net):
                return None, STATUS_MANUAL
        else:
            entries = []
            last = index
            for j in range(index + 1, end):
                raw = lines[j].rstrip("\r\n")
                item = _ITEM.match(raw)
                if item and len(item["indent"]) >= len(indent):
                    entries.append(_value(item["value"]))
                    item_indent = item["indent"]
                    last = j
                elif raw.strip() and not raw.lstrip().startswith("#"):
                    break
            if not _covers(entries, net):
                item_at = last + 1

    if forwarded and proxies and item_at is None:
        return None, STATUS_PRESENT

    if item_at is not None:
        lines.insert(item_at, f"{item_indent}- {net}{newline}")
    added: list[str] = []
    if not forwarded:
        added.append(f"{indent}use_x_forwarded_for: true{newline}")
    if not proxies:
        added += [f"{indent}trusted_proxies:{newline}", f"{indent}  - {net}{newline}"]
    if not lines[start].endswith(("\n", "\r")):
        lines[start] += newline
    lines[start + 1 : start + 1] = added
    return "".join(lines), STATUS_ADDED


def _write(path: Path, text: str) -> None:
    """Replace ``path`` atomically, keeping its permissions."""

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cloudflare-tunnel-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply(supervisor: Supervisor, network: str, path: Path = CONFIG_PATH) -> Result:  # noqa: PLR0911 - one outcome per step
    """Make Home Assistant trust ``network``; never restarts Home Assistant."""

    manual = f"Add this to configuration.yaml yourself, then restart Home Assistant:\n{snippet(network)}"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Result(STATUS_MANUAL, manual)
    except OSError as err:
        return Result(STATUS_ERROR, f"Could not read {path.name}: {err}. {manual}")
    new_text, status = ensure_trusted_proxy(text, network)
    if status == STATUS_PRESENT or new_text is None:
        if status == STATUS_PRESENT:
            return Result(STATUS_PRESENT, "Home Assistant already trusts the tunnel")
        return Result(STATUS_MANUAL, f"configuration.yaml has an http: block this app does not edit. {manual}")

    backup = path.with_name(path.name + BACKUP_SUFFIX)
    try:
        shutil.copy2(path, backup)
        _write(path, new_text)
    except OSError as err:
        return Result(STATUS_ERROR, f"Could not update {path.name}: {err}. {manual}")

    def restore() -> None:
        try:
            _write(path, text)
        except OSError:
            _LOGGER.exception("Could not restore %s; the original is in %s", path, backup)

    try:
        valid, errors = supervisor.check_core_config()
    except SupervisorError as err:
        restore()
        return Result(STATUS_ERROR, f"Home Assistant's configuration check did not run ({err}); nothing was changed.")
    if not valid:
        restore()
        return Result(
            STATUS_INVALID,
            f"Home Assistant's configuration check failed, so configuration.yaml was left unchanged: {errors}",
        )
    _LOGGER.info("Added trusted_proxies %s to %s (backup: %s)", network, path, backup.name)
    return Result(
        STATUS_ADDED,
        f"Added {network} to trusted_proxies in configuration.yaml (backup: {backup.name}). "
        "Restart Home Assistant to apply it.",
    )
