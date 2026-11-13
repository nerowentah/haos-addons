"""Edit Home Assistant's ``http:`` block in ``configuration.yaml``, safely.

Two edits, both line-preserving (comments, order and other keys stay as they are):

* :func:`ensure_trusted_proxy` - ``use_x_forwarded_for`` + ``trusted_proxies``, so
  Home Assistant accepts requests that arrive through this app's HTTPS proxy
  (the same edit the Cloudflare Tunnel app makes);
* :func:`ensure_ssl` / :func:`remove_ssl` - ``ssl_certificate`` + ``ssl_key``, which
  make Home Assistant itself serve HTTPS (opt-in; every http:// URL then stops working).

Only a plain block-style ``http:`` mapping (or none at all) is edited; anything
else (``!include``, flow style, keys someone else set) is left alone and the
snippet is shown instead. :func:`apply` backs the file up, writes it, runs Home
Assistant's configuration check and restores the original when the check fails.
Home Assistant is never restarted here.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from supervisor import Supervisor, SupervisorError

_LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("SSL_CERT_MANAGER_HA_CONFIG", "/homeassistant/configuration.yaml"))
BACKUP_SUFFIX = ".ssl-cert-manager.bak"
DEFAULT_NETWORK = "172.30.33.0/24"  # the Supervisor's network for apps
PROXY_COMMENT = "# Added by the SSL Cert Manager app: lets Home Assistant accept requests from its HTTPS proxy.\n"
SSL_COMMENT = "# Added by the SSL Cert Manager app: Home Assistant serves HTTPS with its certificate.\n"
SSL_MARK = "  # managed by SSL Cert Manager"

STATUS_PRESENT = "present"  # already as wanted: nothing to do
STATUS_ADDED = "added"  # written and checked; a Home Assistant restart applies it
STATUS_REMOVED = "removed"  # written and checked; a Home Assistant restart applies it
STATUS_MANUAL = "manual"  # not safe to edit automatically: show the snippet
STATUS_INVALID = "invalid"  # the config check failed after the edit: restored
STATUS_ERROR = "error"  # could not read, write or check: restored

_TOP_KEY = re.compile(r"^(?P<key>[A-Za-z0-9_]+):(?P<rest>.*)$")
_CHILD = re.compile(r"^(?P<indent> +)(?P<key>[A-Za-z0-9_]+):(?P<rest>.*)$")
_ITEM = re.compile(r"^(?P<indent> *)- +(?P<value>.*)$")
_TRUE = {"true", "yes", "on"}

Edit = Callable[[str], tuple[str | None, str]]


@dataclass(slots=True)
class Result:
    """What happened to configuration.yaml, in words for the panel."""

    status: str
    message: str


@dataclass(slots=True)
class _Block:
    """A top-level block-style ``http:`` mapping: where it is and its direct keys."""

    lines: list[str]
    newline: str
    start: int
    end: int
    indent: str
    keys: dict[str, tuple[int, str]]  # key -> (line index, scalar value)


def network_for(ip: str | None) -> str:
    """The /24 around this app's address (normally 172.30.33.0/24)."""

    try:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False)) if ip else DEFAULT_NETWORK
    except ValueError:
        return DEFAULT_NETWORK


def proxy_snippet(network: str) -> str:
    return f"http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - {network}"


def ssl_snippet(cert: str, key: str) -> str:
    return f"http:\n  ssl_certificate: {cert}\n  ssl_key: {key}"


def _value(text: str) -> str:
    """A YAML scalar without its trailing comment and quotes."""

    text = re.sub(r"\s+#.*$", "", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":  # noqa: PLR2004 - a quoted value
        text = text[1:-1]
    return text.strip()


def _find_block(text: str) -> tuple[_Block | None, str]:
    """The ``http:`` block, or (None, STATUS_ADDED) when there is none, or (None, STATUS_MANUAL) when it is not editable."""

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if (m := _TOP_KEY.match(line.rstrip("\r\n"))) and m["key"] == "http"),
        None,
    )
    if start is None:
        return None, STATUS_ADDED
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
    return _Block(lines, newline, start, end, indent, keys), ""


