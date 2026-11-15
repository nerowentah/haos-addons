"""Read the Developer Mode session token from an LG webOS TV over SSH.

This is what ``ares-novacom --run`` does, without the webOS CLI: the TV in
Developer Mode runs an SSH server (user ``prisoner``) that accepts the RSA key
its key server hands out. The key is encrypted with the passphrase shown in the
Developer Mode app; it is stored as received and never decrypted to disk.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import redact
from safefile import atomic_write

DEFAULT_PORT = 9922
SSH_USER = "prisoner"
KEY_SERVER_PORT = 9991
KEY_SERVER_PATH = "/webos_rsa"
KEY_SERVER_TIMEOUT = 5
MAX_KEY_BYTES = 16 * 1024
KEY_START = b"-----BEGIN"
KEY_BLOCK_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL)
KEY_HEADER_RE = re.compile(r"(Proc-Type|DEK-Info):\s*(\S+)")  # an encrypted key in the older PEM form
BASE64_RE = re.compile(r"[A-Za-z0-9+/=]+")
PUBLIC_KEY_STARTS = ("ssh-rsa ", "ssh-ed25519 ", "ssh-dss ", "ecdsa-")
KEY_LINE_CHARS = 64
TOKEN_FILE = "/var/luna/preferences/devmode_enabled"  # noqa: S105 - a path on the TV, not a secret
TOKEN_RE = re.compile(r"^[0-9A-Za-z_-]{32,128}$")
SSH_TIMEOUT = 25
SSH_CONNECT_TIMEOUT = 8
SSH_FAILED = 255  # ssh's own exit code; anything else is the remote command's
ASKPASS = Path(__file__).resolve().parent / "askpass.sh"
PASSPHRASE_ENV = "LG_TV_PASSPHRASE"  # noqa: S105 - an env var name, not a secret


class TvError(Exception):
    """The token could not be read from the TV."""


class UnreachableError(TvError):
    """No SSH connection: the TV is off or asleep, or the address or port is wrong."""


class KeyRejectedError(TvError):
    """The TV refused the stored key (a new key or passphrase was issued)."""


class HostKeyChangedError(TvError):
    """The TV's SSH host key differs from the one pinned on first use."""


class NoTokenError(TvError):
    """The TV has no session token: Developer Mode is off."""


class KeyServerError(TvError):
    """The key could not be downloaded: Key Server is off in the Developer Mode app."""


class KeyFormatError(TvError):
    """The text given as the TV's SSH key is not a private key."""


@dataclass(frozen=True, slots=True)
class Tv:
    """Where and how to reach one TV."""

    host: redact.Secret
    port: int
    passphrase: redact.Secret
    key_path: Path
    known_hosts: Path


def valid_host(text: str) -> bool:
    """Whether ``text`` is an IP address (the only form accepted for a TV)."""

    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _key_url(host: str) -> str:
    bracketed = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host  # noqa: PLR2004 - IPv6
    return f"http://{bracketed}:{KEY_SERVER_PORT}{KEY_SERVER_PATH}"


def normalize_key(text: str) -> bytes:
    """The private key in ``text`` as a clean key file (raises KeyFormatError).

    A key pasted into a one-line field loses its line breaks (they become
    spaces, or the two characters ``\\n``): the lines are rebuilt here. The key
    stays as it is otherwise, still encrypted with its passphrase.
    """

    raw = text.replace("\\r", "").replace("\\n", "\n").strip()
    if len(raw.encode("utf-8")) > MAX_KEY_BYTES:
        raise KeyFormatError("this is too large to be the TV's SSH key")
    match = KEY_BLOCK_RE.search(raw)
    label = match[1] if match else ""
    if raw.startswith(PUBLIC_KEY_STARTS) or "PUBLIC KEY" in label:
        raise KeyFormatError("this is a public key, which cannot log in: use the TV's private key file")
    if match is None or "PRIVATE KEY" not in label:
        raise KeyFormatError(
            "no private key found: give the whole key file, from -----BEGIN ... PRIVATE KEY----- to its -----END line"
        )
    body = match[2]
    headers = [f"{name}: {value}" for name, value in KEY_HEADER_RE.findall(body)]
    data = "".join(KEY_HEADER_RE.sub("", body).split())
    if not BASE64_RE.fullmatch(data):
        raise KeyFormatError("the private key is damaged: copy the whole key file again")
    lines = [f"-----BEGIN {label}-----", *headers, *([""] if headers else [])]
    lines += [data[start : start + KEY_LINE_CHARS] for start in range(0, len(data), KEY_LINE_CHARS)]
    lines.append(f"-----END {label}-----")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _source_path(tv: Tv) -> Path:
    return tv.key_path.with_name(tv.key_path.name + ".source")