def _append(text: str, comment: str, block: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    lead = "" if not text or text.endswith("\n") else newline
    return text + lead + newline + (comment + block + "\n").replace("\n", newline)


def _insert_after_head(block: _Block, added: list[str]) -> str:
    lines = block.lines
    if not lines[block.start].endswith(("\n", "\r")):
        lines[block.start] += block.newline
    lines[block.start + 1 : block.start + 1] = added
    return "".join(lines)


# -- trusted proxies ----------------------------------------------------------------
def _covers(entries: list[str], network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    for entry in entries:
        try:
            trusted = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue  # !secret, a hostname... not ours to judge
        if trusted.version == network.version and network.subnet_of(trusted):  # type: ignore[arg-type]
            return True
    return False


def ensure_trusted_proxy(text: str, network: str) -> tuple[str | None, str]:  # noqa: PLR0912 - one linear YAML walk
    """``(new_text, status)``: the edited configuration, or None when nothing (or nothing safe) can be done."""

    net = ipaddress.ip_network(network, strict=False)
    block, status = _find_block(text)
    if block is None:
        if status == STATUS_ADDED:
            return _append(text, PROXY_COMMENT, proxy_snippet(str(net))), STATUS_ADDED
        return None, status
    lines, indent, keys, newline = block.lines, block.indent, block.keys, block.newline

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
            for j in range(index + 1, block.end):
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
    return _insert_after_head(block, added), STATUS_ADDED


# -- Home Assistant's own HTTPS -----------------------------------------------------
def ensure_ssl(text: str, cert: str, key: str) -> tuple[str | None, str]:
    """Add ``ssl_certificate``/``ssl_key`` pointing at this app's files (MANUAL if someone else set them)."""

    block, status = _find_block(text)
    if block is None:
        if status == STATUS_ADDED:
            return _append(text, SSL_COMMENT, ssl_snippet(cert, key)), STATUS_ADDED
        return None, status
    current = {name: block.keys[name][1] for name in ("ssl_certificate", "ssl_key") if name in block.keys}
    if current == {"ssl_certificate": cert, "ssl_key": key}:
        return None, STATUS_PRESENT
    if current:
        return None, STATUS_MANUAL  # another certificate is configured: the user decides
    indent, newline = block.indent, block.newline
    added = [f"{indent}ssl_certificate: {cert}{SSL_MARK}{newline}", f"{indent}ssl_key: {key}{SSL_MARK}{newline}"]
    return _insert_after_head(block, added), STATUS_ADDED


def remove_ssl(text: str, cert: str, key: str) -> tuple[str | None, str]:
    """Remove the ``ssl_certificate``/``ssl_key`` lines that point at this app's files (and an emptied block)."""

    block, status = _find_block(text)
    if block is None:
        return None, STATUS_PRESENT if status == STATUS_ADDED else status
    wanted = {"ssl_certificate": cert, "ssl_key": key}
    drop = [index for name, (index, value) in block.keys.items() if wanted.get(name) == value]
    others = [
        name for name in ("ssl_certificate", "ssl_key") if name in block.keys and wanted[name] != block.keys[name][1]
    ]
    if others:
        return None, STATUS_MANUAL
    if not drop:
        return None, STATUS_PRESENT  # nothing of ours is there
    lines = block.lines
    other_lines = [i for i in range(block.start + 1, block.end) if lines[i].strip() and i not in drop]
    if not other_lines:
        # The block only held our two keys (as the app appends it): remove it, its comment and the blank line before.
        first, last = block.start, block.end
        while last > block.start + 1 and not lines[last - 1].strip():
            last -= 1  # blank lines after the block separate it from the next key: keep them
        if first > 0 and lines[first - 1] == SSL_COMMENT.replace("\n", block.newline):
            first -= 1
        if first > 0 and not lines[first - 1].strip():
            first -= 1
        del lines[first:last]
        return "".join(lines), STATUS_REMOVED
    for index in sorted(drop, reverse=True):
        del lines[index]
    return "".join(lines), STATUS_REMOVED


# -- writing ------------------------------------------------------------------------
def _write(path: Path, text: str) -> None:
    """Replace ``path`` atomically, keeping its permissions."""

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ssl-cert-manager-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply(supervisor: Supervisor, edit: Edit, manual: str, path: Path = CONFIG_PATH) -> Result:  # noqa: PLR0911 - one outcome per step
    """Run ``edit`` on configuration.yaml with backup and config check; never restarts Home Assistant.

    ``manual`` is the snippet shown when the file cannot (or may not) be edited.
    """

    how = f"Change configuration.yaml yourself, then restart Home Assistant:\n{manual}"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Result(STATUS_MANUAL, how)
    except OSError as err:
        return Result(STATUS_ERROR, f"Could not read {path.name}: {err}. {how}")
    new_text, status = edit(text)
    if new_text is None:
        if status == STATUS_PRESENT:
            return Result(STATUS_PRESENT, "configuration.yaml already has this setting")
        return Result(STATUS_MANUAL, f"configuration.yaml has an http: block this app does not edit. {how}")

    backup = path.with_name(path.name + BACKUP_SUFFIX)
    try:
        shutil.copy2(path, backup)
        _write(path, new_text)
    except OSError as err:
        return Result(STATUS_ERROR, f"Could not update {path.name}: {err}. {how}")

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
    verb = "Removed the setting from" if status == STATUS_REMOVED else "Updated"
    _LOGGER.info("%s %s (backup: %s)", verb, path, backup.name)
    return Result(status, f"{verb} configuration.yaml (backup: {backup.name}). Restart Home Assistant to apply it.")