def key_source(tv: Tv) -> str:
    """Where the stored key came from: "upload", "option", "server" or "" when there is none."""

    if not tv.key_path.is_file():
        return ""
    try:
        return _source_path(tv).read_text(encoding="utf-8").strip() or "server"
    except OSError:
        return "server"  # stored by a version that only knew the key server


def store_key(tv: Tv, key: bytes, source: str) -> None:
    """Keep ``key`` as this TV's SSH key, owner-only (raises OSError)."""

    atomic_write(tv.key_path, key)
    atomic_write(_source_path(tv), source)


def fetch_key(tv: Tv) -> None:
    """Download the TV's (passphrase-encrypted) SSH key from its key server and store it owner-only."""

    try:
        with urllib.request.urlopen(_key_url(tv.host.reveal()), timeout=KEY_SERVER_TIMEOUT) as response:  # noqa: S310 - the TV's key server only speaks http
            data = response.read(MAX_KEY_BYTES + 1)
    except (urllib.error.URLError, OSError) as err:
        raise KeyServerError("the TV's key server did not answer") from err
    if not isinstance(data, bytes) or len(data) > MAX_KEY_BYTES or not data.lstrip().startswith(KEY_START):
        raise KeyServerError("the TV's key server did not send a key")
    store_key(tv, data, "server")


def ssh_command(tv: Tv, ssh: str = "ssh") -> list[str]:
    """The ssh call that prints the token file (an argument list: no shell, no passphrase)."""

    return [
        ssh,
        "-p",
        str(tv.port),
        "-i",
        str(tv.key_path),
        "-T",
        "-oBatchMode=no",
        "-oIdentitiesOnly=yes",
        "-oNumberOfPasswordPrompts=1",
        "-oHostKeyAlgorithms=+ssh-rsa",  # webOS only offers the legacy RSA host key
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        "-oStrictHostKeyChecking=accept-new",  # pinned on first use; a changed key is refused
        f"-oUserKnownHostsFile={tv.known_hosts}",
        f"-oConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-oLogLevel=ERROR",
        f"{SSH_USER}@{tv.host.reveal()}",
        "cat",
        TOKEN_FILE,
    ]


def parse_result(returncode: int, stdout: str, stderr: str) -> str:
    """The token from an ssh run, or the TvError that explains why there is none."""

    if returncode != SSH_FAILED:
        token = stdout.strip()
        if TOKEN_RE.match(token):
            return token
        if not token:
            raise NoTokenError("the TV has no Developer Mode session token")
        raise TvError("the TV's token file holds something that is not a token")
    text = stderr.lower()
    if "host key verification failed" in text or "identification has changed" in text:
        raise HostKeyChangedError("the TV's SSH host key changed")
    if "permission denied" in text or "passphrase" in text or "load key" in text or "invalid format" in text:
        raise KeyRejectedError("the TV refused the SSH key")
    raise UnreachableError("no SSH connection to the TV")


def read_token(tv: Tv) -> str:
    """The session token the TV holds right now (raises TvError)."""

    ssh = shutil.which("ssh")
    if ssh is None:
        raise TvError("no ssh client in this image")
    env = {
        **os.environ,
        PASSPHRASE_ENV: tv.passphrase.reveal(),
        "SSH_ASKPASS": str(ASKPASS),
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": "none",
    }
    try:
        done = subprocess.run(  # noqa: S603 - fixed argument list, validated IP address and port
            ssh_command(tv, ssh),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise UnreachableError("the TV did not answer in time") from err
    except OSError as err:
        raise TvError("ssh could not be started") from err
    token = parse_result(done.returncode, done.stdout, done.stderr)
    redact.register(token)
    return token


def read_token_with_key(tv: Tv, *, own_key: bool = False) -> str:
    """Read the token, downloading the key first when there is none or the TV refuses it.

    A key the TV refuses is downloaded again once; if the new one is refused
    too, the passphrase is wrong (KeyRejectedError). With ``own_key`` the key
    was given in the options: the key server is never asked.
    """

    if own_key:
        return read_token(tv)
    fresh = False
    if not tv.key_path.is_file():
        fetch_key(tv)
        fresh = True
    try:
        return read_token(tv)
    except KeyRejectedError:
        if fresh:
            raise
    fetch_key(tv)
    return read_token(tv)
